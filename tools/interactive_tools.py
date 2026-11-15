# -*- coding: utf-8 -*-
"""Interactive Agent tools: command approval plumbing and the ask_user question dialog."""
import threading
import uuid

import config
from common import sse

# Registry of pending-approval commands: approval_id -> {"command", "description", "kind",
#                                     "event"(threading.Event), "approved", "final_command"}
_APPROVALS = {}
_APPROVALS_LOCK = threading.Lock()
APPROVAL_TIMEOUT = 300  # Maximum time to wait for user approval (seconds); timeout is treated as "rejected"


def _resolve_approval(aid, action, command=None):
    """The user makes a decision (approve/deny) on a pending command, waking up the blocked Agent loop."""
    with _APPROVALS_LOCK:
        item = _APPROVALS.get(aid)
    if not item:
        return False
    item["approved"] = (action == "approve")
    item["final_command"] = command or None
    item["event"].set()
    return True


def _request_approval(wfile, command: str, description: str, tool_name: str, kind: str, pending_ids=None):
    """Request user approval for a pending agent action, returning (status, final_command).

    `status` is "approved", "denied" (rejected or timed out), or "hard_blocked" (the command
    matched a hard-block pattern and was never shown to the user). The SSE event type stays
    "command_approval"; `dangerous: true` is added when a run_command matches a dangerous pattern.
    When `pending_ids` is a list, the approval id is appended while waiting and removed on completion.

    This dialog is only reached in the "ask" mode; the "agent" mode auto-approves every command
    and never requests approval.
    """
    from tools.shell_tools import _matches_dangerous, _matches_hard_block

    dangerous = False
    if tool_name == "run_command":
        if _matches_hard_block(command):
            return "hard_blocked", command
        dangerous = _matches_dangerous(command)

    aid = uuid.uuid4().hex
    item = {"command": command, "description": description, "kind": kind,
            "event": threading.Event(), "approved": False, "final_command": None}
    with _APPROVALS_LOCK:
        _APPROVALS[aid] = item
    if pending_ids is not None:
        pending_ids.append(aid)
    event = {"type": "command_approval", "id": aid,
             "command": command, "description": description, "kind": kind}
    if dangerous:
        event["dangerous"] = True
    sse(wfile, event)
    item["event"].wait(APPROVAL_TIMEOUT)
    with _APPROVALS_LOCK:
        _APPROVALS.pop(aid, None)
        approved = item["approved"]
        final_command = item["final_command"] or command
    if pending_ids is not None:
        try:
            pending_ids.remove(aid)
        except ValueError:
            pass
    if not approved:
        return "denied", command
    return "approved", final_command


def _tool_permission(name: str, args: dict) -> str:
    """Return the effective "auto" or "ask" permission for a tool call.

    Unknown tools default to "auto" (they are rejected with an error at execution time); a
    callable permission (db_query) is invoked with the parsed arguments and falls back to "ask"
    if it raises.
    """
    perm = config.TOOL_PERMISSION.get(name, "auto")
    if callable(perm):
        try:
            return perm(args)
        except Exception:
            return "ask"
    return perm


def _approval_display(name: str, args: dict) -> tuple:
    """Return (command, description) display strings for a non-shell tool approval dialog."""
    if name == "write_file":
        rel = (args.get("path") or "").strip() or "(missing path)"
        mode = (args.get("mode") or "overwrite").strip().lower()
        if mode == "append":
            return "write_file --append %s" % rel, "Append the next piece to file: %s" % rel
        return "write_file %s" % rel, "Write file: %s" % rel
    if name == "edit_file":
        rel = (args.get("path") or "").strip() or "(missing path)"
        return "edit_file %s" % rel, "Edit file: %s" % rel
    if name == "delete_file":
        rel = (args.get("path") or "").strip() or "(missing path)"
        return "delete_file %s" % rel, "Delete file or folder: %s" % rel
    if name == "create_directory":
        rel = (args.get("path") or "").strip() or "(missing path)"
        return "create_directory %s" % rel, "Create folder: %s" % rel
    if name == "smoke_test":
        entry = (args.get("entry") or "").strip() or "(auto-detect)"
        return "smoke_test %s" % entry, "Run a smoke test on the project"
    return name, "Execute tool: %s" % name


# ------------------------- Agent mode: interactive questions -------------------------
_ASK_QUESTIONS = {}
_ASK_QUESTIONS_LOCK = threading.Lock()
ASK_USER_TIMEOUT = 300  # Maximum time to wait for the user's answer (seconds); timeout is treated as "skipped"


def _resolve_ask(aid, answer):
    """The user answers a pending interactive question, waking up the blocked Agent loop."""
    with _ASK_QUESTIONS_LOCK:
        item = _ASK_QUESTIONS.get(aid)
    if not item:
        return False
    item["answer"] = answer
    item["event"].set()
    return True


def tool_ask_user(args, wfile):
    """Ask the user an interactive question, blocking until the user clicks an option or types a custom answer."""
    question = (args.get("question") or "").strip()
    if not question:
        return "Error: missing question text for ask_user"
    raw_options = args.get("options") or []
    if isinstance(raw_options, str):
        raw_options = [raw_options]
    options = []
    for opt in raw_options:
        opt = str(opt).strip()
        if opt and opt not in options:
            options.append(opt)

    aid = uuid.uuid4().hex
    item = {"event": threading.Event(), "answer": None}
    with _ASK_QUESTIONS_LOCK:
        _ASK_QUESTIONS[aid] = item
    sse(wfile, {"type": "ask_user", "id": aid, "question": question, "options": options})
    item["event"].wait(ASK_USER_TIMEOUT)
    with _ASK_QUESTIONS_LOCK:
        _ASK_QUESTIONS.pop(aid, None)
        answer = item["answer"]

    if answer is None:
        return "The user did not answer the question (timed out or skipped)."
    answer = (answer or "").strip()
    if not answer:
        return "The user skipped the question."
    return "The user's answer: " + answer


# Backwards-compatible alias used by external callers / tests.
_agent_ask_user_tool = tool_ask_user
