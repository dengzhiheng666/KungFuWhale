# -*- coding: utf-8 -*-
"""Agent loop: model-tool dispatch, permission modes, progress (phase) state, cancellation, session wiring."""
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

import config
import checkpoints
import session
from common import (
    APIStatusError,
    APITimeoutError,
    APIConnectionError,
    _GEN_SLOTS,
    _create_compatible,
    _consume_stream,
    _context_window,
    _emit_usage,
    _fit_context,
    _repair_tool_call_order,
    _truncate_for_model,
    build_usage,
    build_usage_breakdown,
    estimate_tokens,
    get_agent_max_iters,
    get_edit_chunk_chars,
    shrink_edit_chunk_chars,
    sse,
)
from tools import file_tools, db_tools, shell_tools, interactive_tools, lint_tools, test_tools, web_tools
from tools.shell_tools import COMMAND_TOOL_NAMES


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files and subfolders within a specified folder (relative to the selected folder).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path; empty means the root directory"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regular expression. Use grep before read_file on large codebases to locate relevant lines quickly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for"},
                    "path": {"type": "string", "description": "Relative file or folder path; empty means the whole selected folder"},
                    "glob": {"type": "string", "description": "Optional filename glob filter, e.g. *.py or **/*.tsx"},
                    "ignore_case": {"type": "boolean", "description": "Case-insensitive matching (default false)"},
                    "max_results": {"type": "integer", "description": "Maximum number of matching lines to return (default 100)"}
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files whose relative path matches a glob pattern (e.g. **/*.tsx, src/*.py). Returns newline-separated relative paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. **/*.tsx or src/*.py"},
                    "path": {"type": "string", "description": "Relative folder to search; empty means the whole selected folder"}
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file (relative to the selected folder) with 1-based line numbers. Use offset and limit to read a slice of a large file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"},
                    "offset": {"type": "integer", "description": "1-based line number to start reading from (default 1)"},
                    "limit": {"type": "integer", "description": "Maximum number of lines to return (default 500, max 2000)"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file (relative to the selected folder), creating parent directories automatically if they do not exist. "
                           "For large files write piece by piece: first call with the opening piece (mode omitted), then call again with mode=\"append\" once per remaining piece. "
                           "Never emit more than the announced piece size in a single call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. snake.html"},
                    "content": {"type": "string", "description": "File contents (or one piece of it)"},
                    "mode": {"type": "string", "description": "\"overwrite\" (default) creates/replaces the file; \"append\" adds content to the end of the existing file"}
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a single unique occurrence of old_string with new_string in an existing text file. Prefer edit_file over write_file for existing files; always read_file before edit_file so old_string matches exactly. "
                           "On a large file, edit in several small windows (each within the announced piece size) instead of replacing the whole file at once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path of the file to edit"},
                    "old_string": {"type": "string", "description": "Exact text to replace (must be unique in the file)"},
                    "new_string": {"type": "string", "description": "Replacement text"}
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file or folder (relative to the selected folder).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "Create a folder (relative to the selected folder).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "db_schema",
            "description": "View the table and view structure and their column information of a SQLite database (.db/.sqlite/.sqlite3/.db3) (read-only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path of the database file, e.g. data.db"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "db_query",
            "description": "Execute SQL against a SQLite database. SELECT/WITH/PRAGMA/EXPLAIN return query results; "
                            "write statements such as CREATE/INSERT/UPDATE/DELETE/DROP modify the database (the file is created automatically if it does not exist, multiple statements supported).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path of the database file, e.g. data.db"},
                    "sql": {"type": "string", "description": "The SQL statement to execute"}
                },
                "required": ["path", "sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_venv",
            "description": "Create a Python virtual environment in the working folder (default .venv) to isolate project dependencies. A confirmation dialog is shown to the user before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path of the virtual environment directory, default .venv"},
                    "python": {"type": "string", "description": "Path of the interpreter used to create the environment; defaults to the current interpreter"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_packages",
            "description": "Install dependency packages in the project's Python virtual environment; installs requirements.txt automatically when no package names are specified. A confirmation dialog is shown to the user before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "packages": {"type": "string", "description": "Package name(s) to install (space-separated, can be multiple); empty installs requirements.txt"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds, default 600 (range 60~3600)"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command in the working folder (such as running a script, running tests, building, etc.) and return standard output and exit code. A confirmation dialog is shown to the user before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds, default 180"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run the project's test suite with pytest and return a concise pass/fail summary with the "
                "tail of the output. This is the primary way to verify code changes: prefer it over a raw "
                "run_command 'pytest' invocation because it returns a normalized verdict. A confirmation "
                "dialog is shown to the user before execution."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional relative file or folder to limit the test run to; empty runs the whole project"},
                    "keyword": {"type": "string", "description": "Optional pytest -k expression to select tests by name, e.g. 'login or auth'"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds, default 300 (range 1~1800)"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "smoke_test",
            "description": (
                "Run a smoke test on the project in the background to automatically verify whether the Python program starts successfully. "
                "It starts the target program in a background thread and observes it for a while: a script-style program that exits normally (exit code 0) is considered passed; "
                "a long-running service-style program that keeps running without crashing is considered passed; crashing at startup is considered failed. "
                "After the test it automatically terminates the process, deletes temporary log files, and cleans up newly created __pycache__/.pytest_cache cache directories. "
                "Returns test_id immediately; use the test_result tool to query the final result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entry": {"type": "string", "description": "Relative path of the entry file (e.g. main.py / app.py); empty auto-detects"},
                    "timeout": {"type": "integer", "description": "Maximum seconds to observe startup, default 10 (range 1~60)"},
                    "smoke": {"type": "boolean", "description": "Whether to start with the --smoke argument (for CLIs supporting a built-in smoke mode), default false"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_result",
            "description": "Query the result of a background smoke-test task (started by smoke_test); if still running, it will prompt you to query again later.",
            "parameters": {
                "type": "object",
                "properties": {
                    "test_id": {"type": "string", "description": "The test_id returned by smoke_test"}
                },
                "required": ["test_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lint",
            "description": (
                "Check Python source files under a path (a .py file or a folder, relative to the selected folder) for syntax and lint errors. "
                "This is read-only and runs immediately without user confirmation. It always performs a built-in syntax check, and additionally "
                "uses ruff / flake8 / pyflakes when available. Run this before smoke_test / running tests, and fix every reported error before proceeding."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path of a .py file or folder to lint; empty means the whole selected folder"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user an interactive clarifying question when a key detail is ambiguous and would "
                "significantly affect the implementation. Provide a short question and, whenever possible, a list of "
                "predefined options so the user can answer with a single click; the user may also type a custom answer "
                "or skip. Use this instead of guessing when the choice matters. Returns the user's answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask the user (clear and specific)"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Predefined choices the user can click (2~4 recommended); omit when a free-form answer is expected",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "progress_write",
            "description": "Track the implementation plan as an ordered list of phases. Call with the full phase plan before implementing; mark exactly one phase in_progress; update phase statuses and notes after each major step.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phases": {
                        "type": "array",
                        "description": "The full ordered phase plan; at most one phase may be in_progress.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "Short phase name, e.g. 'Set up environment'"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "blocked"]},
                                "detail": {"type": "string", "description": "Optional one-line note on the current activity or outcome for this phase"}
                            },
                            "required": ["title", "status"],
                        },
                    },
                },
                "required": ["phases"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current, external, or up-to-date information (news, APIs, "
                "documentation, versions, prices, etc.). Returns structured results with title, URL "
                "and a summary. Use it only when the answer genuinely needs live information — for "
                "code, files, or context you already have, answer directly. If you are unsure whether "
                "a search is needed, ask the user via ask_user first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (1-128 characters)"},
                    "count": {"type": "integer", "description": "Number of results to return (1-50, default 5)"},
                    "need_content": {"type": "boolean", "description": "Whether to also return full page content (default false)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a single web page and return its content as Markdown. Use it to read a specific "
                "URL the user provided or that web_search surfaced."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The HTTP/HTTPS URL to fetch"},
                },
                "required": ["url"],
            },
        },
    },
]


# ------------------------- Agent run cancellation -------------------------
# Per-run threading.Event keyed by run_id; POST /api/agent/stop sets it and the agent loop
# checks it at the top of each iteration and immediately before each tool call.


# ------------------------- Agent run cancellation -------------------------
# Per-run threading.Event keyed by run_id; POST /api/agent/stop sets it and the agent loop
# checks it at the top of each iteration and immediately before each tool call.
_CANCEL_EVENTS = {}
_CANCEL_LOCK = threading.Lock()


def _register_cancel(run_id):
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.setdefault(run_id, threading.Event())


def _cancel_run(run_id):
    """Set the cancel event for a run, returning False when the run_id is unknown."""
    with _CANCEL_LOCK:
        ev = _CANCEL_EVENTS.get(run_id)
    if ev is None:
        return False
    ev.set()
    return True


def _unregister_cancel(run_id):
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(run_id, None)


def _is_cancelled(run_id):
    with _CANCEL_LOCK:
        ev = _CANCEL_EVENTS.get(run_id)
    return bool(ev and ev.is_set())


# ------------------------- Agent mode: progress (phase) tracking -------------------------
_PROGRESS_STATE = {}
_PROGRESS_LOCK = threading.Lock()

_PHASE_STATUSES = ("pending", "in_progress", "completed", "blocked")
_PLAN_PROGRESS_PREFIX = "[Current plan progress]"
_PLAN_PROGRESS_PREFIX_ZH = "[当前计划进度]"
_PERMISSION_MODE_PREFIX = "Current permission mode: "
_PERMISSION_MODE_PREFIX_ZH = "当前权限模式："
_PROJECT_INSTRUCTIONS_PREFIX = "[Project instructions"


def _get_progress(run_id):
    """Return a copy of the current phase plan for a run (empty list when none)."""
    with _PROGRESS_LOCK:
        return list(_PROGRESS_STATE.get(run_id) or [])


def _set_progress(run_id, phases):
    """Store the phase plan for a run."""
    with _PROGRESS_LOCK:
        _PROGRESS_STATE[run_id] = phases


def _render_progress(phases):
    """Render the phase plan into a compact numbered text block for the model."""
    marks = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "blocked": "[!]"}
    if not phases:
        return "(no plan yet)"
    lines = []
    for idx, phase in enumerate(phases, 1):
        line = "%d. %s %s" % (idx, marks.get(phase.get("status"), "[?]"), phase.get("title", ""))
        detail = str(phase.get("detail") or "").strip()
        if detail:
            line += " - " + detail
        lines.append(line)
    return "\n".join(lines)


def _drop_plan_progress(messages):
    """Remove any previously appended plan-progress system message (there should be at most one)."""
    messages[:] = [
        m for m in messages
        if not (m.get("role") == "system"
                and str(m.get("content") or "").startswith((_PLAN_PROGRESS_PREFIX, _PLAN_PROGRESS_PREFIX_ZH)))
    ]


def _read_before_write_check(name, args, root, read_paths):
    """Enforce the read-before-write guardrail for write_file/edit_file.

    Existing files that have not been read this run are refused; new-file writes are allowed.
    Returns an error string, or None when the tool may proceed.
    """
    if name not in ("write_file", "edit_file"):
        return None
    rel = (args.get("path") or "").strip()
    try:
        target = file_tools._safe_resolve(root, rel)
    except ValueError as e:
        return "Error: " + str(e)
    if target.exists() and not target.is_dir() and str(target.resolve()) not in read_paths:
        return "Error: read %s before modifying it." % rel
    return None


def _agent_progress_write(args, wfile, run_id):
    """Agent tool: validate and store the phase plan, emitting a `progress` SSE event."""
    raw = args.get("phases")
    if not isinstance(raw, list) or not raw:
        return "Error: phases must be a non-empty array of {title, status, detail}"
    phases = []
    for item in raw:
        if not isinstance(item, dict):
            return "Error: each phase must be an object with title/status"
        title = str(item.get("title") or "").strip()
        if not title:
            return "Error: each phase must have a non-empty title"
        status = str(item.get("status") or "pending")
        if status not in _PHASE_STATUSES:
            return "Error: invalid status %r (use pending|in_progress|completed|blocked)" % status
        detail = str(item.get("detail") or "").strip()
        phase = {"title": title, "status": status}
        if detail:
            phase["detail"] = detail
        phases.append(phase)
    in_progress = sum(1 for p in phases if p["status"] == "in_progress")
    if in_progress > 1:
        return "Error: at most one phase may be in_progress (got %d)" % in_progress
    _set_progress(run_id, phases)
    sse(wfile, {"type": "progress", "phases": phases})
    return "Updated phase plan (%d phases)." % len(phases)


# Tools allowed in read-only "plan" mode. Everything else (write/command tools) is refused.
PLAN_READ_ONLY_TOOLS = frozenset({
    "list_files", "read_file", "grep", "glob", "db_schema", "lint", "test_result",
    "ask_user", "progress_write", "web_search", "web_fetch",
})
# db_query is additionally offered in plan mode, but only read-only SQL is allowed at runtime.
_PLAN_TOOL_NAMES = PLAN_READ_ONLY_TOOLS | {"db_query"}


def _effective_permission(mode: str, name: str, args: dict) -> str:
    """Resolve the permission for a tool call under the current permission mode.

    Returns "auto", "ask", or "plan" (read-only refusal). Unknown modes fall back to "ask".
    """
    base = interactive_tools._tool_permission(name, args)
    if base == "auto":
        return "auto"
    if mode == "agent":
        # Full auto-approve: every tool runs immediately without a permission dialog.
        return "auto"
    if mode == "plan":
        # Plan mode is read-only: allow inspection tools, refuse anything that writes or runs code.
        if name in PLAN_READ_ONLY_TOOLS:
            return "auto"
        if name == "db_query":
            # SELECT/WITH/PRAGMA/EXPLAIN are read-only and allowed; any write is refused.
            return "auto" if base == "auto" else "plan"
        return "plan"
    return "ask"


def _load_project_instructions(root):
    """Load project instruction files from the working folder, returning (content, files).

    Looks for each file in config.AGENT_INSTRUCTIONS_FILES; content is capped at 16 KB.
    """
    parts = []
    files = []
    for name in config.AGENT_INSTRUCTIONS_FILES:
        p = Path(root) / name
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    parts.append("--- %s ---\n%s" % (name, text))
                    files.append(name)
        except OSError:
            continue
    if not parts:
        return "", []
    return _truncate_for_model("\n\n".join(parts), 16 * 1024), files


def _agent_execute_tool(name, args, root):
    """Execute a tool by name, returning a result string (never raises)."""
    try:
        if name == "list_files":
            return file_tools.tool_list_files(root, args)
        if name == "grep":
            return file_tools.tool_grep(root, args)
        if name == "glob":
            return file_tools.tool_glob(root, args)
        if name == "read_file":
            return file_tools.tool_read_file(root, args)
        if name == "write_file":
            return file_tools.tool_write_file(root, args)
        if name == "edit_file":
            return file_tools.tool_edit_file(root, args)
        if name == "delete_file":
            return file_tools.tool_delete_file(root, args)
        if name == "create_directory":
            return file_tools.tool_create_directory(root, args)
        if name == "db_schema":
            return db_tools.tool_db_schema(root, args)
        if name == "db_query":
            return db_tools.tool_db_query(root, args)
        if name == "smoke_test":
            return test_tools.tool_smoke_test(root, args)
        if name == "test_result":
            return test_tools.tool_test_result(args)
        if name == "lint":
            return lint_tools.tool_lint(root, args)
        if name == "web_search":
            return web_tools.tool_web_search(args)
        if name == "web_fetch":
            return web_tools.tool_web_fetch(args)
        return "Error: unknown tool " + str(name)
    except ValueError as e:
        return "Error: " + str(e)
    except PermissionError as e:
        return "Error: insufficient permission: " + str(e)
    except Exception as e:
        return "Error: " + str(e)


def _agent_execute_approved_tool(name: str, args: dict, root, wfile, pending_ids=None) -> str:
    """Execute a non-shell tool that requires approval (write_file/edit_file/delete_file/create_directory/smoke_test)."""
    command, description = interactive_tools._approval_display(name, args)
    status, _final_command = interactive_tools._request_approval(
        wfile, command, description, name, "tool", pending_ids=pending_ids)
    if status == "hard_blocked":
        return "Error: this command matches a hard-block pattern and will never be executed."
    if status != "approved":
        return "The user rejected the action; not executed: %s" % name
    return _agent_execute_tool(name, args, root)


def _agent_execute_command_tool(name, args, root, wfile, mode="ask", pending_ids=None):
    """Execute a command-type tool (create_venv / install_packages / run_command)."""
    return shell_tools.execute_command_tool(name, args, root, wfile, mode=mode, pending_ids=pending_ids)


# Tools that write file content: a failure of one of them shrinks the global piece size so the
# next attempt uses smaller (and therefore safer) pieces.
_EDIT_TOOL_NAMES = {"write_file", "edit_file"}
# How many consecutive "output was cut off" continuations are allowed before stopping
MAX_TRUNCATION_CONTINUES = 5


def _piece_size_hint(size=None, zh=False):
    """Instruction telling the model the current maximum size of a single write/edit piece."""
    size = size or get_edit_chunk_chars()
    if zh:
        return ("每次工具调用最多输出 %d 个字符的文件内容：第一段用 write_file（覆盖），"
                "之后每段用 write_file 加 mode=\"append\" 追加；编辑时使用较小的 old_string / new_string 窗口。" % size)
    return ("Emit at most %d characters of file content per tool call: write_file for the first "
            "piece, then write_file with mode=\"append\" once per remaining piece; for edits use "
            "small old_string / new_string windows." % size)


def _prompt_preview(messages, keep_last=60):
    """Return (messages, truncated) for the frontend's per-step "prompt" detail view.

    Keeps the first message (usually the system prompt) and the most recent `keep_last`
    messages so the SSE payload stays bounded while still showing the relevant context.
    """
    if not isinstance(messages, list):
        return [], False
    if len(messages) <= keep_last:
        return messages, False
    return messages[:1] + messages[-keep_last:], True


def run_agent(wfile, client, cfg, messages, max_tokens, temperature, top_p, folder,
              enable_thinking=True, mode="ask", run_id=None, reasoning_effort=None, zh=False):
    """Agent mode: repeatedly call the model -> execute tools -> feed results back, until the model gives a final answer."""
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        sse(wfile, {"type": "error", "message": ("Agent 模式需要一个有效的工作文件夹" if zh else "Agent mode requires a valid folder")})
        return

    if not _GEN_SLOTS.acquire(timeout=config.MAX_WAIT):
        sse(wfile, {"type": "error", "message": ("服务器繁忙，请稍后再试" if zh else "Server is busy, please try again later")})
        return

    if not run_id:
        run_id = uuid.uuid4().hex
    started_at = time.time()
    read_paths = set()  # Files successfully read this run (read-before-write guardrail)
    pending_approval_ids = []  # Approval ids currently waiting on the user (persisted with the session)
    _register_cancel(run_id)

    # Resume an in-session conversation: when the frontend supplies a run_id that matches a
    # persisted agent session for the same folder and permission mode, continue from the full
    # saved context (tool calls + results + reasoning) instead of starting fresh. Keeping the
    # whole working context makes follow-up chats able to root out bugs from prior steps.
    if run_id:
        saved = session.load_session(run_id)
        if (isinstance(saved, dict) and saved.get("mode") == mode
                and str(saved.get("folder") or "") == str(root)):
            saved_messages = saved.get("messages")
            if isinstance(saved_messages, list) and saved_messages:
                # The freshly-injected system prompt (from server.py) is replaced by the saved
                # one, which already carries the permission prefix / project instructions.
                new_user = messages[-1] if messages and messages[-1].get("role") == "user" else None
                messages = list(saved_messages)
                if new_user is not None:
                    messages.append(new_user)
            saved_progress = saved.get("progress")
            if isinstance(saved_progress, list):
                _set_progress(run_id, saved_progress)

    # Inject the current permission mode at the top of the agent system prompt (only on a
    # fresh run; a resumed session already carries the injected prefix).
    perm_prefix = _PERMISSION_MODE_PREFIX_ZH if zh else _PERMISSION_MODE_PREFIX
    has_perm_prefix = any(
        m.get("role") == "system"
        and str(m.get("content") or "").startswith((_PERMISSION_MODE_PREFIX, _PERMISSION_MODE_PREFIX_ZH))
        for m in messages
    )
    if not has_perm_prefix:
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = "%s%s\n\n%s" % (perm_prefix, mode, messages[0]["content"])
        else:
            messages.insert(0, {"role": "system", "content": perm_prefix + mode})

    if mode == "agent":
        sse(wfile, {"type": "banner", "kind": "mode", "mode": "agent",
                    "warning": ("Agent 模式：所有工具和命令将自动批准并立即执行，无需确认。" if zh
                                else "Agent mode: all tools and commands auto-approve and run "
                                     "immediately without confirmation.")})

    # Project instruction autoload: prepend any AGENTS.md / CLAUDE.md / .agents.md found
    # in the working folder so the model treats them as authoritative project context. Skip
    # when a previous run of the same session already injected them (resume path).
    instr_content, instr_files = _load_project_instructions(root)
    already_has_instructions = any(
        m.get("role") == "system"
        and str(m.get("content") or "").startswith(_PROJECT_INSTRUCTIONS_PREFIX)
        for m in messages
    )
    if instr_content and not already_has_instructions:
        messages.insert(0, {"role": "system",
                            "content": "[Project instructions — treat as authoritative]\n" + instr_content})
        sse(wfile, {"type": "banner", "kind": "instructions", "files": instr_files})

    start_time = time.monotonic()
    first_token_time = None
    completion_text = ""
    reasoning_text = ""
    usage_acc = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                 "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0}
    tool_base = 0  # Global tool index across turns, so the frontend can correctly map tool calls to results
    truncation_continues = 0  # Consecutive replies cut off by the output token limit
    auto_checkpoint_done = False  # Whether an auto checkpoint has already been created for this Agent session
    recent_tool_calls = []  # Last six (tool_name, sha256(arguments)) tuples for dead-loop detection

    def persist():
        """Persist the current run state to BASE_DIR/.sessions/<run_id>.json (best-effort)."""
        session.save_session({
            "run_id": run_id,
            "folder": str(root),
            "mode": mode,
            "messages": messages,
            "progress": _get_progress(run_id),
            "pending_approval_ids": list(pending_approval_ids),
            "started_at": started_at,
            "updated_at": time.time(),
        })

    fatal_error = None
    exhausted = False
    stopped = False
    try:
        for _iter in range(get_agent_max_iters()):
            if _is_cancelled(run_id):
                stopped = True
                break
            # Mark the start of a new reasoning/tool step so the frontend can render the
            # chain-of-thought as separate, collapsible steps instead of one giant block.
            sse(wfile, {"type": "step_start", "index": _iter + 1})
            persist()
            # Before each turn: compress history when approaching the context limit (keep the current session and recent turns)
            messages, max_tokens = _fit_context(client, cfg, messages, max_tokens, zh=zh)

            # Re-append the current phase plan every iteration so context compression cannot
            # lose it; replace any previous copy to avoid unbounded plan-progress duplication.
            _drop_plan_progress(messages)
            messages.append({
                "role": "system",
                "content": (_PLAN_PROGRESS_PREFIX_ZH if zh else _PLAN_PROGRESS_PREFIX)
                + "\n" + _render_progress(_get_progress(run_id)),
            })

            # Guard against an assistant tool_calls message not being immediately followed by its
            # tool results (e.g. a resumed session persisted before the dead-loop reminder fix).
            messages = _repair_tool_call_order(messages)

            # Plan mode only exposes read-only tools to the model (write/command tools are not offered).
            tools = [t for t in AGENT_TOOLS if t["function"]["name"] in _PLAN_TOOL_NAMES] if mode == "plan" else AGENT_TOOLS
            create_kwargs = dict(
                model=cfg["model"],
                messages=messages,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                tools=tools,
            )
            if cfg.get("kind") == "deepseek":
                # DeepSeek: thinking mode is controlled via thinking extra_body + reasoning_effort
                # (the "thinking level"). Fall back to the model default when not supplied.
                if enable_thinking:
                    create_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                    create_kwargs["reasoning_effort"] = (
                        reasoning_effort or cfg.get("reasoning_effort")
                        or config.DEEPSEEK_REASONING_EFFORT
                    )

            # Expose the prompt sent to the model this turn so the frontend can show it in the
            # step's collapsible details (bounded preview; the full context stays on the server).
            _preview, _preview_truncated = _prompt_preview(messages)
            sse(wfile, {"type": "step_prompt", "index": _iter + 1,
                        "messages": _preview, "truncated": bool(_preview_truncated)})

            # Real-time context breakdown for the frontend context manager (system prompt,
            # tool definitions, tool calls, and conversation messages).
            _bd = build_usage_breakdown(messages, tools)
            sse(wfile, {"type": "context",
                        "system": _bd["system"],
                        "definitions": _bd["definitions"],
                        "tool_calls": _bd["tool_calls"],
                        "conversation": _bd["conversation"],
                        "total": _bd["total"],
                        "window": _context_window(cfg)})

            try:
                # Degrades automatically when the backend rejects optional parameters, so the
                # agent keeps working with any OpenAI-compatible model.
                stream = _create_compatible(client, create_kwargs)
                turn_text, turn_reasoning, tool_acc, _turn_usage, turn_first, finish_reason = _consume_stream(
                    stream, wfile, start_time, tool_base=tool_base, usage_acc=usage_acc)
                reasoning_text += turn_reasoning
                completion_text += turn_text
                if first_token_time is None:
                    first_token_time = turn_first
            except (BrokenPipeError, ConnectionResetError):
                return  # Client disconnected
            except (APITimeoutError, APIConnectionError):
                sse(wfile, {"type": "error", "message": (("无法连接到模型服务 %s（%s），请检查后端配置" % (cfg["name"], cfg["base_url"])) if zh else "Cannot connect to model service %s (%s), please check the backend configuration" % (cfg["name"], cfg["base_url"]))})
                return
            except APIStatusError as e:
                sse(wfile, {"type": "error", "message": (("模型服务返回错误 %s：%s" % (e.status_code, getattr(e, "message", "") or "")) if zh else "Model service returned error %s: %s" % (e.status_code, getattr(e, "message", "") or ""))})
                return

            tool_calls = []
            for idx in sorted(tool_acc):
                tc = tool_acc[idx]
                if not tc["id"]:
                    tc["id"] = "call_" + uuid.uuid4().hex[:12]
                tool_calls.append({
                    "index": tool_base + idx,
                    "id": tc["id"],
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                })

            # Normalize a missing finish_reason (some backends omit it) from the
            # presence of tool calls, so unknown-finish handling only triggers on real values.
            finish = finish_reason or ("tool_calls" if tool_calls else "stop")

            if finish == "length" and tool_calls:
                # The model output was truncated mid-tool-call: never execute a possibly
                # truncated tool call or fall back to json.loads(...) -> {}. Keep the assistant
                # tool_calls message so the following tool error messages form a valid sequence.
                # When the truncation happened while writing a file, the piece was too big for the
                # model's output limit: shrink the piece size and tell the model the new value.
                if any(tc["name"] in _EDIT_TOOL_NAMES for tc in tool_calls):
                    new_size = shrink_edit_chunk_chars()
                else:
                    new_size = get_edit_chunk_chars()
                truncated_note = ("Tool call output was truncated mid-JSON; retrying. "
                                  + _piece_size_hint(new_size))
                content = truncated_note if not turn_text else turn_text.rstrip() + "\n" + truncated_note
                messages.append({
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": turn_reasoning or None,
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in tool_calls
                    ],
                })
                for tc in tool_calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": ("Error: model output was truncated mid-tool-call; tool not "
                                    "executed. " + _piece_size_hint(new_size)),
                    })
                tool_base += len(tool_calls)
                continue

            if finish not in ("stop", "tool_calls", "length"):
                fatal_error = "Agent turn ended with unexpected finish_reason: %s" % finish
                break

            if not tool_calls:
                if finish == "length" and truncation_continues < MAX_TRUNCATION_CONTINUES:
                    # The reply was cut off by the output token limit (finish_reason="length"):
                    # keep the partial answer, ask the model to continue in smaller pieces.
                    truncation_continues += 1
                    new_size = shrink_edit_chunk_chars()
                    messages.append({
                        "role": "assistant",
                        "content": turn_text or None,
                        "reasoning_content": turn_reasoning or None,
                    })
                    messages.append({
                        "role": "system",
                        "content": ((config.CONTINUE_SYSTEM_MESSAGE_ZH if zh else config.CONTINUE_SYSTEM_MESSAGE)
                                    + " " + _piece_size_hint(new_size, zh=zh)),
                    })
                    continue
                break  # No tool calls this turn; the final answer has already been streamed out

            messages.append({
                "role": "assistant",
                "content": turn_text or None,
                "reasoning_content": turn_reasoning or None,
                "tool_calls": [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in tool_calls
                ],
            })

            # If this turn includes tools that add/delete/modify files, and no auto checkpoint has been created yet in this session,
            # snapshot the current folder first so the user can later restore to the pre-modification state with one click.
            if (not auto_checkpoint_done and
                    any(checkpoints._tool_call_modifies(tc["name"], tc["arguments"]) for tc in tool_calls)):
                try:
                    cp = checkpoints._create_checkpoint(folder, None, auto=True)
                    auto_checkpoint_done = True
                    sse(wfile, {"type": "checkpoint", "checkpoint": cp})
                except Exception as e:
                    sse(wfile, {"type": "checkpoint_error", "message": str(e)})

            dead_loop_detected = False
            for tc in tool_calls:
                if _is_cancelled(run_id):
                    stopped = True
                    break
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except (json.JSONDecodeError, ValueError):
                    args = {}

                # Dead-loop detection: if the exact same (tool_name, sha256(arguments)) tuple
                # appears three times within the last-six-tool window, nudge the model instead
                # of letting it loop forever.
                call_sig = (tc["name"], hashlib.sha256((tc["arguments"] or "").encode("utf-8")).hexdigest())
                recent_tool_calls.append(call_sig)
                if len(recent_tool_calls) > 6:
                    recent_tool_calls = recent_tool_calls[-6:]
                if recent_tool_calls.count(call_sig) >= 3:
                    # Defer the reminder until after this turn's tool results are appended:
                    # the API rejects an assistant tool_calls message that is not immediately
                    # followed by the matching tool messages.
                    dead_loop_detected = True
                    recent_tool_calls = []

                executed = False  # Whether the tool body really ran (errors of it shrink the piece size)
                try:
                    if tc["name"] == "ask_user":
                        result = interactive_tools.tool_ask_user(args, wfile)
                    elif tc["name"] == "progress_write":
                        result = _agent_progress_write(args, wfile, run_id)
                    else:
                        perm = _effective_permission(mode, tc["name"], args)
                        if perm == "plan":
                            result = "Error: plan mode is read-only; tool %s is not allowed." % tc["name"]
                        elif tc["name"] in COMMAND_TOOL_NAMES:
                            result = _agent_execute_command_tool(tc["name"], args, root, wfile, mode=mode,
                                                                 pending_ids=pending_approval_ids)
                        else:
                            guard = _read_before_write_check(tc["name"], args, root, read_paths)
                            if guard is not None:
                                result = guard
                            elif perm == "auto":
                                result = _agent_execute_tool(tc["name"], args, root)
                                executed = True
                                if tc["name"] == "read_file" and not result.startswith("Error:"):
                                    try:
                                        target = file_tools._safe_resolve(root, (args.get("path") or "").strip())
                                        read_paths.add(str(target.resolve()))
                                    except Exception:
                                        pass
                            else:
                                result = _agent_execute_approved_tool(tc["name"], args, root, wfile,
                                                                      pending_ids=pending_approval_ids)
                                executed = True
                except (BrokenPipeError, ConnectionResetError):
                    return  # Client disconnected
                except Exception as e:
                    # A single failing step must not abort the whole agent loop: report the
                    # error as the tool result so the model can recover in the next turn.
                    result = "Tool execution error (%s): %s" % (tc["name"], e)
                    executed = True

                # After a successful write/edit the agent owns that content, so the next pieces
                # (append / follow-up edits) are not blocked by the read-before-write guardrail.
                if (executed and tc["name"] in _EDIT_TOOL_NAMES and isinstance(result, str)
                        and not result.startswith("Error:")):
                    try:
                        read_paths.add(str(file_tools._safe_resolve(
                            root, (args.get("path") or "").strip()).resolve()))
                    except Exception:
                        pass

                # A failing write/edit piece usually means the piece was too large for the model
                # (or for the tool): lower the global piece size so the retry uses smaller pieces.
                if (executed and tc["name"] in _EDIT_TOOL_NAMES
                        and isinstance(result, str) and result.startswith("Error:")):
                    new_size = shrink_edit_chunk_chars()
                    if zh:
                        result = "%s\n[片段大小已自动降低为 %d 个字符。%s]" % (
                            result, new_size, _piece_size_hint(new_size, zh=True))
                    else:
                        result = "%s\n[The piece size was reduced automatically to %d characters. %s]" % (
                            result, new_size, _piece_size_hint(new_size))
                # Stream any file changes (red/green diff appendix) captured by the file tools.
                for change in file_tools.drain_changes():
                    sse(wfile, {"type": "file_change", "change": change})
                sse(wfile, {"type": "tool_result", "index": tc["index"], "name": tc["name"], "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": _truncate_for_model(result, config.TOOL_RESULT_MAX_BYTES),
                })

            if dead_loop_detected and not stopped:
                messages.append({
                    "role": "system",
                    "content": ("你似乎在重复相同的操作。请停止，总结你已尝试的方法，"
                                "并用 ask_user 与用户确认下一步如何进行。" if zh
                                else "You appear to be repeating the same action. Stop, summarize "
                                     "what you have tried, and use ask_user to check with the user "
                                     "how to proceed."),
                })

            tool_base += len(tool_calls)
            if stopped:
                break
        else:
            # The loop finished without the model giving a final answer (max attempts reached).
            exhausted = True
    except (BrokenPipeError, ConnectionResetError):
        return  # Client disconnected; stop immediately
    except Exception as e:
        # An unexpected error (e.g. a tool step raised) must not silently kill the stream,
        # otherwise the frontend drops the in-progress conversation. Report it and finish gracefully.
        fatal_error = ("Agent 运行失败：" if zh else "Agent run failed: ") + str(e)
    finally:
        _unregister_cancel(run_id)
        persist()
        _GEN_SLOTS.release()

    if fatal_error is None and exhausted:
        fatal_error = (
            ("Agent 模式在未给出最终答案的情况下达到了最大尝试次数（%d）。"
             "已执行的步骤均已保留；你可以从这里继续。") if zh else
            ("Agent mode reached the maximum number of attempts (%d) without producing a final answer. "
             "The steps already performed are preserved; you can continue from here.")
        ) % get_agent_max_iters()

    if fatal_error is not None:
        try:
            sse(wfile, {"type": "error", "message": fatal_error})
        except (BrokenPipeError, ConnectionResetError):
            return

    if usage_acc["total_tokens"] == 0:
        usage_acc = build_usage(messages)
        usage_acc["completion_tokens"] = estimate_tokens(completion_text) + estimate_tokens(reasoning_text)
        usage_acc["total_tokens"] = usage_acc["prompt_tokens"] + usage_acc["completion_tokens"]

    _emit_usage(wfile, usage_acc, start_time, first_token_time)
    sse(wfile, {"type": "done", "stopped": True} if stopped else {"type": "done"})
