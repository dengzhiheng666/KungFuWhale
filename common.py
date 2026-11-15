# -*- coding: utf-8 -*-
"""Shared runtime state and cross-cutting helpers.

Holds module-level singletons (concurrency semaphore, backend clients, current interpreter,
read limit, agent max iters), token estimation, context compression, and the SSE streaming
helpers, so `agent.py` and `server.py` can both use them without circular imports.
"""
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from openai import APIConnectionError, APITimeoutError, APIStatusError, OpenAI

import config

if getattr(sys, "frozen", False):
    # When bundled with PyInstaller, keep mutable state next to the executable and prefer a
    # user-supplied index.html there, falling back to the copy embedded in the archive.
    BASE_DIR = Path(sys.executable).resolve().parent
    _BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    INDEX_FILE = BASE_DIR / "index.html" if (BASE_DIR / "index.html").is_file() else _BUNDLE_DIR / "index.html"
else:
    BASE_DIR = Path(__file__).resolve().parent
    INDEX_FILE = BASE_DIR / "index.html"

# Request body limit / concurrency slots
MAX_BODY_BYTES = 2 * 1024 * 1024
_GEN_SLOTS = threading.Semaphore(config.MAX_CONCURRENT)

# Per-read file size limit (bytes): prevents the frontend/Agent from blowing up memory
# when opening extremely large files. The initial value comes from config.MAX_READ_BYTES
# and can be adjusted at runtime via /api/settings.
_READ_LIMIT_LOCK = threading.Lock()
_read_limit = config.MAX_READ_BYTES


def get_read_limit():
    with _READ_LIMIT_LOCK:
        return _read_limit


def set_read_limit(value):
    global _read_limit
    with _READ_LIMIT_LOCK:
        _read_limit = int(value)


def _truncate_for_model(text: str, limit: int, keep: str = "head") -> str:
    """Truncate `text` to at most `limit` bytes before it reaches the model.

    Keeps the head (default) or the tail when keep="tail", then appends a byte-counted
    truncation note. The input is str; multi-byte characters split at the byte boundary are
    replaced rather than raising.
    """
    if not isinstance(text, str):
        text = str(text)
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    if keep == "tail":
        kept = data[-limit:]
    else:
        kept = data[:limit]
    snippet = kept.decode("utf-8", errors="replace")
    return snippet + "\n... [truncated %d bytes]" % (len(data) - limit)


# Maximum attempts in Agent mode (adjustable at runtime; initial value from config.AGENT_MAX_ITERS).
_AGENT_MAX_ITERS_LOCK = threading.Lock()
_agent_max_iters = config.AGENT_MAX_ITERS


def get_agent_max_iters():
    with _AGENT_MAX_ITERS_LOCK:
        return _agent_max_iters


def set_agent_max_iters(value):
    global _agent_max_iters
    with _AGENT_MAX_ITERS_LOCK:
        _agent_max_iters = int(value)


# ------------------------- Output cut-off continuation -------------------------
# When a reply is cut off by the output token limit (finish_reason="length"), the partial answer
# is kept and the model is asked to continue in smaller pieces. Both values are adjustable at
# runtime via /api/settings.
_CONTINUE_LOCK = threading.Lock()
_continue_enabled = config.CONTINUE_ENABLED
_continue_max_rounds = config.CONTINUE_MAX_ROUNDS


def get_continue_enabled():
    with _CONTINUE_LOCK:
        return _continue_enabled


def set_continue_enabled(value):
    global _continue_enabled
    with _CONTINUE_LOCK:
        _continue_enabled = bool(value)


def get_continue_max_rounds():
    with _CONTINUE_LOCK:
        return _continue_max_rounds


def set_continue_max_rounds(value):
    global _continue_max_rounds
    with _CONTINUE_LOCK:
        _continue_max_rounds = int(value)


# ------------------------- Piecewise file editing (chunk size) -------------------------
# Global size of a single write/edit piece (in characters). The Agent is told to never emit more
# than this per tool call, so a small model is not cut off by its output token limit. Whenever a
# piece fails, the size is shrunk automatically (halved, floored at config.EDIT_CHUNK_CHARS_MIN).
_EDIT_CHUNK_LOCK = threading.Lock()
_edit_chunk_chars = config.EDIT_CHUNK_CHARS


def get_edit_chunk_chars():
    with _EDIT_CHUNK_LOCK:
        return _edit_chunk_chars


