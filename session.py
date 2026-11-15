# -*- coding: utf-8 -*-
"""Agent session persistence: atomic JSON files under BASE_DIR/.sessions, with pruning."""
import json
import re
import time

from common import BASE_DIR

SESSION_ROOT = BASE_DIR / ".sessions"


def _session_file(run_id):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(run_id))
    return SESSION_ROOT / (safe + ".json")


def save_session(state):
    """Atomically write a session dict to disk (temp file + rename)."""
    try:
        SESSION_ROOT.mkdir(parents=True, exist_ok=True)
        path = _session_file(state.get("run_id") or "")
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def load_session(run_id):
    """Load a persisted session dict, or None when missing/corrupt."""
    try:
        path = _session_file(run_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def prune_sessions(max_age_days=7):
    """Delete session files older than max_age_days (called on startup)."""
    try:
        SESSION_ROOT.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - max_age_days * 86400
        for p in SESSION_ROOT.glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except Exception:
        pass
