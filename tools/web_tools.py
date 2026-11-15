# -*- coding: utf-8 -*-
"""Agent web tools: search the web / fetch a page via the DeepSeek Search & Fetch APIs.

These are read-only tools the agent can call when it needs current or external information.
They use the same DeepSeek API key and base URL as the chat backend.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

import config


def _api_key():
    cfg = config.BACKENDS.get("deepseek") or {}
    return (cfg.get("api_key") or "").strip() or (config.DEEPSEEK_API_KEY or "").strip()


def _base_url():
    cfg = config.BACKENDS.get("deepseek") or {}
    return (cfg.get("base_url") or config.DEEPSEEK_BASE_URL or "https://api.deepseek.com").rstrip("/")


def _post(path, body, timeout=20):
    key = _api_key()
    if not key:
        raise ValueError("DeepSeek API key is not set")
    req = urllib.request.Request(
        _base_url() + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def tool_web_search(args):
    """Search the web and return structured results (title / url / summary)."""
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: web_search requires a non-empty query"
    try:
        count = max(1, min(50, int(args.get("count") or 5)))
    except (TypeError, ValueError):
        count = 5
    need_content = bool(args.get("need_content"))
    try:
        data = _post("/v1/tools/search", {
            "query": query[:128],
            "count": count,
            "need_content": need_content,
        })
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        return "Error: web search failed (HTTP %s): %s" % (e.code, detail)
    except Exception as e:
        return "Error: web search failed: %s" % e
    results = data.get("results") or []
    if not results:
        return "No results found for query: " + query
    lines = ["Web search results for: " + query]
    for r in results:
        title = r.get("title") or ""
        url = r.get("url") or ""
        summary = r.get("summary") or r.get("snippet") or ""
        content = r.get("content") or ""
        authority = r.get("authority")
        date = r.get("date") or ""
        extra = ""
        if authority is not None and authority >= 0:
            extra += " · authority %d/4" % authority
        if date:
            extra += " · %s" % str(date)[:10]
        lines.append("- %s (%s%s)" % (title, url, extra))
        if summary:
            lines.append("  " + summary)
        if need_content and content:
            lines.append("  " + content[:2000])
    return "\n".join(lines)


def tool_web_fetch(args):
    """Fetch a single web page and return its Markdown content."""
    url = (args.get("url") or "").strip()
    if not url:
        return "Error: web_fetch requires a url"
    try:
        data = _post("/v1/tools/fetch", {"url": url})
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        return "Error: web fetch failed (HTTP %s): %s" % (e.code, detail)
    except Exception as e:
        return "Error: web fetch failed: %s" % e
    title = data.get("title") or ""
    content = data.get("content") or ""
    out = ("# %s\n\n%s" % (title, content)).strip()
    return out or "Fetched page is empty."