def set_edit_chunk_chars(value):
    global _edit_chunk_chars
    try:
        value = int(value)
    except (TypeError, ValueError):
        return get_edit_chunk_chars()
    value = max(config.EDIT_CHUNK_CHARS_MIN, min(config.EDIT_CHUNK_CHARS_MAX, value))
    with _EDIT_CHUNK_LOCK:
        _edit_chunk_chars = value
    return value


def shrink_edit_chunk_chars():
    """Halve the current piece size (floored) and return the new value.

    Called automatically when a write/edit piece fails; returns the current value when the floor
    has already been reached so callers can always report a usable limit.
    """
    global _edit_chunk_chars
    with _EDIT_CHUNK_LOCK:
        shrunk = int(_edit_chunk_chars * config.EDIT_CHUNK_SHRINK_FACTOR)
        _edit_chunk_chars = max(config.EDIT_CHUNK_CHARS_MIN, shrunk)
        return _edit_chunk_chars


# ------------------------- Memory file (remember the last selected working folder) -------------------------
MEMORY_FILE = BASE_DIR / ".memory.json"


def _load_memory():
    """Read the memory file and return a dict; return an empty dict if the file is missing or corrupted."""
    try:
        if MEMORY_FILE.exists():
            data = json.loads(MEMORY_FILE.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_memory(data):
    """Atomically write the memory to disk (write a temp file first, then replace, to avoid corruption from partial writes)."""
    try:
        tmp = MEMORY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(MEMORY_FILE)
    except Exception:
        pass


def get_remembered_folder():
    """Return the last remembered working folder path; return an empty string if none or invalid."""
    folder = _load_memory().get("folder")
    return folder if isinstance(folder, str) else ""


def set_remembered_folder(folder):
    """Save the working folder path to the memory file."""
    data = _load_memory()
    data["folder"] = folder or ""
    _save_memory(data)


# Multi-backend clients: DeepSeek models share one base URL and one API key.
# The OpenAI SDK requires a non-empty api_key at construction time, so a placeholder keeps the
# client constructible before the user enters their key. A DeepSeek request with no real key is
# rejected in server.py with a clear error before the client is used.
def build_client(cfg):
    return OpenAI(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"] or "placeholder",
        timeout=config.REQUEST_TIMEOUT,
    )


clients = {name: build_client(cfg) for name, cfg in config.BACKENDS.items()}


def set_backend_api_key(name, key):
    """Set (or clear) the API key for a backend at runtime and rebuild its client(s).

    DeepSeek uses a single shared API key across all of its models, so setting the key for one
    model applies it to every model sharing the same base URL. Empty strings clear the key.
    Returns True when the backend ends up with a non-empty key.
    """
    if name not in config.BACKENDS:
        raise ValueError("Unknown backend: " + str(name))
    value = (key or "").strip()
    base_url = config.BACKENDS[name].get("base_url")
    targets = [n for n, c in config.BACKENDS.items() if c.get("base_url") == base_url] or [name]
    for n in targets:
        config.BACKENDS[n]["api_key"] = value
        clients[n] = build_client(config.BACKENDS[n])
    return bool(value)

# ------------------------- Python run support -------------------------
IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WIN else 0

# Windows Mandatory Integrity Control (MIC) RIDs. A process launched at "Low" integrity
# (Mandatory Label\Low, e.g. from a sandboxed host) can read but not write/delete files or
# folders labelled Medium or higher — so build tools that write into the working directory
# (PyInstaller, git commit, ...) fail with access denied.
_WINDOWS_INTEGRITY_RIDS = {
    0x00000000: "Untrusted",
    0x00001000: "Low",
    0x00002000: "Medium",
    0x00002100: "Medium Plus",
    0x00003000: "High",
    0x00004000: "System",
    0x00005000: "Protected Process",
}


def get_windows_integrity_level():
    """Return (name, rid) of the current process's Windows mandatory integrity level.

    Returns None on non-Windows platforms or when the process token cannot be queried.
    """
    if not IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 wintypes.LPVOID, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        TOKEN_QUERY = 0x0008
        TokenIntegrityLevel = 25

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY,
                                         ctypes.byref(token)):
            return None
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, TokenIntegrityLevel, buf, size,
                                                ctypes.byref(size)):
                return None
            # TOKEN_MANDATORY_LABEL starts with SID_AND_ATTRIBUTES.Label -> PSID (pointer-sized).
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid:
                return None
            # SID layout: [0]=revision, [1]=sub-authority count, [2..7]=authority, then DWORDs.
            sub_count = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))[1]
            last_rid = ctypes.cast(sid + 8 + (sub_count - 1) * 4,
                                   ctypes.POINTER(wintypes.DWORD))[0]
            return _WINDOWS_INTEGRITY_RIDS.get(last_rid, "Unknown"), last_rid
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return None

