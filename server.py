# -*- coding: utf-8 -*-
"""HTTP server: routing, SSE streaming, folder browsing, terminal sessions, and all endpoints."""
import codecs
import concurrent.futures
import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent
import config
from agent import _cancel_run
from checkpoints import _create_checkpoint, _delete_checkpoint, _list_checkpoints, _restore_checkpoint
from session import load_session as _load_session
from tools.db_tools import _db_query, _db_schema_tables
from tools.interactive_tools import _resolve_approval, _resolve_ask
from common import (
    BASE_DIR,
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    INDEX_FILE,
    IS_WIN,
    MAX_BODY_BYTES,
    _GEN_SLOTS,
    _compress_history,
    _consume_stream,
    _create_compatible,
    _emit_usage,
    _fit_context,
    _num,
    build_client,
    build_usage,
    clients,
    estimate_tokens,
    get_agent_max_iters,
    get_continue_enabled,
    get_continue_max_rounds,
    get_current_python,
    get_edit_chunk_chars,
    get_read_limit,
    get_remembered_folder,
    set_agent_max_iters,
    set_backend_api_key,
    set_continue_enabled,
    set_continue_max_rounds,
    set_current_python,
    set_edit_chunk_chars,
    set_read_limit,
    set_remembered_folder,
    sse,
)
from python_env import _py_version, _terminate_process, _venv_activate, _venv_name, discover_python_envs
from tools import db_tools, interactive_tools


# ------------------------- Folder browsing -------------------------
def list_folder(path_str):
    """List folder contents, returning (absolute path, parent directory, entry list)."""
    p = Path(path_str).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError("Path does not exist: " + path_str)
    if not p.is_dir():
        raise NotADirectoryError("Not a folder: " + path_str)
    try:
        children = list(p.iterdir())
    except PermissionError:
        raise PermissionError("No permission to access the folder: " + path_str)
    entries = []
    for child in children:
        try:
            stat = child.stat()
        except OSError:
            continue  # Skip entries that cannot be read (e.g. broken symlinks)
        is_dir = child.is_dir()
        entries.append({
            "name": child.name,
            "is_dir": is_dir,
            "size": stat.st_size if not is_dir else 0,
            "mtime": stat.st_mtime,
        })
    entries.sort(key=lambda e: (not e["is_dir"], -(e["mtime"] or 0)))
    parent = str(p.parent) if p.parent != p else None
    return str(p), parent, entries


# ------------------------- Windows native folder selection -------------------------
def native_folder_picker():
    """Open the Windows default "Select Folder" dialog and return the selected path.

    Returns None when cancelled; raises RuntimeError when no approach is available (non-Windows or no GUI).
    Prefers tkinter (bundled with Python; shows the modern system folder picker),
    falling back to PowerShell's FolderBrowserDialog.
    """
    if not IS_WIN:
        raise RuntimeError("Native folder selection is only supported on Windows")

    # 1) tkinter: system modern "Select Folder" dialog
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        try:
            path = filedialog.askdirectory(title="Select working folder", parent=root)
        finally:
            try:
                root.destroy()
            except Exception:
                pass
        if path:
            return str(path)
        return None
    except Exception:
        pass

    # 2) Fallback: PowerShell FolderBrowserDialog (classic system folder browser)
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "$owner = New-Object System.Windows.Forms.Form\n"
        "$owner.TopMost = $true\n"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog\n"
        "$d.Description = 'Select working folder'\n"
        "$d.ShowNewFolderButton = $true\n"
        "if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {\n"
        "    [Console]::Out.Write($d.SelectedPath)\n"
        "}\n"
        "$owner.Dispose()\n"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True, text=True, timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        raise RuntimeError("Unable to open the folder selection dialog")
    path = (res.stdout or "").strip()
    return path or None


# ------------------------- Python run support -------------------------
_RUNNING = {}
_RUNNING_LOCK = threading.Lock()

# Registry of interactive terminal sessions: term_id -> {"proc", "buf", "lock", "exited", "code"}
_TERM = {}
_TERM_LOCK = threading.Lock()


# ------------------------- Interactive terminal support -------------------------
def _term_reader(term_id, term):
    """Background thread: continuously read the terminal process output and write it into the buffer (capturing the prompt in real time, byte by byte)."""
    proc = term["proc"]
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while True:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            text = decoder.decode(chunk)
            with term["lock"]:
                term["buf"].append(text)
    except Exception:
        pass
    finally:
        try:
            proc.wait()
        except Exception:
            pass
        with term["lock"]:
            term["exited"] = True
            term["code"] = proc.returncode
        try:
            proc.stdout.close()
        except Exception:
            pass


_win_banner_cache = None
_win_banner_lock = threading.Lock()


def _win_banner():
    """Windows cmd.exe startup banner (version + copyright); returns an empty string on failure."""
    global _win_banner_cache
    with _win_banner_lock:
        if _win_banner_cache is None:
            banner = ""
            try:
                res = subprocess.run(
                    ["cmd.exe", "/C", "ver"], capture_output=True, text=True,
                    timeout=5, creationflags=CREATE_NO_WINDOW,
                )
                ver = (res.stdout or "").strip()
                if ver:
                    banner = "%s\r\n(c) Microsoft Corporation. All rights reserved.\r\n\r\n" % ver
            except Exception:
                banner = ""
            _win_banner_cache = banner
        return _win_banner_cache


def _prune_exited_terms():
    """Clean up exited terminal sessions to avoid memory bloat during long runs."""
    with _TERM_LOCK:
        for tid in [t for t, v in _TERM.items() if v.get("exited")]:
            _TERM.pop(tid, None)


