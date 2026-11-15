# -*- coding: utf-8 -*-
"""Agent lint tool: syntax + optional ruff/flake8/pyflakes diagnostics with a linter cache."""
import os
import threading
import time
from pathlib import Path

import config
from common import _truncate_for_model
from python_env import _command_env, _venv_python_or_default
from tools.file_tools import _agent_rel, _safe_resolve
from tools.shell_tools import _run_shell_command

_LINT_SKIP_DIRS = {
    "__pycache__", ".pytest_cache", ".git", ".venv", "venv", "env",
    "node_modules", ".idea", ".vscode", ".checkpoints", "dist", "build",
}

# Detected-linter cache: (interpreter_path, str(root)) -> (monotonic timestamp, linter or None).
# Avoids re-running `ruff/flake8/pyflakes --version` on every lint call.
_LINTER_CACHE = {}
_LINTER_CACHE_LOCK = threading.Lock()
_LINTER_CACHE_TTL = 300  # seconds


def _detect_linter(root):
    """Detect the best available linter (ruff -> flake8 -> pyflakes), cached for 5 minutes."""
    exe = str(_venv_python_or_default(root))
    key = (exe, str(root))
    with _LINTER_CACHE_LOCK:
        item = _LINTER_CACHE.get(key)
        if item is not None and time.monotonic() - item[0] < _LINTER_CACHE_TTL:
            return item[1]

    env = _command_env(root)
    linter = None
    for module in ("ruff", "flake8", "pyflakes"):
        vcode, _ = _run_shell_command('"%s" -m %s --version' % (exe, module), root, timeout=30, env=env)
        if vcode == 0:
            linter = module
            break
    with _LINTER_CACHE_LOCK:
        _LINTER_CACHE[key] = (time.monotonic(), linter)
    return linter


def tool_lint(root, path):
    """Agent tool: check syntax/lint errors in Python files under the given path (read-only)."""
    target = _safe_resolve(root, path or "")
    if not target.exists():
        return "Error: path does not exist: " + (path or "(root)")

    # Collect Python source files, skipping generated / virtual-environment directories
    pyfiles = []
    if target.is_file():
        if target.suffix.lower() != ".py":
            return "Error: not a Python file: " + (path or "")
        pyfiles = [target]
    else:
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = [d for d in dirnames if d not in _LINT_SKIP_DIRS]
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    pyfiles.append(Path(dirpath) / fn)

    if not pyfiles:
        return "No Python (.py) files found to lint under: %s" % (path or "the selected folder")

    # 1) Built-in syntax check (always runs; catches the errors most likely to break testing)
    syntax_errors = []
    for f in pyfiles:
        rel = _agent_rel(root, f)
        try:
            compile(f.read_text(encoding="utf-8", errors="replace"), str(f), "exec")
        except SyntaxError as e:
            syntax_errors.append("%s: line %s: SyntaxError: %s" % (rel, e.lineno or "?", e.msg or str(e)))
        except OSError as e:
            syntax_errors.append("%s: cannot read file: %s" % (rel, e))

    # 2) Prefer a real linter (ruff -> flake8 -> pyflakes) for richer diagnostics when available
    exe = str(_venv_python_or_default(root))
    env = _command_env(root)
    linter = _detect_linter(root)

    linter_note = ""
    lcode = None
    linter_output = ""
    if linter == "ruff":
        # ruff excludes virtual environments / cache directories by default
        rel_target = _agent_rel(root, target) or "."
        lcode, lout = _run_shell_command('"%s" -m ruff check "%s"' % (exe, rel_target), root, timeout=180, env=env)
        linter_output = lout.strip()
    elif linter in ("flake8", "pyflakes"):
        # pass individual files so virtual environments / cache dirs are never scanned
        files_arg = " ".join('"%s"' % _agent_rel(root, f) for f in pyfiles[:200])
        truncated = len(pyfiles) > 200
        if linter == "flake8":
            # only report "definitely a bug" categories, not style (E501 line length etc.)
            lcmd = '"%s" -m flake8 --select=E9,F63,F7,F82 %s' % (exe, files_arg)
        else:
            lcmd = '"%s" -m pyflakes %s' % (exe, files_arg)
        lcode, lout = _run_shell_command(lcmd, root, timeout=180, env=env)
        linter_output = lout.strip()
        if truncated:
            linter_output = (linter_output + ("\n" if linter_output else "") +
                             "(note: only the first 200 files were passed to %s)" % linter).strip()

    if linter:
        if lcode == 0:
            linter_note = "\nLinter (%s): no issues found." % linter
        elif lcode > 0:
            linter_note = "\nLinter (%s) reported issues:\n%s" % (linter, linter_output or "(none)")
        else:
            linter_note = "\nLinter (%s) failed to run: %s" % (linter, linter_output or "unknown error")
    else:
        linter_note = "\n(No external linter available: ruff/flake8/pyflakes not installed; only the built-in syntax check was performed.)"

    lines = ["Lint check completed: %d Python file(s) scanned." % len(pyfiles)]
    if syntax_errors:
        lines.append("Syntax errors found (%d):" % len(syntax_errors))
        lines.extend("  - " + s for s in syntax_errors)
    else:
        lines.append("Syntax check: OK (no syntax errors).")
    lines.append(linter_note)
    return _truncate_for_model("\n".join(lines), config.LINT_OUTPUT_MAX_BYTES, keep="head")