# Current interpreter: default = the interpreter running this service (i.e., the one the user selected in the IDE).
_CURRENT_PY_LOCK = threading.Lock()
_current_python = sys.executable


def get_current_python():
    with _CURRENT_PY_LOCK:
        return _current_python


def set_current_python(path):
    global _current_python
    with _CURRENT_PY_LOCK:
        _current_python = path


# ------------------------- Token estimation (local fallback) -------------------------
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text):
    """Roughly estimate token count: roughly 1 token per Chinese character, 4 characters per token for English."""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    non_cjk = _CJK_RE.sub(" ", text)
    non_cjk_chars = len(re.sub(r"\s+", "", non_cjk))
    return cjk + int(round(non_cjk_chars / 4.0)) + 1


def message_tokens(message):
    """Estimate tokens for a single message: content, tool-call payloads, and role/format overhead."""
    n = estimate_tokens(message.get("content") or "")
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        n += estimate_tokens(fn.get("name") or "")
        n += estimate_tokens(fn.get("arguments") or "")
    return n + 4


def build_usage(messages):
    """Estimate usage locally (fallback when the server does not return usage)."""
    prompt = sum(message_tokens(m) for m in messages)
    return {"prompt_tokens": prompt, "completion_tokens": 0, "total_tokens": prompt}


def build_usage_breakdown(messages, tools=None):
    """Estimate the context usage broken down by category.

    Returns {system, definitions, tool_calls, conversation, total} in estimated tokens.
    - system: the injected system prompt(s)
    - definitions: the JSON size of the tool/function schemas
    - tool_calls: tool-call arguments plus tool-result contents
    - conversation: user + assistant message text
    """
    system = 0
    tool_calls = 0
    conversation = 0
    for m in messages:
        role = m.get("role")
        if role == "system":
            system += message_tokens(m)
        elif role == "user":
            conversation += estimate_tokens(m.get("content") or "") + 4
        elif role == "assistant":
            conversation += estimate_tokens(m.get("content") or "")
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                tool_calls += estimate_tokens(fn.get("name") or "") + estimate_tokens(fn.get("arguments") or "")
        elif role == "tool":
            tool_calls += estimate_tokens(m.get("content") or "") + 4
    definitions = 0
    if tools:
        try:
            definitions = estimate_tokens(json.dumps(tools, ensure_ascii=False))
        except Exception:
            definitions = 0
    total = system + definitions + tool_calls + conversation
    return {
        "system": system,
        "definitions": definitions,
        "tool_calls": tool_calls,
        "conversation": conversation,
        "total": total,
    }


# ------------------------- Context compression (history summary) -------------------------
def _context_window(cfg):
    """Return the maximum context window (in tokens) of the backend model."""
    return int(cfg.get("context_window") or config.DEFAULT_CONTEXT_WINDOW)


def _summarize_history(client, cfg, messages, zh=False):
    """Call the same backend to compress-summarize historical messages; returns None on failure (in which case old messages are simply dropped)."""
    text = "\n".join(
        "%s: %s" % (m.get("role"), (m.get("content") or "").strip())
        for m in messages
        if (m.get("content") or "").strip()
    )
    if not text.strip():
        return None
    # When the summary input is too long, keep the head and tail to avoid exceeding the summarization model's own limit
    limit = 24000
    if len(text) > limit:
        head = int(limit * 0.6)
        tail = int(limit * 0.4)
        text = text[:head] + "\n...(middle content omitted)...\n" + text[-tail:]
    try:
        resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": config.SUMMARY_SYSTEM_PROMPT_ZH if zh else config.SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=config.SUMMARY_TEMPERATURE,
            max_tokens=config.SUMMARY_MAX_TOKENS,
            stream=False,
        )
        summary = (resp.choices[0].message.content or "").strip()
        return summary or None
    except Exception:
        return None


def _split_turns(messages):
    """Split the conversation into turns at user boundaries; system messages are kept separately at the front."""
    system, rest = [], []
    for m in messages:
        (system if m.get("role") == "system" else rest).append(m)
    turns, cur = [], []
    for m in rest:
        if m.get("role") == "user" and cur:
            turns.append(cur)
            cur = []
        cur.append(m)
    if cur:
        turns.append(cur)
    return system, turns


