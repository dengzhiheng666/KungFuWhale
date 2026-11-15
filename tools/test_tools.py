# -*- coding: utf-8 -*-
"""Background smoke-test tools for the Agent: smoke_test and test_result."""
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from common import CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, IS_WIN, _num
from python_env import _command_env, _terminate_process, _venv_python_or_default

_TEST_JOBS = {}
_TEST_JOBS_LOCK = threading.Lock()

# Candidate names for auto-detecting the entry file (ordered by priority)
_SMOKE_ENTRY_CANDIDATES = (
    "main.py", "app.py", "run.py", "server.py", "manage.py", "wsgi.py", "start.py",
)

# Keywords for judging "obvious errors" in output (best-effort, for reference only)
_SMOKE_ERROR_MARKERS = (
    "Traceback (most recent call last)", "FATAL", "Unhandled exception",
    "ModuleNotFoundError", "ImportError", "SyntaxError",
)


def _detect_project_entry(root):
    """Auto-detect the Python project entry file and return its relative path; returns None if none is detected."""
    for name in _SMOKE_ENTRY_CANDIDATES:
        if (root / name).is_file():
            return name
    return None


def _snapshot_cache_dirs(root):
    """Record the __pycache__ / .pytest_cache directories that existed before the test, so that newly created caches can be cleaned up afterward."""
    found = set()
    try:
        for dirpath, dirnames, _files in os.walk(root):
            for d in dirnames:
                if d in ("__pycache__", ".pytest_cache"):
                    found.add(str(Path(dirpath) / d))
    except OSError:
        pass
    return found


def _cleanup_new_caches(root, before):
    """Delete the __pycache__ / .pytest_cache directories newly created during the smoke test."""
    try:
        to_remove = []
        for dirpath, dirnames, _files in os.walk(root):
            for d in dirnames:
                if d in ("__pycache__", ".pytest_cache"):
                    p = Path(dirpath) / d
                    if str(p) not in before:
                        to_remove.append(p)
        for p in to_remove:
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _read_log_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _summarize_output(text, limit=4000):
    if not text or not text.strip():
        return "(no output)"
    text = text.strip()
    if len(text) > limit:
        return text[:limit] + "\n... (output too long, truncated)"
    return text


def _prune_done_test_jobs():
    """Clean up completed test jobs to avoid memory bloat during long runs."""
    with _TEST_JOBS_LOCK:
        for jid in [j for j, v in _TEST_JOBS.items() if v.get("status") == "done"]:
            _TEST_JOBS.pop(jid, None)


def _smoke_test_worker(root, job, entry, timeout, smoke):
    """Background thread: run one smoke test and fill in the result.

    Wrapped in a top-level guard so an unexpected error can never leave the job stuck in the
    "running" state (which would make test_result poll forever and stall the agent loop).
    """
    try:
        _smoke_test_run(root, job, entry, timeout, smoke)
    except Exception as e:
        try:
            proc = job.get("proc")
            if proc is not None:
                _terminate_process(proc)
        except Exception:
            pass
        job["proc"] = None
        job["result"] = "Smoke test failed due to an internal error: " + str(e)
        job["status"] = "done"


