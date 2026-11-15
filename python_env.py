# -*- coding: utf-8 -*-
"""Python interpreter discovery, virtual-environment helpers, and subprocess termination."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from common import CREATE_NO_WINDOW, IS_WIN, get_current_python


def _py_version(exe):
    """Best-effort retrieval of the interpreter version; returns None on failure."""
    try:
        res = subprocess.run(
            [exe, "--version"], capture_output=True, text=True,
            timeout=5, creationflags=CREATE_NO_WINDOW,
        )
        raw = (res.stdout or res.stderr).strip()
        return raw[7:] if raw.startswith("Python ") else (raw or None)
    except Exception:
        return None


def _venv_python(venv_dir):
    win = venv_dir / "Scripts" / "python.exe"
    if win.exists():
        return win
    posix = venv_dir / "bin" / "python"
    if posix.exists():
        return posix
    return None


def _venv_name(interpreter):
    """Infer the virtual environment name (directory name) from the interpreter path; returns None if not inside a virtual environment."""
    try:
        p = Path(interpreter).resolve()
    except Exception:
        return None
    if IS_WIN:
        if p.name.lower() == "python.exe" and p.parent.name.lower() == "scripts":
            return p.parent.parent.name
    else:
        if p.name == "python" and p.parent.name == "bin":
            return p.parent.parent.name
    return None


def scan_venv_pythons(folder):
    """Scan folder, its ancestor directories, and one level of child directories for virtual-environment Pythons."""
    found = {}
    try:
        p = Path(folder).expanduser().resolve()
    except Exception:
        return found
    for root in [p] + list(p.parents):
        for name in (".venv", "venv", "env"):
            exe = _venv_python(root / name)
            if exe:
                found[str(exe)] = str(root / name)
    try:
        children = list(p.iterdir())
    except OSError:
        children = []
    for child in children:
        try:
            if child.is_dir() and child.name.lower() in (".venv", "venv", "env"):
                exe = _venv_python(child)
                if exe:
                    found[str(exe)] = str(child)
        except OSError:
            continue
    return found


def discover_python_envs(folder):
    """Discover available Python interpreters: current interpreter (sys.executable) / py launcher / PATH / venv / conda."""
    candidates = []
    seen = set()

    def add(path, kind, detail=""):
        if not path:
            return
        try:
            path = str(Path(path).resolve())
        except Exception:
            return
        if path in seen or not Path(path).exists():
            return
        seen.add(path)
        candidates.append({"path": path, "kind": kind, "detail": detail})

    # 0) Current interpreter (default = the interpreter running this service = the one selected by the user in the IDE), highest priority;
    #    after the user selects a new interpreter in the UI it has been updated via /api/python/current, so this follows the latest selection.
    add(get_current_python(), "current")
    # The interpreter running this service is also listed as a normal system interpreter (still selectable when different from the current interpreter)
    add(sys.executable, "system")

    # 1) Windows py launcher
    py_launcher = shutil.which("py")
    if py_launcher:
        try:
            res = subprocess.run(
                [py_launcher, "-0p"], capture_output=True, text=True,
                timeout=10, creationflags=CREATE_NO_WINDOW,
            )
            for line in (res.stdout or "").splitlines():
                m = re.match(r"\s*-V:(\S+)\s*\*?\s+(.+)$", line.strip())
                if m:
                    add(m.group(2).strip(), "system", "Python " + m.group(1))
        except Exception:
            pass

    # 2) python / python3 on PATH
    for name in ("python", "python3"):
        exe = shutil.which(name)
        if exe:
            add(exe, "system")

    # 3) Virtual environments
    for exe, venv_dir in scan_venv_pythons(folder).items():
        add(exe, "venv", Path(venv_dir).name)

    # 4) conda environments
    conda = shutil.which("conda")
    if conda:
        try:
            res = subprocess.run(
                [conda, "env", "list", "--json"], capture_output=True, text=True,
                timeout=8, creationflags=CREATE_NO_WINDOW,
            )
            for env_dir in (json.loads(res.stdout or "{}").get("envs") or []):
                exe = Path(env_dir) / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")
                add(str(exe), "conda", Path(env_dir).name)
        except Exception:
            pass

    return candidates


def _terminate_process(proc):
    """Terminate the process and its children."""
    try:
        if IS_WIN:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _venv_activate(interpreter):
    """Infer the virtual environment activation script from the interpreter path; returns None if not found."""
    try:
        p = Path(interpreter)
    except Exception:
        return None
    if IS_WIN:
        if p.name.lower() == "python.exe" and p.parent.name.lower() == "scripts":
            act = p.parent / "activate.bat"
            return str(act) if act.exists() else None
    else:
        if p.name == "python" and p.parent.name == "bin":
            act = p.parent / "activate"
            return str(act) if act.exists() else None
    return None


def _project_venv(root):
    """Return the virtual environment directory (.venv/venv/env) under the working folder, or None if none exists."""
    for name in (".venv", "venv", "env"):
        d = Path(root) / name
        if _venv_python(d):
            return d
    return None


def _venv_python_or_default(root):
    """Prefer returning the project virtual environment's Python, falling back to the current interpreter if none exists."""
    venv = _project_venv(root)
    if venv:
        py = _venv_python(venv)
        if py:
            return py
    return Path(get_current_python())


def _command_env(root):
    """Build the command execution environment: if the project has a venv, activate it (prepend Scripts/bin to PATH, set VIRTUAL_ENV)."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    venv = _project_venv(root)
    if venv:
        env["VIRTUAL_ENV"] = str(venv)
        scripts = venv / ("Scripts" if IS_WIN else "bin")
        env["PATH"] = str(scripts) + os.pathsep + env.get("PATH", "")
    return env