def _compress_history(client, cfg, messages, zh=False):
    """Compress history: keep the most recent turns intact and summarize earlier history into a single message."""
    system, turns = _split_turns(messages)
    keep_turns = max(1, config.CONTEXT_KEEP_TURNS)
    if len(turns) <= keep_turns:
        return messages
    dropped = [m for t in turns[:-keep_turns] for m in t]
    kept = [m for t in turns[-keep_turns:] for m in t]
    summary = _summarize_history(client, cfg, dropped, zh=zh)
    out = list(system)
    if summary:
        prefix = "[早前的对话摘要]\n" if zh else "[Earlier conversation summary]\n"
        out.append({"role": "system", "content": prefix + summary})
    out.extend(kept)
    return out


def _repair_tool_call_order(messages):
    """Move any messages interleaved between an assistant tool_calls message and its tool
    responses to after those responses (in place).

    OpenAI-compatible APIs reject a request when an assistant message carrying `tool_calls`
    is not immediately followed by the `tool` messages answering each call id. Shift such
    interleaved messages (e.g. an injected system reminder) past the tool results while
    preserving the list identity and everything else verbatim. When the list ends before a
    tool call was answered (e.g. a session persisted mid-tool-execution), a placeholder tool
    response is synthesized so the message sequence stays valid for the next request.
    """
    out = []
    i, n = 0, len(messages)
    while i < n:
        m = messages[i]
        out.append(m)
        i += 1
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
            buffered = []
            while i < n and pending:
                cur = messages[i]
                if cur.get("role") == "tool" and cur.get("tool_call_id") in pending:
                    pending.discard(cur["tool_call_id"])
                    out.append(cur)
                else:
                    buffered.append(cur)
                i += 1
            for missing in sorted(pending):
                out.append({
                    "role": "tool",
                    "tool_call_id": missing,
                    "content": ("Error: tool result unavailable (the session was interrupted "
                                "before this tool call completed)."),
                })
            out.extend(buffered)
    messages[:] = out
    return messages


def _fit_context(client, cfg, messages, max_tokens, zh=False):
    """Compress history when approaching the context limit and tighten max_tokens according to remaining space.

    Returns (messages, max_tokens). Both compression and tightening are best-effort; keep as-is on failure.
    """
    context_window = _context_window(cfg)
    prompt = build_usage(messages)["prompt_tokens"]

    if prompt >= int(context_window * config.CONTEXT_USAGE_THRESHOLD):
        messages = _compress_history(client, cfg, messages, zh=zh)
        prompt = build_usage(messages)["prompt_tokens"]

    # Reserve room for the reply: prompt + max_tokens must not exceed the context window (leave a 128-token margin)
    room = context_window - prompt - 128
    if room < 1:
        max_tokens = 1
    elif max_tokens > room:
        max_tokens = room
    return messages, max_tokens


def _num(payload, key, default, cast):
    """Safely read a numeric parameter: fall back to the default when missing/empty, while preserving legal values such as 0.

    Note that `payload.get(key) or default` must not be used, otherwise 0 / 0.0 would be mistaken for missing.
    """
    v = payload.get(key)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


