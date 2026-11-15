# -*- coding: utf-8 -*-
"""Acceptance tests for the agent-loop hardening changes.

These tests exercise app.py directly (with a fake OpenAI client / fake SSE writer), so no
network or model service is required.
"""
import json
import threading
import time
from types import SimpleNamespace

import app
import common
import config


class FakeWfile:
    """Minimal wfile stand-in: accepts write()/flush() and stores raw bytes."""

    def __init__(self):
        self.chunks = []

    def write(self, b):
        self.chunks.append(b)

    def flush(self):
        pass


def make_sse_capture():
    """Return (events, fake_sse) where fake_sse replaces app.sse and records each event dict."""
    events = []

    def fake_sse(wfile, obj):
        events.append(obj)
        data = json.dumps(obj, ensure_ascii=False)
        try:
            wfile.write(("data: " + data + "\n\n").encode("utf-8"))
            wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    return events, fake_sse


def wait_for_event(events, type_, timeout=15.0):
    """Poll the captured events until one with the given type appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for e in events:
            if e.get("type") == type_:
                return e
        time.sleep(0.02)
    raise AssertionError("event %r was not emitted in time; got %r" % (type_, events))


def make_chunk(delta=None, finish_reason=None, usage=None):
    """Build a fake OpenAI streaming chunk."""
    choices = []
    if delta is not None or finish_reason is not None:
        choices = [SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    return SimpleNamespace(choices=choices, usage=usage)


def tool_call_chunk(tid, name, arguments):
    """Build a chunk whose delta carries a single tool call."""
    delta = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[
            SimpleNamespace(
                index=0,
                id=tid,
                function=SimpleNamespace(name=name, arguments=arguments),
            )
        ],
    )
    return make_chunk(delta=delta)


def make_client(streams, capture=None):
    """Return a fake client that pops one stream per create() call (optionally capturing kwargs)."""
    class FakeCompletions:
        def create(self, **kwargs):
            if capture is not None:
                capture.append(kwargs)
            return next(streams)

    class FakeClient:
        chat = SimpleNamespace(completions=FakeCompletions())

    return FakeClient()


def run_agent(tmp_path, monkeypatch, streams, backend="deepseek", capture=None, messages=None):
    """Run _run_agent synchronously with a fake client and return (events, messages)."""
    events, fake_sse = make_sse_capture()
    monkeypatch.setattr(app, "sse", fake_sse)
    if messages is None:
        messages = [{"role": "user", "content": "go"}]
    client = make_client(iter(streams), capture)
    app.Handler._run_agent(
        None, FakeWfile(), client, dict(config.BACKENDS[backend]), messages,
        1000, 0.7, 0.8, str(tmp_path), enable_thinking=True,
    )
    return events, messages


# --------------------------------------------------------------------------
# 1. DeepSeek + enable_thinking: reasoning_content is preserved on the
#    assistant tool-call message and the create kwargs carry thinking mode.
# --------------------------------------------------------------------------
def test_deepseek_agent_turn_preserves_reasoning_content(tmp_path, monkeypatch):
    reasoning_delta = SimpleNamespace(
        content=None, reasoning_content="I should list the files first",
        reasoning=None, tool_calls=None,
    )
    final_delta = SimpleNamespace(
        content="All done", reasoning_content=None, reasoning=None, tool_calls=None,
    )
    streams = [
        [make_chunk(delta=reasoning_delta),
         tool_call_chunk("call_1", "list_files", '{"path": ""}'),
         make_chunk(finish_reason="tool_calls")],
        [make_chunk(delta=final_delta), make_chunk(finish_reason="stop")],
    ]

    capture = []
    events, messages = run_agent(
        tmp_path, monkeypatch, streams, backend="deepseek", capture=capture,
        messages=[{"role": "user", "content": "list files"}],
    )

    # The create kwargs must enable DeepSeek thinking mode.
    assert capture, "expected at least one create() call"
    assert capture[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert capture[0]["reasoning_effort"] == config.BACKENDS["deepseek"]["reasoning_effort"]

    # The outgoing assistant message that carries tool_calls must echo reasoning_content.
    assistant_tool_calls = [
        m for m in messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert assistant_tool_calls, "expected an assistant message with tool_calls"
    assert assistant_tool_calls[0].get("reasoning_content") == "I should list the files first"

    # No error event: the turn completed cleanly (no 400).
    assert not any(e.get("type") == "error" for e in events)
    assert any(e.get("type") == "done" for e in events)


# --------------------------------------------------------------------------
# 2. write_file without a prior read_file triggers the approval dialog with
#    kind: "tool".
# --------------------------------------------------------------------------
def test_write_file_requires_approval_kind_tool(tmp_path, monkeypatch):
    events, fake_sse = make_sse_capture()
    monkeypatch.setattr(app, "sse", fake_sse)

    result_holder = {}
    wfile = FakeWfile()

    def run():
        result_holder["result"] = app._agent_execute_approved_tool(
            "write_file", {"path": "x.txt", "content": "hi"}, tmp_path, wfile)

    t = threading.Thread(target=run)
    t.start()
    ev = wait_for_event(events, "command_approval")
    try:
        assert ev["kind"] == "tool"
        assert app._resolve_approval(ev["id"], "approve", None)
        t.join(timeout=15)
    finally:
        if t.is_alive():
            app._resolve_approval(ev["id"], "deny", None)
            t.join(timeout=5)

    assert not t.is_alive()
    assert result_holder["result"].startswith("Wrote file:")
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hi"


# --------------------------------------------------------------------------
# 3. run_command("rm -rf /") is hard-blocked with no dialog.
# --------------------------------------------------------------------------
def test_run_command_rm_rf_root_hard_blocked(tmp_path, monkeypatch):
    events, fake_sse = make_sse_capture()
    monkeypatch.setattr(app, "sse", fake_sse)
    result = app._agent_execute_command_tool(
        "run_command", {"command": "rm -rf /"}, tmp_path, FakeWfile())
    assert result == "Error: this command matches a hard-block pattern and will never be executed."
    assert not any(e.get("type") == "command_approval" for e in events)


# --------------------------------------------------------------------------
# 4. run_command("sudo apt install x") shows the dialog with dangerous: true.
# --------------------------------------------------------------------------
def test_run_command_sudo_shows_dialog_dangerous(tmp_path, monkeypatch):
    events, fake_sse = make_sse_capture()
    monkeypatch.setattr(app, "sse", fake_sse)

    result_holder = {}
    wfile = FakeWfile()

    def run():
        result_holder["result"] = app._agent_execute_command_tool(
            "run_command", {"command": "sudo apt install x"}, tmp_path, wfile)

    t = threading.Thread(target=run)
    t.start()
    ev = wait_for_event(events, "command_approval")
    try:
        assert ev.get("dangerous") is True
        app._resolve_approval(ev["id"], "deny", None)
        t.join(timeout=15)
    finally:
        if t.is_alive():
            app._resolve_approval(ev["id"], "deny", None)
            t.join(timeout=5)

    assert result_holder["result"].startswith("The user rejected the command")


# --------------------------------------------------------------------------
# 5. A run_command emitting 5 MB of output is truncated to
#    COMMAND_OUTPUT_MAX_BYTES keeping the tail, and the loop still completes.
# --------------------------------------------------------------------------
def test_run_command_output_truncated_tail_and_loop_completes(tmp_path, monkeypatch):
    script = tmp_path / "gen.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('BEGIN_MARKER\\n')\n"
        "sys.stdout.write('A' * (5 * 1024 * 1024))\n"
        "sys.stdout.write('END_MARKER\\n')\n",
        encoding="utf-8",
    )
    command = '"%s" "%s"' % (app.get_current_python(), script)

    streams = [
        [tool_call_chunk("call_r", "run_command", json.dumps({"command": command})),
         make_chunk(finish_reason="tool_calls")],
        [make_chunk(delta=SimpleNamespace(content="Done", reasoning_content=None,
                                          reasoning=None, tool_calls=None)),
         make_chunk(finish_reason="stop")],
    ]

    events, fake_sse = make_sse_capture()
    monkeypatch.setattr(app, "sse", fake_sse)
    client = make_client(iter(streams))
    messages = [{"role": "user", "content": "run it"}]

    def run():
        app.Handler._run_agent(
            None, FakeWfile(), client, dict(config.BACKENDS["deepseek"]), messages,
            1000, 0.7, 0.8, str(tmp_path), enable_thinking=True)

    t = threading.Thread(target=run)
    t.start()
    ev = wait_for_event(events, "command_approval")
    try:
        app._resolve_approval(ev["id"], "approve", None)
        t.join(timeout=30)
    finally:
        if t.is_alive():
            app._resolve_approval(ev["id"], "deny", None)
            t.join(timeout=5)

    tool_results = [e for e in events
                    if e.get("type") == "tool_result" and e.get("name") == "run_command"]
    assert tool_results, "expected a run_command tool_result"
    out = tool_results[0]["result"]
    # Tail kept: the end marker survives, the start marker is gone.
    assert "END_MARKER" in out
    assert "BEGIN_MARKER" not in out
    assert "[truncated" in out
    assert len(out.encode("utf-8")) <= config.COMMAND_OUTPUT_MAX_BYTES + 128

    # The message fed back to the model is capped to TOOL_RESULT_MAX_BYTES.
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs
    assert len(tool_msgs[-1]["content"].encode("utf-8")) <= config.TOOL_RESULT_MAX_BYTES + 128

    assert any(e.get("type") == "done" for e in events)


# --------------------------------------------------------------------------
# 6. read_file on a 50 MB file returns the "too large" error pointing at
#    offset and limit.
# --------------------------------------------------------------------------
def test_read_file_too_large_points_at_offset_and_limit(tmp_path):
    big = tmp_path / "big.bin"
    with open(big, "wb") as f:
        f.seek(50 * 1024 * 1024 - 1)
        f.write(b"\0")
    result = app._agent_execute_tool("read_file", {"path": "big.bin"}, tmp_path)
    assert "too large" in result
    assert "offset" in result
    assert "limit" in result


# --------------------------------------------------------------------------
# 7. finish_reason == "length" with pending tool_calls executes no tool.
# --------------------------------------------------------------------------
def test_finish_reason_length_does_not_execute_tool(tmp_path, monkeypatch):
    # Truncated arguments: missing the closing brace.
    tc_delta = SimpleNamespace(
        content=None, reasoning_content=None, reasoning=None,
        tool_calls=[
            SimpleNamespace(
                index=0, id="call_w",
                function=SimpleNamespace(name="write_file", arguments='{"path": "oops.txt"'),
            )
        ],
    )
    final_delta = SimpleNamespace(content="Recovered", reasoning_content=None,
                                  reasoning=None, tool_calls=None)
    streams = [
        [make_chunk(delta=tc_delta), make_chunk(finish_reason="length")],
        [make_chunk(delta=final_delta), make_chunk(finish_reason="stop")],
    ]

    events, messages = run_agent(
        tmp_path, monkeypatch, streams,
        messages=[{"role": "user", "content": "write a file"}],
    )

    assert not (tmp_path / "oops.txt").exists()
    contents = [m.get("content") for m in messages]
    assert any("truncated mid-JSON" in (c or "") for c in contents)
    assert any("tool not executed" in (c or "") for c in contents)
    assert any(e.get("type") == "done" for e in events)


# --------------------------------------------------------------------------
# 8. Consecutive 429 responses end in a clean SSE error event, not a stack
#    trace.
# --------------------------------------------------------------------------
def test_repeated_429s_end_in_clean_error_event(tmp_path, monkeypatch):
    import httpx2

    events, fake_sse = make_sse_capture()
    monkeypatch.setattr(app, "sse", fake_sse)
    monkeypatch.setattr(app.time, "sleep", lambda _s: None)  # skip backoff

    calls = []

    class Always429Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise app.APIStatusError(
                "429",
                response=httpx2.Response(429, request=httpx2.Request("POST", "http://t")),
                body=None,
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Always429Completions()))
    messages = [{"role": "user", "content": "hi"}]
    app.Handler._run_agent(
        None, FakeWfile(), client, dict(config.BACKENDS["deepseek"]), messages,
        1000, 0.7, 0.8, str(tmp_path), enable_thinking=True)

    # max_attempts=4: the final attempt re-raises instead of retrying.
    assert len(calls) == 4
    error_events = [e for e in events if e.get("type") == "error"]
    assert error_events
    assert "429" in error_events[0]["message"]


# --------------------------------------------------------------------------
# 9. A repeated (tool_name, arguments) tuple three times injects the
#    dead-loop system message once and does not abort.
# --------------------------------------------------------------------------
def test_dead_loop_injects_system_message_once(tmp_path, monkeypatch):
    streams = []
    for i in range(3):
        streams.append(
            [tool_call_chunk("call_%d" % i, "list_files", '{"path": ""}'),
             make_chunk(finish_reason="tool_calls")]
        )
    final_delta = SimpleNamespace(content="Stopping now", reasoning_content=None,
                                  reasoning=None, tool_calls=None)
    streams.append([make_chunk(delta=final_delta), make_chunk(finish_reason="stop")])

    events, messages = run_agent(tmp_path, monkeypatch, streams)

    dead_loops = [
        m for m in messages
        if m.get("role") == "system" and "repeating the same action" in (m.get("content") or "")
    ]
    assert len(dead_loops) == 1
    assert any(e.get("type") == "done" for e in events)


# --------------------------------------------------------------------------
# 10. list_files on a directory containing node_modules returns project files
#     only.
# --------------------------------------------------------------------------
def test_list_files_skips_node_modules(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "cache.pyc").write_bytes(b"\x00\x00")

    result = app._agent_execute_tool("list_files", {"path": ""}, tmp_path)

    assert "README.md" in result
    assert "main.py" in result
    assert "node_modules" not in result
    assert "pkg.js" not in result
    assert "cache.pyc" not in result


# --------------------------------------------------------------------------
# 11. finish_reason == "length" with tool_calls keeps the assistant tool_calls
#     message so the following tool error messages remain a valid sequence.
# --------------------------------------------------------------------------
def test_length_truncation_keeps_assistant_tool_calls(tmp_path, monkeypatch):
    tc_delta = SimpleNamespace(
        content=None, reasoning_content=None, reasoning=None,
        tool_calls=[
            SimpleNamespace(
                index=0, id="call_x",
                function=SimpleNamespace(name="write_file", arguments='{"path": "oops.txt"'),
            )
        ],
    )
    final_delta = SimpleNamespace(content="Recovered", reasoning_content=None,
                                  reasoning=None, tool_calls=None)
    streams = [
        [make_chunk(delta=tc_delta), make_chunk(finish_reason="length")],
        [make_chunk(delta=final_delta), make_chunk(finish_reason="stop")],
    ]

    events, messages = run_agent(
        tmp_path, monkeypatch, streams,
        messages=[{"role": "user", "content": "write a file"}],
    )

    truncated = [
        m for m in messages
        if m.get("role") == "assistant" and m.get("tool_calls")
        and "truncated mid-JSON" in (m.get("content") or "")
    ]
    assert truncated, "expected an assistant tool_calls message noting truncation"
    assert [tc["id"] for tc in truncated[0]["tool_calls"]] == ["call_x"]

    # Every assistant tool_calls message must be immediately followed by matching tool replies.
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            for off, cid in enumerate(ids, 1):
                reply = messages[i + off]
                assert reply.get("role") == "tool", "tool reply must follow tool_calls"
                assert reply.get("tool_call_id") == cid


# --------------------------------------------------------------------------
# 12. run_tests with pytest available and no collected tests returns the
#     normalized exit-code-5 verdict.
# --------------------------------------------------------------------------
def test_run_tests_no_tests_collected(tmp_path, monkeypatch):
    events, fake_sse = make_sse_capture()
    monkeypatch.setattr(app, "sse", fake_sse)

    result_holder = {}
    wfile = FakeWfile()

    def run():
        result_holder["result"] = app._agent_execute_command_tool(
            "run_tests", {"path": "", "timeout": 30}, tmp_path, wfile)

    t = threading.Thread(target=run)
    t.start()
    ev = wait_for_event(events, "command_approval")
    try:
        assert ev["kind"] == "run_tests"
        app._resolve_approval(ev["id"], "approve", None)
        t.join(timeout=60)
    finally:
        if t.is_alive():
            app._resolve_approval(ev["id"], "deny", None)
            t.join(timeout=5)

    assert not t.is_alive()
    assert "exit code 5" in result_holder["result"]


# --------------------------------------------------------------------------
# 13. _repair_tool_call_order synthesizes placeholder tool responses when a
#     session was interrupted before all tool calls completed.
# --------------------------------------------------------------------------
def test_repair_tool_call_order_synthesizes_missing():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "list_files", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    common._repair_tool_call_order(messages)

    ids = [m.get("tool_call_id") for m in messages if m.get("role") == "tool"]
    assert ids == ["a", "b"]
    assert "tool result unavailable" in messages[-1]["content"]


# --------------------------------------------------------------------------
# 14. message_tokens counts tool_call names and arguments.
# --------------------------------------------------------------------------
def test_message_tokens_counts_tool_calls():
    with_args = common.message_tokens({
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "write_file", "arguments": '{"content": "hello world"}'}}],
    })
    bare = common.message_tokens({"role": "assistant", "content": None})
    assert with_args > bare
