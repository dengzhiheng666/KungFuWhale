# -*- coding: utf-8 -*-
"""Agent file tools: list/read/write/edit/delete/create plus grep and glob search."""
import difflib
import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from common import CREATE_NO_WINDOW, IS_WIN, _num, get_edit_chunk_chars, get_read_limit


# ------------------------- Change capture (diff appendix) -------------------------
# File-modifying tools record a line-level diff here so the agent loop can stream
# "file_change" events to the frontend, which renders a red/green diff appendix.
_pending_changes = []


def _line_diff(old_text, new_text):
    """Return a list of {"kind": "add"|"del", "text": line} for the changed lines only."""
    old_lines = (old_text or "").splitlines()
    new_lines = (new_text or "").splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    diff = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        if op in ("delete", "replace"):
            for line in old_lines[i1:i2]:
                diff.append({"kind": "del", "text": line})
        if op in ("insert", "replace"):
            for line in new_lines[j1:j2]:
                diff.append({"kind": "add", "text": line})
    return diff


def _record_change(rel, old_text, new_text):
    """Record (or merge) a file's line diff into the pending change list."""
    diff = _line_diff(old_text, new_text)
    if not diff:
        return
    adds = sum(1 for d in diff if d["kind"] == "add")
    dels = sum(1 for d in diff if d["kind"] == "del")
    for change in _pending_changes:
        if change["path"] == rel and change.get("kind") == "edit":
            change["diff"].extend(diff)
            change["additions"] += adds
            change["deletions"] += dels
            return
    _pending_changes.append({
        "path": rel, "kind": "edit", "additions": adds, "deletions": dels, "diff": diff,
    })


def _record_delete(rel):
    _pending_changes.append({"path": rel, "kind": "delete", "additions": 0, "deletions": 0, "diff": []})


def drain_changes():
    """Return and clear the accumulated file changes (for the agent loop to emit)."""
    global _pending_changes
    out = _pending_changes
    _pending_changes = []
    return out


def _safe_resolve(root, rel):
    """Resolve a relative path inside root; raise ValueError when it escapes."""
    root = Path(root).resolve()
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if rel in ("", "."):
        return root
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ValueError("Path is outside the selected folder: " + rel)
    return p


def _agent_rel(root, p):
    """Return a path relative to root (fallback to the absolute path when it escapes)."""
    try:
        return str(Path(p).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(p)


# Directories / file suffixes always skipped by the project-aware file iterator and list_files.
_PROJECT_SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".checkpoints", ".idea", ".vscode", "dist", "build",
}
_PROJECT_SKIP_SUFFIXES = (".pyc", ".so", ".dll")


def _iter_project_files(root: Path, include_globs=None):
    """Yield file paths under `root`, honouring .gitignore and skipping generated files/dirs.

    Uses the `pathspec` package when importable; otherwise falls back to a best-effort
    fnmatch-based .gitignore parser. Always skips .git, .venv, venv, env, node_modules,
    __pycache__, .pytest_cache, .checkpoints, .idea, .vscode, dist, build, and the extensions
    .pyc / .so / .dll. `include_globs` is reserved for future callers and currently ignored.
    """
    root = Path(root)
    if not root.is_dir():
        return

    spec = None
    fallback_patterns = []
    gitignore = root / ".gitignore"
    try:
        import pathspec  # optional dependency
        if gitignore.is_file():
            try:
                spec = pathspec.PathSpec.from_lines(
                    "gitwildmatch",
                    gitignore.read_text(encoding="utf-8", errors="replace").splitlines(),
                )
            except Exception:
                spec = None
    except ImportError:
        pathspec = None

    if spec is None and gitignore.is_file():
        try:
            for line in gitignore.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("!"):
                    continue
                fallback_patterns.append(line.rstrip("/"))
        except Exception:
            fallback_patterns = []

    def _fallback_ignored(rel, name):
        for pat in fallback_patterns:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) or rel.startswith(pat + "/"):
                return True
        return False

    for dirpath, dirnames, filenames in os.walk(root):
        kept_dirs = []
        for d in sorted(dirnames):
            if d in _PROJECT_SKIP_DIRS:
                continue
            rel_d = (Path(dirpath) / d).relative_to(root).as_posix()
            if spec is not None:
                if spec.match_file(rel_d + "/"):
                    continue
            elif _fallback_ignored(rel_d, d):
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs
        for fn in sorted(filenames):
            if fn.endswith(_PROJECT_SKIP_SUFFIXES):
                continue
            p = Path(dirpath) / fn
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                rel = fn
            if spec is not None:
                if spec.match_file(rel):
                    continue
            elif _fallback_ignored(rel, fn):
                continue
            yield p


