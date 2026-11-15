# -*- coding: utf-8 -*-
"""Shell/command tools (run_command, create_venv, install_packages) + dangerous-command checks."""
import os
import re
import subprocess
from pathlib import Path

import config
from common import CREATE_NO_WINDOW, IS_WIN, _num, _truncate_for_model, get_current_python
from python_env import _command_env, _venv_python_or_default
from tools.file_tools import _safe_resolve

# Command-type tools for the Agent: create_venv / install_packages / run_command / run_tests.
COMMAND_TOOL_NAMES = {"create_venv", "install_packages", "run_command", "run_tests"}


def _matches_dangerous(command: str) -> bool:
    """Return True when `command` matches any DANGEROUS_COMMAND_PATTERNS entry."""
    for pattern in config.DANGEROUS_COMMAND_PATTERNS:
        try:
            if re.search(pattern, command or ""):
                return True
        except re.error:
            continue
    return False


def _matches_hard_block(command: str) -> bool:
    """Return True when `command` matches any HARD_BLOCK_PATTERNS entry."""
    for pattern in config.HARD_BLOCK_PATTERNS:
        try:
            if re.search(pattern, command or ""):
                return True
        except re.error:
            continue
    return False


def _run_shell_command(command, cwd, timeout=180, env=None):
    """Execute a shell command, returning (returncode, merged output text)."""
    try:
        proc = subprocess.run(
            command, cwd=str(cwd), shell=True,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env or os.environ.copy(),
            creationflags=CREATE_NO_WINDOW if IS_WIN else 0,
        )
        out = (proc.stdout or "").rstrip()
        err = (proc.stderr or "").rstrip()
        merged = out
        if err:
            merged = (merged + "\n" if merged else "") + err
        # Cap output before it reaches the model; keep the tail (stderr errors sit at the bottom).
        merged = _truncate_for_model(merged, config.COMMAND_OUTPUT_MAX_BYTES, keep="tail")
        return proc.returncode, merged
    except subprocess.TimeoutExpired:
        return -1, "Command timed out (exceeded %d seconds)" % timeout
    except Exception as e:
        return -1, "Command execution failed: " + str(e)


def _format_command_result(code, out):
    if code == 0:
        return "Command executed successfully (exit code 0):\n%s" % (out or "(no output)")
    return "Command execution failed (exit code %d):\n%s" % (code, out or "(no output)")


def _format_test_result(code, out):
    """Format a pytest run: pytest uses exit code 1 for test failures, 5 for 'no tests collected'."""
    if code == 0:
        return "Tests passed (exit code 0):\n%s" % (out or "(no output)")
    if code == 5:
        return "Tests could not run (exit code 5): no tests were collected.\n%s" % (out or "(no output)")
    if code in (1, 2, 3):
        return "Tests failed (exit code %d):\n%s" % (code, out or "(no output)")
    return "Tests error (exit code %d):\n%s" % (code, out or "(no output)")


def execute_command_tool(name, args, root, wfile, mode="ask", pending_ids=None):
    """Execute a command-type tool: ask the user to approve via a dialog, then execute after approval.

    In "agent" mode, every command (including dangerous and hard-blocked ones) runs
    immediately without a dialog.
    """
    if name == "create_venv":
        rel = (args.get("path") or ".venv").strip()
        target = _safe_resolve(root, rel)
        interpreter = (args.get("python") or "").strip() or str(get_current_python())
        command = '"%s" -m venv "%s"' % (interpreter, str(target))
        description = "Create a Python virtual environment in the working folder (%s)" % rel
        timeout = 180
    elif name == "install_packages":
        pkgs = args.get("packages") or ""
        if isinstance(pkgs, list):
            pkgs = " ".join(str(p) for p in pkgs)
        pkgs = (pkgs or "").strip()
        pip = str(_venv_python_or_default(root))
        if pkgs:
            command = '"%s" -m pip install %s' % (pip, pkgs)
        elif (root / "requirements.txt").is_file():
            command = '"%s" -m pip install -r requirements.txt' % pip
        else:
            return "Error: no package names specified and no requirements.txt in the project"
        description = "Install dependency packages in the project's Python environment"
        timeout = max(60, min(_num(args, "timeout", 600, int), 3600))
    elif name == "run_command":
        command = (args.get("command") or "").strip()
        if not command:
            return "Error: missing command"
        # Hard-block dangerous commands before showing any dialog (released in agent mode).
        if mode != "agent" and _matches_hard_block(command):
            return "Error: this command matches a hard-block pattern and will never be executed."
        description = "Execute a shell command in the working folder"
        timeout = max(1, min(_num(args, "timeout", 180, int), 3600))
    elif name == "run_tests":
        path = (args.get("path") or "").strip()
        keyword = (args.get("keyword") or "").strip().replace('"', "")
        timeout = max(1, min(_num(args, "timeout", 300, int), 1800))
        pip = str(_venv_python_or_default(root))
        # Detect pytest before showing the dialog so the model gets an actionable error instead
        # of an opaque "command failed" when pytest is missing.
        vcode, _vout = _run_shell_command('"%s" -m pytest --version' % pip, root, timeout=60, env=_command_env(root))
        if vcode != 0:
            return ("Error: pytest is not available in the project's Python environment (%s). "
                    "Use install_packages to install pytest, or use run_command to run unittest/scripts manually." % pip)
        parts = ['"%s" -m pytest -q' % pip]
        if keyword:
            parts.append('-k "%s"' % keyword)
        if path:
            target = _safe_resolve(root, path)
            if not target.exists():
                return "Error: test path does not exist: " + path
            parts.append('"%s"' % str(target))
        command = " ".join(parts)
        description = "Run the project's tests (pytest)" + ((" for: " + path) if path else "")
    else:
        return "Error: unknown command tool " + str(name)

    # In agent mode, commands run immediately without any approval or block checks.
    if mode == "agent":
        code, out = _run_shell_command(command, root, timeout=timeout, env=_command_env(root))
        return _format_test_result(code, out) if name == "run_tests" else _format_command_result(code, out)

    # 1) Approval dialog: send an SSE event and block until the user decides.
    #    _request_approval re-checks run_command against dangerous / hard-block patterns.
    from tools.interactive_tools import _request_approval
    status, final_command = _request_approval(wfile, command, description, name, name, pending_ids=pending_ids)
    if status == "hard_blocked":
        return "Error: this command matches a hard-block pattern and will never be executed."
    if status != "approved":
        return "The user rejected the command; not executed: %s" % command

    # 2) Execute after approval
    code, out = _run_shell_command(final_command, root, timeout=timeout, env=_command_env(root))
    return _format_test_result(code, out) if name == "run_tests" else _format_command_result(code, out)