def _start_term(interpreter, cwd):
    """Start an interactive command prompt (cmd.exe on Windows, with the virtual environment activated), returning term_id; returns None on failure."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["TERM"] = "dumb"
    env["PYTHONSTARTUP"] = ""

    workdir = str(BASE_DIR)
    if cwd:
        try:
            workdir = str(Path(cwd).resolve())
        except Exception:
            pass

    banner = ""
    if IS_WIN:
        cmdline = [os.environ.get("COMSPEC", "cmd.exe")]
        activate = _venv_activate(interpreter)
        # Force cmd to output UTF-8 (otherwise, with no console under CREATE_NO_WINDOW, it falls back to OEM 437 and non-ASCII paths become ?)
        command = "chcp 65001 >nul"
        if activate:
            command += " && " + activate
        cmdline += ["/K", command]
        banner = _win_banner()
    else:
        cmdline = [interpreter, "-c", "import code; code.interact(banner='', exitmsg='')"]

    try:
        proc = subprocess.Popen(
            cmdline,
            cwd=workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env,
            creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if IS_WIN else 0,
        )
    except Exception:
        return None

    buf = []
    if banner:
        buf.append(banner)
    term_id = uuid.uuid4().hex
    term = {"proc": proc, "buf": buf, "lock": threading.Lock(), "exited": False, "code": None}
    with _TERM_LOCK:
        _TERM[term_id] = term
    threading.Thread(target=_term_reader, args=(term_id, term), daemon=True).start()
    return term_id


class Handler(BaseHTTPRequestHandler):
    server_version = "LLMChat/1.0"

    def handle(self):
        # Don't print a stack trace when the client disconnects abruptly (e.g. stops generation)
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass

    def log_message(self, fmt, *args):  # Concise logging (avoid address_string's reverse DNS lookup)
        print("[%s] %s" % (self.client_address[0], fmt % args))

    # --- Static page / API ---
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            if not INDEX_FILE.exists():
                self.send_error(404, "index.html not found")
                return
            body = INDEX_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/app-icon.svg":
            self._serve_static(BASE_DIR / "app-icon.svg", "image/svg+xml")
        elif self.path == "/whale-logo.svg":
            self._serve_static(BASE_DIR / "whale-logo.svg", "image/svg+xml")
        elif self.path.startswith("/api/files"):
            self.handle_api_files()
        elif self.path.startswith("/api/file/read"):
            self.handle_file_read()
        elif self.path.startswith("/api/db/schema"):
            self.handle_db_schema()
        elif self.path.startswith("/api/checkpoints"):
            self.handle_checkpoint_list()
        elif self.path.startswith("/api/python/envs"):
            self.handle_python_envs()
        elif self.path.startswith("/api/python/inspect"):
            self.handle_python_inspect()
        elif self.path.startswith("/api/terminal/output"):
            self.handle_terminal_output()
        elif self.path.startswith("/api/settings"):
            self.handle_settings_get()
        elif self.path == "/api/balance":
            self.handle_balance_get()
        elif self.path == "/api/context":
            self.handle_context_get()
        elif self.path.startswith("/api/memory"):
            self.handle_memory_get()
        elif self.path.startswith("/api/session/"):
            self.handle_session_get()
        else:
            self.send_error(404)

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path, content_type):
        """Serve a static file (e.g. the app icon) with a simple 404 on failure."""
        try:
            p = Path(path)
            if not p.is_file():
                self.send_error(404, "Not found")
                return
            body = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.send_error(404, "Not found")

    def handle_api_files(self):
        """GET /api/files?path=<folder path> — list folder contents."""
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path_str = (qs.get("path") or [""])[0].strip() or str(BASE_DIR)
        try:
            abs_path, parent, entries = list_folder(path_str)
        except FileNotFoundError as e:
            self._send_json(404, {"ok": False, "error": str(e)})
            return
        except NotADirectoryError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        except PermissionError as e:
            self._send_json(403, {"ok": False, "error": str(e)})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {
            "ok": True,
            "path": abs_path,
            "parent": parent,
            "entries": entries,
        })

    # --- POST dispatch ---
    def do_POST(self):
        if self.path == "/chat":
            self._handle_chat()
        elif self.path == "/api/folder/select":
            self.handle_folder_select()
        elif self.path == "/api/file/write":
            self.handle_file_write()
        elif self.path == "/api/db/query":
            self.handle_db_query()
        elif self.path == "/api/checkpoint/create":
            self.handle_checkpoint_create()
        elif self.path == "/api/checkpoint/restore":
            self.handle_checkpoint_restore()
        elif self.path == "/api/checkpoint/delete":
            self.handle_checkpoint_delete()
        elif self.path == "/api/python/current":
            self.handle_python_set_current()
        elif self.path == "/api/python/run":
            self.handle_python_run()
        elif self.path == "/api/python/stop":
            self.handle_python_stop()
        elif self.path == "/api/terminal/start":
            self.handle_terminal_start()
        elif self.path == "/api/terminal/input":
            self.handle_terminal_input()
        elif self.path == "/api/terminal/stop":
            self.handle_terminal_stop()
        elif self.path == "/api/command/approval":
            self.handle_command_approval()
        elif self.path == "/api/ask/answer":
            self.handle_ask_answer()
        elif self.path == "/api/agent/stop":
            self.handle_agent_stop()
        elif self.path == "/api/settings":
            self.handle_settings_post()
        elif self.path == "/api/memory":
            self.handle_memory_post()
        elif self.path == "/api/compact":
            self.handle_compact()
        else:
            self.send_error(404)

    def _read_json_body(self):
        """Read and parse the JSON request body; returns (None, error message) on error."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY_BYTES:
                return None, "payload too large"
            return json.loads(self.rfile.read(length) or b"{}"), None
        except Exception as e:
            return None, "bad json: %s" % e

    # --- /chat streaming endpoint ---
    def _handle_chat(self):
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # Critical: must be close. BaseHTTPRequestHandler sets close_connection to False when send_header("Connection","keep-alive")
        # is called, and an SSE response has no Content-Length, causing the connection to never end, so the browser-side
        # reader.read() never sees done and the whole frontend hangs in the streaming state.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            self._stream(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Client already disconnected, nothing more to write
        except Exception as e:  # Fallback error event
            try:
                sse(self.wfile, {"type": "error", "message": str(e)})
            except Exception:
                pass

    def handle_folder_select(self):
        """POST /api/folder/select — open the Windows native folder selection dialog."""
        try:
            path = native_folder_picker()
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        if path:
            self._send_json(200, {"ok": True, "path": path})
        else:
            self._send_json(200, {"ok": False, "cancelled": True})

    # --- File read / save ---
    def handle_file_read(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path_str = (qs.get("path") or [""])[0].strip()
        if not path_str:
            self._send_json(400, {"ok": False, "error": "Missing path parameter"})
            return
        p = Path(path_str).expanduser().resolve()
        if not p.exists():
            self._send_json(404, {"ok": False, "error": "File does not exist: " + path_str})
            return
        if not p.is_file():
            self._send_json(400, {"ok": False, "error": "Not a file: " + path_str})
            return
        try:
            if p.stat().st_size > get_read_limit():
                self._send_json(413, {"ok": False, "error": "File too large (exceeds %d MB), cannot open in the editor" % (get_read_limit() // (1024 * 1024))})
                return
        except OSError:
            pass
        try:
            raw = p.read_bytes()
        except PermissionError as e:
            self._send_json(403, {"ok": False, "error": "No permission to read: " + str(e)})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        if b"\x00" in raw:
            self._send_json(400, {"ok": False, "error": "Binary file, cannot open in the editor"})
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        self._send_json(200, {"ok": True, "path": str(p), "name": p.name, "content": text})

    def handle_file_write(self):
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        path_str = (payload.get("path") or "").strip()
        content = payload.get("content") or ""
        if not path_str:
            self._send_json(400, {"ok": False, "error": "Missing path"})
            return
        p = Path(path_str).expanduser().resolve()
        try:
            p.write_text(content, encoding="utf-8")
        except PermissionError as e:
            self._send_json(403, {"ok": False, "error": "No permission to write: " + str(e)})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True, "path": str(p)})

    # --- SQLite database viewer ---
    def handle_db_schema(self):
        """GET /api/db/schema?path=<database path> — return tables/views and their column structures."""
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path_str = (qs.get("path") or [""])[0].strip()
        if not path_str:
            self._send_json(400, {"ok": False, "error": "Missing path parameter"})
            return
        try:
            tables = _db_schema_tables(path_str)
        except FileNotFoundError as e:
            self._send_json(404, {"ok": False, "error": str(e)})
            return
        except sqlite3.DatabaseError as e:
            self._send_json(400, {"ok": False, "error": "Unable to open database: " + str(e)})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True, "tables": tables})

    def handle_db_query(self):
        """POST /api/db/query — execute read-only SQL, returning columns and rows (with a row limit)."""
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        path_str = (payload.get("path") or "").strip()
        sql = payload.get("sql") or ""
        if not path_str:
            self._send_json(400, {"ok": False, "error": "Missing path"})
            return
        try:
            columns, rows, truncated = _db_query(path_str, sql)
        except FileNotFoundError as e:
            self._send_json(404, {"ok": False, "error": str(e)})
            return
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        except sqlite3.DatabaseError as e:
            self._send_json(400, {"ok": False, "error": "Query error: " + str(e)})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {
            "ok": True, "columns": columns, "rows": rows, "truncated": bool(truncated),
        })

    # --- Server settings (file read limit / Agent max attempts) ---
    def handle_settings_get(self):
        """GET /api/settings — return the current server-adjustable settings."""
        try:
            import getpass
            username = getpass.getuser() or ""
        except Exception:
            username = ""
        self._send_json(200, {
            "ok": True,
            "max_read_bytes": get_read_limit(),
            "agent_max_iters": get_agent_max_iters(),
            "edit_chunk_chars": get_edit_chunk_chars(),
            "continue_enabled": get_continue_enabled(),
            "continue_max_rounds": get_continue_max_rounds(),
            "api_keys": {name: bool(cfg.get("api_key")) for name, cfg in config.BACKENDS.items()},
            "pricing": config.DEEPSEEK_PRICING,
            "username": username,
        })

    def handle_context_get(self):
        """GET /api/context — return the static context breakdown (system prompt + tool
        definitions + context window) so the frontend can show a meaningful breakdown even
        before a conversation starts. Conversation/tool-call parts stream live via `context` SSE."""
        cfg = config.BACKENDS.get("deepseek") or {}
        window = int(cfg.get("context_window") or config.DEFAULT_CONTEXT_WINDOW)
        try:
            system_tokens = estimate_tokens(config.AGENT_SYSTEM_PROMPT)
            definitions_tokens = estimate_tokens(json.dumps(agent.AGENT_TOOLS, ensure_ascii=False))
        except Exception:
            system_tokens = 0
            definitions_tokens = 0
        self._send_json(200, {
            "ok": True,
            "system": system_tokens,
            "definitions": definitions_tokens,
            "window": window,
        })

    def handle_balance_get(self):
        """GET /api/balance — query the DeepSeek account balance for the configured API key.

        Uses DeepSeek's official `GET /user/balance` endpoint (https://api.deepseek.com/user/balance),
        authenticated with the same API key used for chat. Returns the raw balance payload on success.
        """
        cfg = config.BACKENDS.get("deepseek") or {}
        key = (cfg.get("api_key") or "").strip() or (config.DEEPSEEK_API_KEY or "").strip()
        if not key:
            self._send_json(400, {"ok": False, "error": "API key is not set"})
            return
        base_url = (cfg.get("base_url") or config.DEEPSEEK_BASE_URL or "https://api.deepseek.com").rstrip("/")
        url = base_url + "/user/balance"
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            self._send_json(400, {"ok": False, "error": "Balance request failed (HTTP %s): %s" % (e.code, detail)})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": "Could not reach DeepSeek: %s" % e})
            return
        try:
            data = json.loads(body or "{}")
        except Exception:
            self._send_json(500, {"ok": False, "error": "Unexpected response from DeepSeek"})
            return
        self._send_json(200, {"ok": True, "balance": data})

    def handle_settings_post(self):
        """POST /api/settings — update server-adjustable settings (e.g. max_read_bytes / agent_max_iters)."""
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return

        # Support updating multiple fields at once; validate and update only the fields present in the request
        if "max_read_bytes" in payload:
            value = _num(payload, "max_read_bytes", None, int)
            if value is None or value < 1024:
                self._send_json(400, {"ok": False, "error": "max_read_bytes must be an integer no smaller than 1KB"})
                return
            set_read_limit(value)
        if "agent_max_iters" in payload:
            value = _num(payload, "agent_max_iters", None, int)
            if value is None or value < 1 or value > config.AGENT_MAX_ITERS_CAP:
                self._send_json(400, {
                    "ok": False,
                    "error": "agent_max_iters must be an integer between 1 and %d" % config.AGENT_MAX_ITERS_CAP,
                })
                return
            set_agent_max_iters(value)
        if "edit_chunk_chars" in payload:
            # Size of one write/edit piece (characters) used for piecewise large-file editing.
            value = _num(payload, "edit_chunk_chars", None, int)
            if value is None:
                self._send_json(400, {"ok": False, "error": "edit_chunk_chars must be an integer"})
                return
            set_edit_chunk_chars(value)
        if "continue_enabled" in payload:
            set_continue_enabled(payload.get("continue_enabled"))
        if "continue_max_rounds" in payload:
            value = _num(payload, "continue_max_rounds", None, int)
            if value is None or value < 1 or value > 20:
                self._send_json(400, {"ok": False, "error": "continue_max_rounds must be an integer between 1 and 20"})
                return
            set_continue_max_rounds(value)
        if "api_key" in payload:
            backend_name = (payload.get("backend") or "").strip()
            if backend_name not in config.BACKENDS:
                self._send_json(400, {"ok": False, "error": "Unknown backend: " + backend_name})
                return
            try:
                set_backend_api_key(backend_name, payload.get("api_key") or "")
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
                return

        self._send_json(200, {
            "ok": True,
            "max_read_bytes": get_read_limit(),
            "agent_max_iters": get_agent_max_iters(),
            "edit_chunk_chars": get_edit_chunk_chars(),
            "continue_enabled": get_continue_enabled(),
            "continue_max_rounds": get_continue_max_rounds(),
            "api_keys": {name: bool(cfg.get("api_key")) for name, cfg in config.BACKENDS.items()},
        })

    # --- Memory (remember the last selected working folder) ---
    def handle_memory_get(self):
        """GET /api/memory — return the last remembered working folder (empty string if none)."""
        folder = get_remembered_folder()
        if folder:
            try:
                if not Path(folder).expanduser().is_dir():
                    folder = ""  # Folder has been deleted/moved; fall back to default
            except Exception:
                folder = ""
        self._send_json(200, {"ok": True, "folder": folder})

    def handle_session_get(self):
        """GET /api/session/<run_id> — return a persisted Agent session (full messages + progress)."""
        run_id = self.path[len("/api/session/"):].strip()
        if not run_id:
            self._send_json(400, {"ok": False, "error": "Missing run_id"})
            return
        data = _load_session(run_id)
        if data is None:
            self._send_json(404, {"ok": False, "error": "Session not found"})
            return
        self._send_json(200, {"ok": True, "session": data})

    def handle_memory_post(self):
        """POST /api/memory — save the current working folder."""
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        folder = (payload.get("folder") or "").strip()
        set_remembered_folder(folder)
        self._send_json(200, {"ok": True, "folder": folder})

    def handle_compact(self):
        """POST /api/compact — summarize earlier conversation turns into a single message.

        Body: {messages: [{role, content}, ...], backend: "deepseek", lang: "zh"|"en"}.
        Returns the compacted message list (summary + most recent turns kept intact).
        """
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            self._send_json(400, {"ok": False, "error": "No messages to compact"})
            return
        backend = (payload.get("backend") or "").strip() or "deepseek"
        cfg = config.BACKENDS.get(backend) or config.BACKENDS.get("deepseek")
        lang = (payload.get("lang") or "").strip()
        zh = lang == "zh" or any("\u4e00" <= ch <= "\u9fff" for m in raw_messages for ch in (m.get("content") or ""))
        messages = [
            {"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")}
            for m in raw_messages
            if (m.get("role") in ("user", "assistant", "system"))
        ]
        try:
            client = build_client(cfg)
            compacted = _compress_history(client, cfg, messages, zh=zh)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": "Compaction failed: %s" % e})
            return
        # Only return user/assistant/system entries (drop internal tool messages for the UI).
        out = [{"role": m.get("role"), "content": m.get("content") or ""}
               for m in compacted if m.get("role") in ("user", "assistant", "system")]
        self._send_json(200, {"ok": True, "messages": out})

    # --- Checkpoints (snapshot / restore) ---
    def handle_checkpoint_list(self):
        """GET /api/checkpoints?path=<folder path> — list the checkpoints of that working folder."""
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        folder = (qs.get("path") or [""])[0].strip()
        if not folder:
            self._send_json(400, {"ok": False, "error": "Missing path parameter"})
            return
        try:
            checkpoints = _list_checkpoints(folder)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True, "checkpoints": checkpoints})

    def handle_checkpoint_create(self):
        """POST /api/checkpoint/create — create a checkpoint snapshot for the working folder."""
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        folder = (payload.get("path") or "").strip()
        if not folder:
            self._send_json(400, {"ok": False, "error": "Missing path"})
            return
        label = (payload.get("label") or "").strip()
        try:
            cp = _create_checkpoint(folder, label)
        except FileNotFoundError as e:
            self._send_json(404, {"ok": False, "error": str(e)})
            return
        except PermissionError as e:
            self._send_json(403, {"ok": False, "error": str(e)})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True, "checkpoint": cp})

    def handle_checkpoint_restore(self):
        """POST /api/checkpoint/restore — restore the working folder to the specified checkpoint."""
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        folder = (payload.get("path") or "").strip()
        cid = (payload.get("id") or "").strip()
        if not folder or not cid:
            self._send_json(400, {"ok": False, "error": "Missing path or id"})
            return
        try:
            _restore_checkpoint(folder, cid)
        except FileNotFoundError as e:
            self._send_json(404, {"ok": False, "error": str(e)})
            return
        except PermissionError as e:
            self._send_json(403, {"ok": False, "error": str(e)})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True})

    def handle_checkpoint_delete(self):
        """POST /api/checkpoint/delete — delete a checkpoint snapshot."""
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        folder = (payload.get("path") or "").strip()
        cid = (payload.get("id") or "").strip()
        if not folder or not cid:
            self._send_json(400, {"ok": False, "error": "Missing path or id"})
            return
        try:
            _delete_checkpoint(folder, cid)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True})

    # --- Python interpreter discovery ---
    def handle_python_envs(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        folder = (qs.get("path") or [""])[0].strip() or str(BASE_DIR)
        candidates = discover_python_envs(folder)

        def enrich(c):
            c = dict(c)
            c["version"] = _py_version(c["path"])
            return c

        if candidates:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(candidates))) as ex:
                candidates = list(ex.map(enrich, candidates))

        envs = []
        for c in candidates:
            version = c.get("version") or ""
            if c["kind"] == "current":
                venv = _venv_name(c["path"]) or ""
                if venv and version:
                    label = "Current interpreter · %s · Python %s" % (venv, version)
                elif version:
                    label = "Current interpreter · Python %s" % version
                else:
                    label = "Current interpreter (%s)" % Path(c["path"]).name
            elif c["kind"] == "venv":
                venv = c.get("detail") or ""
                label = ("%s · Python %s" % (venv, version)) if version else ("%s (venv)" % venv)
            elif c["kind"] == "conda":
                venv = c.get("detail") or ""
                label = ("conda: %s · Python %s" % (venv, version)) if version else ("conda: %s" % venv)
            else:
                venv = _venv_name(c["path"]) or ""
                if venv and version:
                    label = "%s · Python %s" % (venv, version)
                elif version:
                    label = "Python %s" % version
                else:
                    label = "Python (%s)" % Path(c["path"]).name
            envs.append({"path": c["path"], "label": label, "kind": c["kind"], "version": version, "venv": venv})
        self._send_json(200, {"ok": True, "envs": envs})

    def handle_python_inspect(self):
        """GET /api/python/inspect?path=<interpreter path> — validate the interpreter path manually specified by the user."""
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path_str = (qs.get("path") or [""])[0].strip()
        if not path_str:
            self._send_json(400, {"ok": False, "error": "Missing path parameter"})
            return
        p = Path(path_str).expanduser()
        try:
            p = p.resolve()
        except Exception:
            pass
        if not p.is_file():
            self._send_json(400, {"ok": False, "error": "Not a valid interpreter file: " + str(p)})
            return
        version = _py_version(str(p))
        venv = _venv_name(str(p)) or ""
        if venv and version:
            label = "%s · Python %s" % (venv, version)
        elif version:
            label = "Python %s" % version
        else:
            label = "Python (%s)" % p.name
        self._send_json(200, {
            "ok": True,
            "path": str(p),
            "label": label,
            "kind": "custom",
            "version": version or "",
            "venv": venv,
        })

    def handle_python_set_current(self):
        """POST /api/python/current — set the user-selected interpreter as the current interpreter."""
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        path_str = (payload.get("path") or "").strip()
        if not path_str:
            self._send_json(400, {"ok": False, "error": "Missing path"})
            return
        p = Path(path_str).expanduser()
        try:
            p = p.resolve()
        except Exception:
            pass
        if not p.is_file():
            self._send_json(400, {"ok": False, "error": "Not a valid interpreter file: " + str(p)})
            return
        set_current_python(str(p))
        self._send_json(200, {"ok": True, "path": str(p)})

    # --- Python run (SSE streaming output) ---
    def handle_python_run(self):
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        script = Path((payload.get("path") or "").strip())
        interpreter = (payload.get("interpreter") or "").strip()
        args = payload.get("args") or []

        # Fall back to the current interpreter (following the user's latest selection) when unspecified or invalid
        if not interpreter or not Path(interpreter).is_file():
            interpreter = get_current_python()
        if not script.is_file():
            self.send_error(400, "Script does not exist")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        run_id = uuid.uuid4().hex
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        try:
            proc = subprocess.Popen(
                [interpreter, str(script)] + [str(a) for a in args],
                cwd=str(script.parent) or None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if IS_WIN else 0,
            )
        except Exception as e:
            sse(self.wfile, {"type": "error", "message": "Unable to start interpreter: " + str(e)})
            return

        with _RUNNING_LOCK:
            _RUNNING[run_id] = proc
        sse(self.wfile, {"type": "start", "run_id": run_id,
                         "command": " ".join([interpreter, str(script)] + [str(a) for a in args])})

        killed = False
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    sse(self.wfile, {"type": "output", "content": line})
                except (BrokenPipeError, ConnectionResetError):
                    killed = True
                    _terminate_process(proc)
                    break
            if not killed:
                proc.wait()
                sse(self.wfile, {"type": "exit", "code": proc.returncode})
        finally:
            if killed or proc.poll() is None:
                _terminate_process(proc)
            with _RUNNING_LOCK:
                _RUNNING.pop(run_id, None)

    def handle_python_stop(self):
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        run_id = payload.get("run_id") or ""
        with _RUNNING_LOCK:
            proc = _RUNNING.get(run_id)
        if not proc:
            self._send_json(404, {"ok": False, "error": "Process does not exist or has already exited"})
            return
        _terminate_process(proc)
        self._send_json(200, {"ok": True})

    # --- Interactive terminal ---
    def handle_terminal_start(self):
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        interpreter = (payload.get("interpreter") or "").strip()
        cwd = (payload.get("cwd") or "").strip()
        # Fall back to the current interpreter (following the user's latest selection) when unspecified or invalid
        if not interpreter or not Path(interpreter).is_file():
            interpreter = get_current_python()
        if cwd:
            p = Path(cwd).expanduser()
            cwd = str(p) if p.is_dir() else None
        _prune_exited_terms()
        term_id = _start_term(interpreter, cwd)
        if not term_id:
            self._send_json(500, {"ok": False, "error": "Unable to start the terminal process"})
            return
        self._send_json(200, {"ok": True, "term_id": term_id})

    def handle_terminal_input(self):
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        term_id = payload.get("term_id") or ""
        text = payload.get("input") or ""
        with _TERM_LOCK:
            term = _TERM.get(term_id)
        if not term:
            self._send_json(404, {"ok": False, "error": "Terminal session does not exist or has ended"})
            return
        try:
            term["proc"].stdin.write(text.encode("utf-8"))
            term["proc"].stdin.flush()
        except Exception as e:
            self._send_json(500, {"ok": False, "error": "Write failed: " + str(e)})
            return
        self._send_json(200, {"ok": True})

    def handle_terminal_output(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        term_id = (qs.get("term_id") or [""])[0].strip()
        try:
            seq = int((qs.get("seq") or ["0"])[0])
        except (TypeError, ValueError):
            seq = 0
        with _TERM_LOCK:
            term = _TERM.get(term_id)
        if not term:
            self._send_json(404, {"ok": False, "error": "Terminal session does not exist or has ended"})
            return
        with term["lock"]:
            buf = list(term["buf"])
            exited = term["exited"]
            code = term["code"]
        data = "".join(buf[seq:])
        self._send_json(200, {
            "ok": True,
            "data": data,
            "seq": len(buf),
            "exited": exited,
            "code": code,
        })

    def handle_terminal_stop(self):
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        term_id = payload.get("term_id") or ""
        with _TERM_LOCK:
            term = _TERM.get(term_id)
        if not term:
            self._send_json(404, {"ok": False, "error": "Terminal session does not exist or has ended"})
            return
        _terminate_process(term["proc"])
        with _TERM_LOCK:
            _TERM.pop(term_id, None)
        self._send_json(200, {"ok": True})

    def handle_command_approval(self):
        """POST /api/command/approval — relay the user's approve/deny decision for a pending Agent command."""
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        aid = (payload.get("id") or "").strip()
        action = (payload.get("action") or "").strip()
        command = (payload.get("command") or "").strip()
        if not aid or action not in ("approve", "deny"):
            self._send_json(400, {"ok": False, "error": "Missing id or action parameter"})
            return
        ok = _resolve_approval(aid, action, command or None)
        self._send_json(200 if ok else 404, {"ok": ok})

    def handle_agent_stop(self):
        """POST /api/agent/stop — request a running Agent loop to stop cleanly at the next check."""
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        run_id = (payload.get("run_id") or "").strip()
        if not run_id:
            self._send_json(400, {"ok": False, "error": "Missing run_id"})
            return
        if _cancel_run(run_id):
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"ok": False, "error": "Unknown run_id"})

    def handle_ask_answer(self):
        """POST /api/ask/answer — relay the user's answer for a pending interactive Agent question."""
        payload, err = self._read_json_body()
        if err:
            self.send_error(400, err)
            return
        aid = (payload.get("id") or "").strip()
        answer = payload.get("answer")
        if not aid:
            self._send_json(400, {"ok": False, "error": "Missing id parameter"})
            return
        ok = _resolve_ask(aid, answer)
        self._send_json(200 if ok else 404, {"ok": ok})

    def _stream(self, payload):
        wfile = self.wfile

        # No longer trim historical messages: the full context is handed to the model (the model has its own context-window handling)
        messages = payload.get("messages") or []

        # Determine whether to use Chinese messages: the UI is in Chinese, or the user's latest
        # message is written in Chinese. Used for both the system prompt and error messages.
        lang = (payload.get("lang") or "").strip()
        last_user = ""
        for _m in reversed(messages):
            if _m.get("role") == "user":
                last_user = _m.get("content") or ""
                break
        zh = lang == "zh" or any("\u4e00" <= _ch <= "\u9fff" for _ch in last_user)

        # Select the DeepSeek model (backend field = model id)
        backend = payload.get("backend") or config.DEFAULT_BACKEND
        if backend not in config.BACKENDS:
            backend = config.DEFAULT_BACKEND
        cfg = config.BACKENDS[backend]
        client = clients[backend]

        # DeepSeek requires a real API key; fail fast with a clear message instead of a
        # confusing 401 from the upstream service when the key was never configured.
        if cfg.get("kind") == "deepseek" and not cfg.get("api_key"):
            sse(wfile, {"type": "error", "message": (
                "DeepSeek API 密钥未设置；请在设置中添加后再聊天。"
                if zh else "DeepSeek API key is not set; add it in Settings before chatting")})
            return

        max_tokens = _num(payload, "max_tokens", config.DEFAULT_MAX_TOKENS, int)
        backend_max = cfg.get("max_tokens")
        if backend_max:
            max_tokens = min(max_tokens, backend_max)  # Backend output cap
        temperature = _num(payload, "temperature", 0.7, float)
        top_p = _num(payload, "top_p", 0.95, float)

        # Agent mode: when enabled and a folder is specified, run the file-tool loop.
        # Plan mode also runs the loop (read-only) when a folder is provided, so the model can
        # explore the project before writing the plan.
        agent_mode = bool(payload.get("agent"))
        # Permission mode: plan (read-only) / ask (default, prompts for approval) / agent (full auto-approve).
        # Legacy "auto-edit" / "yolo" values are mapped to "agent"; unknown values fall back to "ask".
        mode = (payload.get("mode") or "").strip() or "ask"
        if mode in ("auto-edit", "yolo"):
            mode = "agent"
        if mode not in ("plan", "ask", "agent"):
            mode = "ask"
        plan_mode = (mode == "plan")
        folder = (payload.get("folder") or "").strip()
        run_id = (payload.get("run_id") or "").strip() or None

        # AI programming assistant: inject the system prompt (skip if a system message already exists)
        if not any(m.get("role") == "system" for m in messages):
            if agent_mode and folder:
                if plan_mode:
                    system_prompt = config.PLAN_AGENT_SYSTEM_PROMPT_ZH if zh else config.PLAN_AGENT_SYSTEM_PROMPT
                else:
                    # Inject the max attempts and the current piece size so the model knows both limits
                    base = config.AGENT_SYSTEM_PROMPT_ZH if zh else config.AGENT_SYSTEM_PROMPT
                    system_prompt = base.replace(
                        "{AGENT_MAX_ATTEMPTS}", str(get_agent_max_iters())).replace(
                        "{EDIT_CHUNK_CHARS}", str(get_edit_chunk_chars()))
            elif plan_mode:
                system_prompt = config.PLAN_SYSTEM_PROMPT_ZH if zh else config.PLAN_SYSTEM_PROMPT
            else:
                system_prompt = config.ASSISTANT_SYSTEM_PROMPT_ZH if zh else config.ASSISTANT_SYSTEM_PROMPT
            messages = [{"role": "system", "content": system_prompt}] + messages

        # DeepSeek thinking mode + thinking level (reasoning effort). All abilities are on by default.
        thinking = bool(payload.get("enable_thinking", True))
        reasoning_effort = (payload.get("reasoning_effort") or "").strip()
        if reasoning_effort not in config.REASONING_EFFORT_OPTIONS:
            reasoning_effort = cfg.get("reasoning_effort") or config.DEEPSEEK_REASONING_EFFORT

        if agent_mode and folder:
            self._run_agent(wfile, client, cfg, messages, max_tokens, temperature, top_p, folder,
                            enable_thinking=thinking, mode=mode, run_id=run_id,
                            reasoning_effort=reasoning_effort, zh=zh)
            return

        # Request-level parameters: DeepSeek controls thinking via extra_body + reasoning_effort.
        create_extra = {}
        if thinking:
            create_extra["extra_body"] = {"thinking": {"type": "enabled"}}
            create_extra["reasoning_effort"] = reasoning_effort

        # Concurrency limit: avoid the model being overwhelmed by too many requests
        if not _GEN_SLOTS.acquire(timeout=config.MAX_WAIT):
            sse(wfile, {"type": "error", "message": ("服务器繁忙，请稍后再试" if zh else "Server is busy, please try again later")})
            return

        # Compress history when approaching the context limit to keep key information for continuing the conversation
        messages, max_tokens = _fit_context(client, cfg, messages, max_tokens, zh=zh)

        start_time = time.monotonic()
        completion_text = ""
        reasoning_text = ""
        usage_acc = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                     "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0}
        has_usage = False
        first_token_time = None
        finish_reason = None
        cont_messages = list(messages)
        max_rounds = get_continue_max_rounds()

        try:
            for _round in range(max_rounds + 1):
                create_kwargs = dict(
                    model=cfg["model"],
                    messages=cont_messages,
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                    **create_extra,
                )
                # Degrades automatically if the backend rejects optional parameters.
                stream = _create_compatible(client, create_kwargs)
                turn_text, turn_reasoning, _tool_acc, turn_usage, turn_first, turn_finish = _consume_stream(
                    stream, wfile, start_time)
                completion_text += turn_text
                reasoning_text += turn_reasoning
                if turn_usage:
                    has_usage = True
                    usage_acc["prompt_tokens"] += turn_usage.get("prompt_tokens", 0)
                    usage_acc["completion_tokens"] += turn_usage.get("completion_tokens", 0)
                    usage_acc["total_tokens"] += turn_usage.get("total_tokens", 0)
                if first_token_time is None:
                    first_token_time = turn_first
                finish_reason = turn_finish
                # Continue only when the reply was cut off by the output token limit; keep the
                # partial answer and ask the model to continue in smaller pieces (never repeat).
                if (finish_reason != "length" or not get_continue_enabled()
                        or _round >= max_rounds or not turn_text):
                    break
                cont_messages = cont_messages + [
                    {"role": "assistant", "content": turn_text},
                    {"role": "system", "content": config.CONTINUE_SYSTEM_MESSAGE_ZH if zh else config.CONTINUE_SYSTEM_MESSAGE},
                ]
        except (BrokenPipeError, ConnectionResetError):
            return  # Client disconnected; stop generating
        except (APITimeoutError, APIConnectionError):
            sse(wfile, {"type": "error", "message": (("无法连接到模型服务 %s（%s），请检查后端配置" % (cfg["name"], cfg["base_url"])) if zh else "Cannot connect to model service %s (%s), please check the backend configuration" % (cfg["name"], cfg["base_url"]))})
            return
        except APIStatusError as e:
            msg = getattr(e, "message", None) or ""
            sse(wfile, {"type": "error", "message": (("模型服务返回错误 %s：%s" % (e.status_code, msg)) if zh else "Model service returned error %s: %s" % (e.status_code, msg))})
            return
        finally:
            _GEN_SLOTS.release()

        if not has_usage:
            usage = build_usage(messages)
            usage["completion_tokens"] = estimate_tokens(completion_text) + estimate_tokens(reasoning_text)
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        else:
            usage = usage_acc

        _emit_usage(wfile, usage, start_time, first_token_time)
        sse(wfile, {"type": "done"})

    def _run_agent(self, wfile, client, cfg, messages, max_tokens, temperature, top_p, folder,
                   enable_thinking=True, mode="ask", run_id=None, reasoning_effort=None, zh=False):
        """Agent mode entry point (delegates to agent.run_agent)."""
        agent.run_agent(wfile, client, cfg, messages, max_tokens, temperature, top_p, folder,
                        enable_thinking=enable_thinking, mode=mode, run_id=run_id,
                        reasoning_effort=reasoning_effort, zh=zh)