def tool_list_files(root, args):
    """List project files (not generated dirs) under a folder, relative to that folder."""
    target = _safe_resolve(root, args.get("path") or "")
    if not target.is_dir():
        return "Error: not a folder"
    lines = []
    for p in _iter_project_files(target):
        lines.append("📄 " + _agent_rel(target, p))
    return "\n".join(lines) if lines else "(empty folder)"


def tool_grep(root, args):
    """Search file contents with a regular expression, preferring ripgrep when available.

    Returns matches as "relpath:lineno: text" lines, capping each matched line at 300 characters
    and noting when max_results is reached. Falls back to a Python re implementation.
    """
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        return "Error: missing pattern"
    try:
        re.compile(pattern, re.IGNORECASE if args.get("ignore_case") else 0)
    except re.error as e:
        return "Error: invalid regular expression: " + str(e)

    path = (args.get("path") or "").strip()
    target = _safe_resolve(root, path or "")
    glob_pattern = (args.get("glob") or "").strip()
    max_results = _num(args, "max_results", 100, int)
    max_results = max(1, min(max_results, 1000))

    rg = shutil.which("rg")
    if rg and target.exists():
        rel_target = _agent_rel(root, target)
        cmd = [rg, "-n", "--no-heading", "--color", "never", "--max-columns", "300",
               "-e", pattern, "-m", str(max_results + 1)]
        if args.get("ignore_case"):
            cmd.append("-i")
        if glob_pattern:
            cmd.extend(["-g", glob_pattern])
        cmd.append(rel_target)
        try:
            res = subprocess.run(
                cmd, cwd=str(root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
                creationflags=CREATE_NO_WINDOW if IS_WIN else 0,
            )
        except Exception:
            res = None
        if res is not None and res.returncode in (0, 1):
            results = []
            hit_cap = False
            for line in (res.stdout or "").splitlines():
                if len(results) >= max_results:
                    hit_cap = True
                    break
                # rg emits "relpath:lineno:text"; normalise to "relpath:lineno: text"
                if line.startswith("./"):
                    line = line[2:]
                idx1 = line.find(":")
                idx2 = line.find(":", idx1 + 1) if idx1 != -1 else -1
                if idx2 != -1:
                    line = line[:idx2] + ":" + " " + line[idx2 + 1:]
                results.append(line)
            out = "\n".join(results)
            if hit_cap:
                out += "\n... [grep: more than %d matches; refine the pattern or path]" % max_results
            return out or "(no matches)"

    # Fallback: walk project files and match with Python re.
    if target.is_file():
        files = [target]
    elif target.is_dir():
        files = _iter_project_files(target)
    else:
        return "Error: path does not exist: " + (path or ".")

    results = []
    hit_cap = False
    for f in files:
        rel = _agent_rel(root, f)
        if glob_pattern and not (fnmatch.fnmatch(rel, glob_pattern) or fnmatch.fnmatch(f.name, glob_pattern)):
            continue
        try:
            with open(f, "rb") as fh:
                if b"\x00" in fh.read(8192):
                    continue
        except OSError:
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.rstrip("\r\n")
                    if re.search(pattern, line, re.IGNORECASE if args.get("ignore_case") else 0):
                        if len(results) >= max_results:
                            hit_cap = True
                            break
                        text = line if len(line) <= 300 else line[:300] + "..."
                        results.append("%s:%d: %s" % (rel, lineno, text))
        except OSError:
            continue
        if hit_cap:
            break
    out = "\n".join(results)
    if hit_cap:
        out += "\n... [grep: more than %d matches; refine the pattern or path]" % max_results
    return out or "(no matches)"


def _glob_to_regex(pattern):
    """Translate a glob pattern (with `**` for zero-or-more directories) into a full-match regex."""
    parts = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                parts.append(".*")
                i += 2
                if i < n and pattern[i] == "/":
                    parts.append("(?:.*/)?")
                    i += 1
            else:
                parts.append("[^/]*")
                i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1
    return "^" + "".join(parts) + "$"


def tool_glob(root, args):
    """Find files whose relative path matches a glob pattern, capped at 500 results."""
    pattern = (args.get("pattern") or "").strip().replace("\\", "/")
    if not pattern:
        return "Error: missing pattern"
    path = (args.get("path") or "").strip()
    target = _safe_resolve(root, path or "")
    if not target.is_dir():
        return "Error: path is not a folder: " + (path or ".")
    try:
        regex = re.compile(_glob_to_regex(pattern))
    except re.error:
        return "Error: invalid glob pattern: " + pattern
    matches = []
    for p in _iter_project_files(target):
        rel = _agent_rel(root, p).replace("\\", "/")
        if regex.match(rel):
            matches.append(rel)
            if len(matches) >= 500:
                break
    out = "\n".join(matches)
    if len(matches) >= 500:
        out += "\n... [glob: results truncated at 500 matches]"
    return out or "(no matches)"


def _read_file_slice(target, offset, limit):
    """Stream a line window from disk, returning (lines, total_lines, truncated).

    `lines` is a list of (1-based lineno, text) tuples; `total_lines` is the file's full line
    count; `truncated` is True when any leading or trailing lines were not returned.
    """
    lines = []
    total = 0
    truncated = False
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        it = enumerate(fh, 1)
        for lineno, line in it:
            total = lineno
            if lineno < offset:
                continue
            if len(lines) < limit:
                lines.append((lineno, line.rstrip("\r\n")))
            else:
                truncated = True
        if truncated:
            for lineno, _line in it:
                total = lineno
    return lines, total, truncated


def tool_read_file(root, args):
    """Read a text file, returning numbered lines; supports offset/limit for large files."""
    rel = (args.get("path") or "").strip()
    if not rel:
        return "Error: missing path"
    target = _safe_resolve(root, rel)
    if not target.is_file():
        return "Error: file does not exist: " + rel
    if rel.lower().endswith((".db", ".sqlite", ".sqlite3", ".db3")):
        return "This is a SQLite database file; please use the db_schema (view table structure) or db_query (execute SQL) tool instead"

    sliced = ("offset" in args) or ("limit" in args)
    if not sliced:
        try:
            if target.stat().st_size > get_read_limit():
                return ("Error: file too large to read (exceeds %d bytes); "
                        "use offset and limit to read a smaller range" % get_read_limit())
        except OSError:
            pass

    offset = _num(args, "offset", 1, int)
    limit = _num(args, "limit", 500, int)
    offset = max(1, offset)
    limit = max(1, min(limit, 2000))

    try:
        with open(target, "rb") as fh:
            if b"\x00" in fh.read(8192):
                return "Error: binary file cannot be read"
    except OSError as e:
        return "Error: cannot read file: " + str(e)

    lines, total, truncated = _read_file_slice(target, offset, limit)
    if total == 0:
        return "(empty file)"
    if not lines:
        return "Error: offset %d is beyond the end of the file (%d lines)" % (offset, total)
    body = "\n".join("%6d\t%s" % (ln, text) for ln, text in lines)
    if truncated or offset > 1:
        first = lines[0][0]
        last = lines[-1][0]
        return "[showing lines %d-%d of %d]\n%s" % (first, last, total, body)
    return body


def tool_write_file(root, args):
    """Create, overwrite, or append to a file, creating parent directories automatically.

    mode="append" appends `content` to an existing file. That is how a large file is written
    piece by piece (first piece: overwrite, every following piece: append), so a model with a
    small output-token limit never has to emit a whole file in a single reply.
    """
    rel = (args.get("path") or "").strip()
    if not rel:
        return "Error: missing path"
    target = _safe_resolve(root, rel)
    content = args.get("content") or ""
    mode = (args.get("mode") or "overwrite").strip().lower()
    if mode not in ("overwrite", "append"):
        return "Error: unknown mode '%s'; use 'overwrite' (default) or 'append'." % mode

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        written = len(content)
        if mode == "append":
            previous = target.stat().st_size if target.is_file() else 0
            old_text = ""
            if target.is_file():
                try:
                    old_text = target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    old_text = ""
            with open(target, "ab") as fh:
                fh.write(content.encode("utf-8"))
            _record_change(rel, old_text, old_text + content)
            msg = "Appended %d characters to file: %s (now about %d bytes)" % (
                written, rel, previous + len(content.encode("utf-8")))
        else:
            old_text = ""
            if target.is_file():
                try:
                    old_text = target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    old_text = ""
            target.write_text(content, encoding="utf-8")
            _record_change(rel, old_text, content)
            msg = "Wrote file: %s (%d characters)" % (rel, written)
    except OSError as e:
        return "Error: cannot write file %s: %s" % (rel, e)

    limit = get_edit_chunk_chars()
    if written > limit:
        msg += ("\nNote: this piece was %d characters, above the recommended piece size of %d. "
                "Split the remaining content into smaller pieces and append them "
                "(write_file with mode=\"append\")." % (written, limit))
    return msg


def tool_edit_file(root, args):
    """Replace a single unique occurrence of old_string in a file, preserving its line endings."""
    rel = (args.get("path") or "").strip()
    if not rel:
        return "Error: missing path"
    target = _safe_resolve(root, rel)
    old = (args.get("old_string") or "").replace("\r\n", "\n")
    new = (args.get("new_string") or "").replace("\r\n", "\n")
    if old == "":
        return "Error: old_string is empty; provide the exact text to replace."
    if not target.is_file():
        return "Error: file does not exist: %s. Use write_file to create it." % rel
    raw = target.read_bytes()
    if b"\x00" in raw:
        return "Error: binary file cannot be edited"
    has_crlf = b"\r\n" in raw
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    count = text.count(old)
    if count == 0:
        return "Error: old_string not found in %s. Re-read the file and try again." % rel
    if count > 1:
        return "Error: old_string matches %d times in %s. Add more surrounding context." % (count, rel)
    new_text = text.replace(old, new, 1)
    _record_change(rel, text, new_text)
    if has_crlf:
        new_text = new_text.replace("\n", "\r\n")
    # Write bytes directly so the restored line endings are not re-translated by text-mode newline handling.
    target.write_bytes(new_text.encode("utf-8"))
    msg = "Edited %s (replaced %d chars with %d)." % (rel, len(old), len(new))
    limit = get_edit_chunk_chars()
    if len(new) > limit:
        msg += ("\nNote: this piece was %d characters, above the recommended piece size of %d; "
                "for further changes to this file use smaller old_string / new_string windows."
                % (len(new), limit))
    return msg


def tool_delete_file(root, args):
    """Delete a file or folder."""
    rel = (args.get("path") or "").strip()
    if not rel:
        return "Error: missing path"
    target = _safe_resolve(root, rel)
    if target == root:
        return "Error: cannot delete the root of the selected folder"
    if not target.exists():
        return "Error: path does not exist: " + rel
    if target.is_dir():
        shutil.rmtree(target)
        return "Deleted folder: " + rel
    target.unlink()
    _record_delete(rel)
    return "Deleted file: " + rel


def tool_create_directory(root, args):
    """Create a folder (including nested subfolders)."""
    rel = (args.get("path") or "").strip()
    if not rel:
        return "Error: missing path"
    target = _safe_resolve(root, rel)
    target.mkdir(parents=True, exist_ok=True)
    return "Created folder: " + rel