# ------------------------- SSE helpers -------------------------
def _create_with_retry(client, kwargs, max_attempts=4):
    """Call client.chat.completions.create with retry and exponential backoff.

    Retries only APIStatusError with status_code in (429, 500, 502, 503, 504) and
    APITimeoutError / APIConnectionError; any other error (including other 4xx) is re-raised
    immediately. Backoff is 2**attempt seconds plus uniform(0, 0.5) jitter, and the final
    attempt re-raises the original error.
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except (APIStatusError, APITimeoutError, APIConnectionError) as e:
            if isinstance(e, APIStatusError) and e.status_code not in (429, 500, 502, 503, 504):
                raise
            last_error = e
        if attempt < max_attempts:
            time.sleep((2 ** attempt) + random.uniform(0, 0.5))
    raise last_error


# Parameters dropped (in order) when a backend rejects the request as unsupported, so the same
# code path works even if the endpoint rejects optional parameters.
_COMPAT_DROP_STEPS = (
    ("extra_body",),
    ("top_p", "temperature"),
    ("stream_options",),
)


def _create_compatible(client, kwargs, max_attempts=4):
    """Create a completion, degrading the request when a backend rejects optional parameters.

    Many OpenAI-compatible servers answer 400 for parameters they do not implement (extra_body
    vendor knobs, top_p/temperature on some推理 endpoints, stream_options, ...). On such a 400 the
    request is retried with those parameters removed, from the most vendor-specific to the most
    basic one, so a plain (model, messages, max_tokens, stream) request is always attempted
    before giving up. Non-400 errors keep the retry/backoff behaviour of `_create_with_retry`.
    """
    try:
        return _create_with_retry(client, kwargs, max_attempts=max_attempts)
    except APIStatusError as e:
        if getattr(e, "status_code", None) != 400:
            raise
        fallback = dict(kwargs)
        for drop in _COMPAT_DROP_STEPS:
            if not any(k in fallback for k in drop):
                continue
            for k in drop:
                fallback.pop(k, None)
            try:
                return client.chat.completions.create(**fallback)
            except APIStatusError:
                continue  # Try the next, even weaker request before reporting failure.
        raise


def _raw_sse(wfile, obj):
    data = json.dumps(obj, ensure_ascii=False)
    wfile.write(f"data: {data}\n\n".encode("utf-8"))
    wfile.flush()


def sse(wfile, obj):
    """Emit an SSE event.

    Delegates to `app.sse` when that has been monkeypatched (e.g. by the acceptance tests),
    otherwise writes directly. Keeping the indirection lets the modularized code stay patchable
    through `app.sse` exactly as before.
    """
    import sys
    app = sys.modules.get("app") or sys.modules.get("__main__")
    fn = getattr(app, "sse", None) if app is not None else None
    if fn is not None and fn is not sse:
        fn(wfile, obj)
        return
    _raw_sse(wfile, obj)


def _consume_stream(stream, wfile, start_time, tool_base=0, usage_acc=None):
    """Consume a streaming response: write SSE in real time and aggregate incremental content.

    Returns (completion_text, reasoning_text, tool_acc, usage, first_token_time, finish_reason).
    - When usage_acc is passed, each official usage chunk is accumulated into it; otherwise usage is the last chunk's usage.
    - tool_acc is {local index: {"id","name","arguments"}}, where the index does not include the tool_base offset.
    - finish_reason is taken from the last chunk that carries one (None when the backend reports none).
    """
    completion_text = ""
    reasoning_text = ""
    tool_acc = {}
    usage = None
    first_token_time = None
    finish_reason = None

    for chunk in stream:
        if not chunk.choices:
            # End chunk: may carry official usage
            if getattr(chunk, "usage", None):
                u = chunk.usage
                piece = {
                    "prompt_tokens": getattr(u, "prompt_tokens", 0),
                    "completion_tokens": getattr(u, "completion_tokens", 0),
                    "total_tokens": getattr(u, "total_tokens", 0),
                    "prompt_cache_hit_tokens": getattr(u, "prompt_cache_hit_tokens", 0),
                    "prompt_cache_miss_tokens": getattr(u, "prompt_cache_miss_tokens", 0),
                }
                if usage_acc is not None:
                    for k in usage_acc:
                        usage_acc[k] += piece[k]
                else:
                    usage = piece
            continue

        delta = chunk.choices[0].delta

        # Finish reason is only present on the final chunk of a turn.
        fr = getattr(chunk.choices[0], "finish_reason", None)
        if fr:
            finish_reason = fr

        # Reasoning process (Chain of Thought) real-time streaming output
        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
        if reasoning:
            if first_token_time is None:
                first_token_time = time.monotonic()
            reasoning_text += reasoning
            sse(wfile, {"type": "reasoning", "content": reasoning})

        # Tool calls (e.g. python / terminal): aggregate incremental arguments by index
        for tc in getattr(delta, "tool_calls", None) or []:
            if first_token_time is None:
                first_token_time = time.monotonic()
            idx = getattr(tc, "index", 0) or 0
            item = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if getattr(tc, "id", None):
                item["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn:
                if getattr(fn, "name", None):
                    item["name"] = fn.name
                if getattr(fn, "arguments", None):
                    item["arguments"] += fn.arguments
            sse(wfile, {"type": "tool_call", "index": tool_base + idx,
                        "name": item["name"], "arguments": item["arguments"]})

        content = getattr(delta, "content", None)
        if content:
            if first_token_time is None:
                first_token_time = time.monotonic()
            completion_text += content
            sse(wfile, {"type": "delta", "content": content})

    return completion_text, reasoning_text, tool_acc, usage, first_token_time, finish_reason


def _emit_usage(wfile, usage, start_time, first_token_time):
    """Emit a unified usage and speed statistics event. start_time / first_token_time must be monotonic timestamps."""
    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    first_token_ms = int((first_token_time - start_time) * 1000) if first_token_time else None
    elapsed_s = elapsed_ms / 1000.0
    tokens_per_second = round(usage["completion_tokens"] / elapsed_s, 1) if elapsed_s > 0 else 0.0
    sse(wfile, {
        "type": "usage",
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens", 0),
        "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens", 0),
        "elapsed_ms": elapsed_ms,
        "first_token_ms": first_token_ms,
        "tokens_per_second": tokens_per_second,
    })