# ------------------------- Graceful shutdown -------------------------
# The active HTTP server, so signal/console handlers can request a clean stop. `shutdown()` must
# run on a thread other than the one blocked in `serve_forever()`, so handlers spawn a daemon thread.
_active_server = None
_active_server_lock = threading.Lock()


def _get_active_server():
    with _active_server_lock:
        return _active_server


def _set_active_server(server):
    global _active_server
    with _active_server_lock:
        _active_server = server


def _cleanup_child_processes():
    """Terminate interactive terminals and running Python processes spawned by this app."""
    with _TERM_LOCK:
        terms = list(_TERM.values())
    for term in terms:
        _terminate_process(term["proc"])
    with _RUNNING_LOCK:
        procs = list(_RUNNING.values())
    for proc in procs:
        _terminate_process(proc)


def request_shutdown():
    """Ask the running server to stop cleanly (safe to call from any thread)."""
    def _do():
        server = _get_active_server()
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
    threading.Thread(target=_do, daemon=True).start()


def install_shutdown_handlers():
    """Install SIGINT/SIGTERM and Windows console-close handlers so the port is released cleanly.

    When the user closes the app (Ctrl+C, Ctrl+Break, or the console X button), the server stops
    serving, the listening socket is closed via `server_close()`, and child processes are killed —
    so port 8888 is free for the next launch.
    """
    def _on_signal(signum, frame):
        request_shutdown()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    if IS_WIN:
        try:
            import ctypes

            _win_console_handler_ref = {}

            @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
            def _console_handler(ctrl_type):
                # CTRL_C_EVENT=0, CTRL_BREAK_EVENT=1, CTRL_CLOSE_EVENT=2,
                # CTRL_LOGOFF_EVENT=5, CTRL_SHUTDOWN_EVENT=6
                if ctrl_type in (0, 1, 2, 5, 6):
                    request_shutdown()
                    return 1  # Handled; prevents the default terminate (gives us time to clean up)
                return 0
            _win_console_handler_ref["handler"] = _console_handler  # keep a strong reference alive
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
        except Exception:
            pass


