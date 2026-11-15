# -*- coding: utf-8 -*-
"""Application entry point and backwards-compatible re-export surface.

The service has been split into focused modules; `app.py` boots the server (after asking the
user which localhost port to use) and re-exports the public helpers that external callers
(including the acceptance tests) import from here.
"""
import os
import signal
import socket
import subprocess
import sys
import time

from openai import APIStatusError

import agent
import config  # noqa: F401
import session
from agent import (
    AGENT_TOOLS,
    _agent_execute_approved_tool,
    _agent_execute_command_tool,
    _agent_execute_tool,
    _cancel_run,
    _is_cancelled,
    _register_cancel,
    _unregister_cancel,
)
from common import (
    BASE_DIR,
    CREATE_NO_WINDOW,
    IS_WIN,
    _num,
    _truncate_for_model,
    build_usage,
    clients,
    estimate_tokens,
    get_agent_max_iters,
    get_current_python,
    get_edit_chunk_chars,
    get_read_limit,
    get_windows_integrity_level,
    set_agent_max_iters,
    set_current_python,
    set_edit_chunk_chars,
    set_read_limit,
    shrink_edit_chunk_chars,
    sse,
)
from server import Handler, install_shutdown_handlers, run_server
from tools.file_tools import _agent_rel, _safe_resolve
from tools.interactive_tools import _agent_ask_user_tool, _resolve_approval, _resolve_ask

# ------------------------- Startup port management -------------------------
# Feasible port range for a localhost-only service (mirrors config.py):
#   * 1-1023      : well-known ports (binding them usually requires administrator/root rights)
#   * 1024-49151  : registered user ports — recommended range
#   * 49152-65535 : dynamic/ephemeral ports — usable, but may collide with OS-assigned ones
PORT_MIN = config.PORT_MIN
PORT_RECOMMENDED_MAX = config.PORT_RECOMMENDED_MAX
PORT_MAX = config.PORT_MAX
PORT_DEFAULT = config.PORT_DEFAULT

# PID file: records this app instance so a later start can detect (and close) a stale instance,
# and so a clean shutdown can remove it. Lives next to the other runtime state (.memory.json etc.).
PID_FILE = BASE_DIR / ".app.pid"


def _port_is_busy(port, host=None):
    """True when `port` cannot be bound on localhost (already in use or not permitted)."""
    host = host or config.HOST
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # On Windows SO_REUSEADDR alone would let two processes bind the same port;
            # SO_EXCLUSIVEADDRUSE makes the check trustworthy.
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        sock.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _in_range(port):
    return PORT_MIN <= port <= PORT_MAX


def _port_from_argv(argv):
    """Read an explicit port from the command line (--port 8080 / --port=8080 / -p 8080)."""
    for i, arg in enumerate(argv):
        if arg in ("--port", "-p") and i + 1 < len(argv):
            try:
                return int(str(argv[i + 1]).strip())
            except (TypeError, ValueError):
                return None
        if arg.startswith("--port=") or arg.startswith("-p="):
            try:
                return int(arg.split("=", 1)[1].strip())
            except (TypeError, ValueError):
                return None
    return None


def _pids_listening_on(port, host=None):
    """Return the PIDs currently listening on `port` (best effort, cross-platform)."""
    host = host or config.HOST
    pids = []
    if IS_WIN:
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                timeout=15, creationflags=CREATE_NO_WINDOW,
            )
            for line in (out.stdout or "").splitlines():
                parts = line.split()
                # Expected columns: Proto Local_Address Foreign_Address State PID
                if len(parts) >= 5 and parts[0].upper() == "TCP":
                    local, state = parts[1], parts[3].upper()
                    if state == "LISTENING" and local.endswith(":" + str(port)):
                        try:
                            pids.append(int(parts[4]))
                        except ValueError:
                            pass
        except Exception:
            pass
    else:
        for cmd in (["lsof", "-ti", ":%d" % port], ["fuser", "-n", "tcp", str(port)]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                for token in (out.stdout or "").split():
                    token = token.strip()
                    if token.isdigit():
                        pids.append(int(token))
            except Exception:
                continue
    return pids


def _previous_instance_pid():
    """Return the PID recorded by a previous run of this app (None when absent/invalid)."""
    try:
        if PID_FILE.exists():
            raw = PID_FILE.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                return int(raw)
    except Exception:
        pass
    return None


def _kill_pids(pids):
    """Forcefully terminate the given PIDs (never our own process)."""
    self_pid = os.getpid()
    for pid in set(pids):
        if pid == self_pid:
            continue
        try:
            if IS_WIN:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, creationflags=CREATE_NO_WINDOW,
                )
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def _ensure_port_free(port):
    """Make sure `port` is free so the app can (re)open on it.

    When the port is taken — normally by a previous instance of this app that was not closed
    cleanly — the conflicting process is closed automatically and the port is re-opened on the
    same number (the app always serves on port 8888 by default).
    """
    if not _port_is_busy(port):
        return True
    pids = set(_pids_listening_on(port))
    previous = _previous_instance_pid()
    if previous and previous != os.getpid():
        pids.add(previous)
    if pids:
        print("Port %d is in use by process %s; closing it and reopening on %d ..."
              % (port, ", ".join(str(p) for p in sorted(pids)), port))
        _kill_pids(pids)
        for _ in range(20):  # up to ~5s for the OS to release the socket
            time.sleep(0.25)
            if not _port_is_busy(port):
                break
    return not _port_is_busy(port)


def _write_pid_file():
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def _remove_pid_file():
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass


def _resolve_start_port(argv):
    """Resolve the startup port: CLI argument, otherwise the configured default (8888)."""
    explicit = _port_from_argv(argv)
    if explicit is not None and _in_range(explicit):
        return explicit
    return PORT_DEFAULT


def main(argv=None):
    # Windows console/redirected output defaults to cp1252; printing non-ASCII characters raises
    # UnicodeEncodeError and fails startup. Force stdout/stderr to UTF-8 to avoid the service
    # crashing on non-ASCII characters in logs.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # On Windows, surface a Low-integrity token early: build/install tools that write into the
    # working directory (PyInstaller, git commit, pip install, ...) will fail with access denied
    # because a Low-integrity process cannot write to Medium-integrity files/folders.
    il = get_windows_integrity_level()
    if il and il[0] in ("Low", "Untrusted"):
        print("WARNING: this process runs at Windows integrity level '%s' (RID 0x%04X);"
              % (il[0], il[1]))
        print("         it cannot write to the working folder (Medium integrity). Launch the agent")
        print("         at Medium integrity, or point build tools at a Low-writable folder such as")
        print("         %%USERPROFILE%%\\AppData\\LocalLow.")

    session.prune_sessions(7)  # Prune Agent session files older than 7 days on startup.
    argv = list(sys.argv[1:] if argv is None else argv)
    port = _resolve_start_port(argv)

    # Requirement: serve on port 8888 (configurable via --port / LLM_CHAT_PORT). If the port is
    # taken, close the conflicting process and reopen on the same port instead of failing.
    _ensure_port_free(port)
    _write_pid_file()
    install_shutdown_handlers()

    try:
        for _attempt in range(3):
            if run_server(port):
                break
            # Bind still failed (rare race): free again and retry on the same port.
            print("Could not start on port %d; freeing it and retrying ..." % port)
            _ensure_port_free(port)
            time.sleep(0.5)
    finally:
        _remove_pid_file()


if __name__ == "__main__":
    main()
