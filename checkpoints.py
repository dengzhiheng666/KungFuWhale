# -*- coding: utf-8 -*-
"""Working-folder checkpoints: snapshot, list, restore, and delete."""
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from common import BASE_DIR
from tools.db_tools import _db_statement_is_read

CHECKPOINT_EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".idea", ".vscode", ".checkpoints",
}
# In Agent mode, the following tools add/delete/modify files in the working folder;
# an auto checkpoint is created before the first execution of these tools, so the user can restore with one click.
AUTO_CHECKPOINT_MODIFY_TOOLS = {"write_file", "edit_file", "delete_file", "create_directory"}


def _tool_call_modifies(name, arguments):
    """Determine whether a tool call will modify the working folder contents (used to decide whether to auto-create a checkpoint).

    db_query is judged by whether the SQL is a read or a write; other tools are judged by tool name.
    """
    if name in AUTO_CHECKPOINT_MODIFY_TOOLS:
        return True
    if name == "db_query":
        try:
            args = json.loads(arguments or "{}")
        except Exception:
            args = {}
        return not _db_statement_is_read(args.get("sql") or "")
    return False


CHECKPOINT_ROOT = BASE_DIR / ".checkpoints"
CHECKPOINT_INDEX_FILE = CHECKPOINT_ROOT / "index.json"
_CHECKPOINT_LOCK = threading.Lock()


def _norm_folder(folder):
    """Return the canonical path used for indexing/hashing (lowercased, resolved absolute path)."""
    try:
        return str(Path(folder).expanduser().resolve()).lower()
    except Exception:
        return str(folder).strip().lower()


def _checkpoint_folder_hash(folder):
    """On-disk storage directory name for the working folder (short hash)."""
    return hashlib.sha256(_norm_folder(folder).encode("utf-8")).hexdigest()[:24]


def _load_checkpoint_index():
    try:
        if CHECKPOINT_INDEX_FILE.exists():
            data = json.loads(CHECKPOINT_INDEX_FILE.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_checkpoint_index(index):
    CHECKPOINT_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT_INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CHECKPOINT_INDEX_FILE)


def _snapshot_folder(folder, dest):
    """Recursively copy folder to dest, skipping excluded directories, returning the number of copied files."""
    folder = Path(folder)
    dest = Path(dest)
    count = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in CHECKPOINT_EXCLUDE_DIRS)
        rel_root = Path(root).relative_to(folder)
        for name in files:
            src = Path(root) / name
            dst = dest / rel_root / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            count += 1
    return count


def _prune_empty_dirs(folder):
    """Delete empty directories left after snapshot restore (does not touch excluded directories, and does not delete the root)."""
    folder = Path(folder)
    for root, dirs, files in os.walk(folder):
        for name in dirs:
            if name in CHECKPOINT_EXCLUDE_DIRS:
                continue
            p = Path(root) / name
            try:
                p.rmdir()  # Only empty directories can be removed
            except OSError:
                pass


def _restore_snapshot(folder, src):
    """Restore the working folder to the state of snapshot src: delete added files, overwrite modified files, and recreate deleted files."""
    folder = Path(folder)
    src = Path(src)

    # Set of relative file paths in the snapshot (POSIX style)
    snapshot_files = set()
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        for name in files:
            snapshot_files.add((rel_root / name).as_posix())

    # 1) Delete files in the current folder that do not exist in the snapshot (skip excluded directories)
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in CHECKPOINT_EXCLUDE_DIRS)
        rel_root = Path(root).relative_to(folder)
        for name in files:
            if (rel_root / name).as_posix() not in snapshot_files:
                try:
                    (Path(root) / name).unlink()
                except OSError:
                    pass

    # 2) Overwrite / recreate files from the snapshot
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        for name in files:
            src_file = Path(root) / name
            dst_file = folder / rel_root / name
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)

    # 3) Clean up empty directories that do not exist in the snapshot
    _prune_empty_dirs(folder)


def _create_checkpoint(folder, label, auto=False):
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError("Folder does not exist: " + str(folder))
    key = _norm_folder(folder)
    cid = uuid.uuid4().hex
    dest = CHECKPOINT_ROOT / _checkpoint_folder_hash(folder) / cid
    with _CHECKPOINT_LOCK:
        file_count = _snapshot_folder(folder, dest)
        size_bytes = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
        cp = {
            "id": cid,
            "label": (label or "").strip() or None,
            "auto": bool(auto),
            "created": time.time(),
            "file_count": file_count,
            "size_bytes": size_bytes,
        }
        index = _load_checkpoint_index()
        index.setdefault(key, []).insert(0, cp)
        _save_checkpoint_index(index)
    return cp


def _list_checkpoints(folder):
    index = _load_checkpoint_index()
    return index.get(_norm_folder(folder), [])


def _restore_checkpoint(folder, cid):
    folder = Path(folder).expanduser().resolve()
    src = CHECKPOINT_ROOT / _checkpoint_folder_hash(folder) / cid
    if not src.is_dir():
        raise FileNotFoundError("Checkpoint does not exist: " + cid)
    with _CHECKPOINT_LOCK:
        _restore_snapshot(folder, src)


def _delete_checkpoint(folder, cid):
    key = _norm_folder(folder)
    src = CHECKPOINT_ROOT / _checkpoint_folder_hash(folder) / cid
    with _CHECKPOINT_LOCK:
        shutil.rmtree(src, ignore_errors=True)
        index = _load_checkpoint_index()
        lst = [c for c in index.get(key, []) if c.get("id") != cid]
        if lst:
            index[key] = lst
        else:
            index.pop(key, None)
        _save_checkpoint_index(index)