def run_server(port=None):
    """Configure and start the threaded HTTP server (blocking).

    `port` overrides config.PORT. Returns True when the server ran and stopped normally, False
    when it could not be bound (e.g. the port is already in use), so the caller can retry.
    """
    try:
        port = int(port) if port else int(config.PORT)
    except (TypeError, ValueError):
        port = int(config.PORT)
    Handler.timeout = 600  # Read timeout (seconds): prevent stuck connections from occupying threads for a long time
    try:
        server = ThreadingHTTPServer((config.HOST, port), Handler)
    except OSError as e:
        # Port already in use / permission denied: report instead of crashing so the caller can retry.
        print("Failed to start on %s:%d -> %s" % (config.HOST, port, e))
        return False
    server.daemon_threads = True
    _set_active_server(server)
    url = f"http://{config.HOST}:{port}"
    print(f"LLM chat service started: {url}")
    for name, cfg in config.BACKENDS.items():
        print(f"Model[{name}]: {cfg['base_url']}  model: {cfg['model']}")
    print(f"Piece size for large-file editing: {get_edit_chunk_chars()} characters per tool call")
    # Auto-open the page in the default browser after startup
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"Failed to auto-open browser (you can manually visit {url}): {e}")
    try:
        server.serve_forever()
    finally:
        _set_active_server(None)
        try:
            server.server_close()  # Release the listening socket (port 8888 becomes free)
        except Exception:
            pass
        _cleanup_child_processes()
        print("Service stopped; port %d released." % port)
    return True