def _smoke_test_run(root, job, entry, timeout, smoke):
    """Run one smoke test and fill in the result."""
    exe = str(_venv_python_or_default(root))
    env = _command_env(root)
    cache_before = _snapshot_cache_dirs(root)

    entry_file = entry or _detect_project_entry(root)
    if not entry_file:
        job["status"] = "done"
        job["result"] = (
            "Smoke test failed: could not auto-detect the project entry file. Please first use list_files / read_file to inspect the project structure, "
            "then explicitly specify the entry file (e.g. main.py / app.py) via the entry parameter of smoke_test."
        )
        return

    entry_path = Path(root) / entry_file
    if not entry_path.is_file():
        job["status"] = "done"
        job["result"] = "Smoke test failed: entry file does not exist: " + entry_file
        return

    # Redirect output to a temp log file (system temp directory) to avoid the background thread blocking on pipe buffering; deleted afterwards.
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".smoke.log",
                                      encoding="utf-8", errors="replace", delete=False)
    log_path = tmp.name
    tmp.close()

    argv = [exe, str(entry_path)]
    if smoke:
        argv.append("--smoke")

    try:
        log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            argv,
            cwd=str(root),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if IS_WIN else 0,
        )
        job["proc"] = proc
    except Exception as e:
        try:
            log_fh.close()
        except Exception:
            pass
        _cleanup_new_caches(root, cache_before)
        try:
            os.remove(log_path)
        except OSError:
            pass
        job["status"] = "done"
        job["result"] = "Smoke test failed: unable to start the process: " + str(e)
        return

    deadline = time.monotonic() + timeout
    exited = False
    exit_code = None
    try:
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                exited = True
                exit_code = code
                break
            time.sleep(0.2)
    finally:
        if not exited:
            # Still running after the observation window -> treat as a long-running service; terminate and reclaim
            _terminate_process(proc)
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        try:
            log_fh.close()
        except Exception:
            pass
        job["proc"] = None

    output = _read_log_text(log_path)
    summary = _summarize_output(output)

    # Clean up temp files and newly created cache directories produced by the test
    try:
        os.remove(log_path)
    except OSError:
        pass
    _cleanup_new_caches(root, cache_before)

    if not exited:
        has_error = any(m in output for m in _SMOKE_ERROR_MARKERS)
        if has_error:
            job["result"] = (
                "Smoke test (warning): the program (%s) started and kept running for %d seconds without exiting, but the output contains error/exception signs; please confirm manually.\n"
                "Output summary:\n%s"
            ) % (entry_file, timeout, summary)
        else:
            job["result"] = (
                "Smoke test passed: the program (%s) started successfully and kept running for %d seconds without crashing (treated as a long-running service / background program).\n"
                "Output summary:\n%s"
            ) % (entry_file, timeout, summary)
    elif exit_code == 0:
        job["result"] = (
            "Smoke test passed: the program (%s) exited normally after running (exit code 0).\nOutput summary:\n%s"
        ) % (entry_file, summary)
    else:
        job["result"] = (
            "Smoke test failed: the program (%s) exited abnormally after startup (exit code %d).\nOutput:\n%s"
        ) % (entry_file, exit_code, summary)

    job["status"] = "done"


def _agent_smoke_test_start(root, entry, timeout, smoke):
    """Start a background smoke test and return test_id immediately."""
    _prune_done_test_jobs()
    test_id = uuid.uuid4().hex[:12]
    job = {"status": "running", "result": None, "proc": None}
    with _TEST_JOBS_LOCK:
        _TEST_JOBS[test_id] = job
    threading.Thread(
        target=_smoke_test_worker, args=(root, job, entry, timeout, smoke), daemon=True
    ).start()
    return "Smoke test started in the background (test_id=%s, observation window %d seconds%s). Use the test_result tool to query the result." % (
        test_id, timeout, ", --smoke argument appended" if smoke else "")


def _agent_smoke_test_result(test_id):
    """Query the background smoke-test result; prompt to query again later if still running."""
    with _TEST_JOBS_LOCK:
        job = _TEST_JOBS.get(test_id)
    if not job:
        return "Error: test_id does not exist: " + test_id
    if job["status"] == "running":
        return "Smoke test is still running in the background (test_id=%s); please query again later." % test_id
    return job["result"] or "(no result)"


def tool_smoke_test(root, args):
    """Agent tool entry: start a background smoke test."""
    entry = (args.get("entry") or "").strip()
    timeout = _num(args, "timeout", 10, int)
    timeout = max(1, min(timeout, 60))
    smoke = bool(args.get("smoke"))
    return _agent_smoke_test_start(root, entry, timeout, smoke)


def tool_test_result(args):
    """Agent tool entry: query a background smoke-test result."""
    test_id = (args.get("test_id") or "").strip()
    if not test_id:
        return "Error: missing test_id"
    return _agent_smoke_test_result(test_id)
