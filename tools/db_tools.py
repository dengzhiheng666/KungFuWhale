# -*- coding: utf-8 -*-
"""SQLite schema/query tools and helpers for the Agent and the frontend database viewer."""
import re
import sqlite3
from pathlib import Path

from tools.file_tools import _safe_resolve

DB_ROW_LIMIT = 1000  # Maximum rows returned per query
_DB_ALLOWED_PREFIXES = ("SELECT", "WITH", "PRAGMA", "EXPLAIN", "VALUES")


def _db_connect(path):
    """Open a SQLite database in read-only mode, returning the connection (raises on failure)."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError("File does not exist: " + str(p))
    # Read-only URI: file:///...?mode=ro, avoiding any writes; as_uri correctly escapes non-ASCII/space paths
    uri = p.as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _db_value(v):
    """Convert a SQLite return value into a JSON-serializable form (BLOB to hex)."""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
        return "0x" + b.hex() if len(b) <= 256 else ("<BLOB %d bytes>" % len(b))
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _db_schema_tables(path):
    """Return [{name, type, columns:[{name, type, pk, fk}]}], including tables and views.

    The fk in the column structure is foreign-key info ({"ref_table":..., "ref_column":...}); omitted when there is no foreign key.
    Foreign-key relationships are obtained via PRAGMA foreign_key_list, used by the frontend to show relationships between tables.
    """
    con = _db_connect(path)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables = []
        for name, typ in cur.fetchall():
            columns = []
            try:
                quoted = '"' + name.replace('"', '""') + '"'
                # Foreign keys: PRAGMA foreign_key_list returns
                # (id, seq, table, from, to, on_update, on_delete, match)
                fk_map = {}
                fk_cur = con.cursor()
                fk_cur.execute("PRAGMA foreign_key_list(" + quoted + ")")
                for row in fk_cur.fetchall():
                    fk_map[row[3]] = {"ref_table": row[2], "ref_column": row[4]}
                pcur = con.cursor()
                pcur.execute("PRAGMA table_info(" + quoted + ")")
                for row in pcur.fetchall():
                    col = {"name": row[1], "type": row[2] or "", "pk": bool(row[5])}
                    if row[1] in fk_map:
                        col["fk"] = fk_map[row[1]]
                    columns.append(col)
            except Exception:
                columns = []
            tables.append({"name": name, "type": typ, "columns": columns})
        return tables
    finally:
        con.close()


def _db_query(path, sql):
    """Execute read-only SQL, returning (columns, rows, truncated)."""
    stmt = (sql or "").strip()
    if not stmt:
        raise ValueError("SQL statement is empty")
    first = stmt.lstrip(";").lstrip().upper()
    if not first or not first.startswith(_DB_ALLOWED_PREFIXES):
        raise ValueError("Only read-only queries are allowed (SELECT / WITH / PRAGMA / EXPLAIN / VALUES)")
    if re.search(r"\b(ATTACH|DETACH|VACUUM|REINDEX)\b", first):
        raise ValueError("This statement is not allowed")

    con = _db_connect(path)
    try:
        # Prevent heavy queries from occupying for too long: auto-interrupt after the specified number of VM steps
        counter = {"n": 0}

        def _progress():
            counter["n"] += 1
            return 1 if counter["n"] > 200000 else 0

        try:
            con.set_progress_handler(_progress, 1000)
        except Exception:
            pass

        cur = con.cursor()
        cur.execute(stmt)
        columns = [d[0] for d in cur.description] if cur.description else []
        raw = cur.fetchmany(DB_ROW_LIMIT + 1)
        truncated = len(raw) > DB_ROW_LIMIT
        rows = [[_db_value(v) for v in r] for r in raw[:DB_ROW_LIMIT]]
        return columns, rows, truncated
    finally:
        con.close()


# Maximum rows returned by a single query in Agent mode (avoid oversized results blowing up the context)
_AGENT_DB_LIMIT = 200


def _db_statement_is_read(sql):
    """Determine whether SQL is a read-only statement (SELECT / WITH / PRAGMA / EXPLAIN)."""
    first = (sql or "").strip().lstrip(";").lstrip().upper()
    return bool(first) and first.startswith(("SELECT", "WITH", "PRAGMA", "EXPLAIN"))


def _db_rows_text(columns, rows, truncated):
    """Format query results into text that is easy for the model to read."""
    lines = []
    if columns:
        lines.append("Columns: " + " | ".join(str(c) for c in columns))
    for r in rows:
        lines.append(" | ".join("NULL" if v is None else str(v) for v in r))
    if truncated:
        lines.append("... (results truncated, showing first %d rows only)" % _AGENT_DB_LIMIT)
    if not rows and not columns:
        lines.append("(no rows returned)")
    return "\n".join(lines)


def _agent_db_schema(root, rel):
    """Agent tool: return the table structure and column info of a SQLite database (read-only)."""
    target = _safe_resolve(root, rel)
    if not target.is_file():
        return "Error: database file does not exist: " + rel
    tables = _db_schema_tables(str(target))
    if not tables:
        return "No tables or views in database %s" % rel
    lines = []
    for tb in tables:
        cols = ", ".join(c["name"] for c in tb["columns"]) if tb["columns"] else "(no columns)"
        kind = "view" if tb["type"] == "view" else "table"
        lines.append("%s %s (%s)" % (kind, tb["name"], cols))
    return "Database %s structure:\n%s" % (rel, "\n".join(lines))


def _agent_db_query(root, rel, sql):
    """Agent tool: execute SQL against a SQLite database (read or write), creating the file automatically if it does not exist."""
    target = _safe_resolve(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(target))
    try:
        if _db_statement_is_read(sql):
            cur = con.cursor()
            cur.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            raw = cur.fetchmany(_AGENT_DB_LIMIT + 1)
            truncated = len(raw) > _AGENT_DB_LIMIT
            rows = [[_db_value(v) for v in r] for r in raw[:_AGENT_DB_LIMIT]]
            text = _db_rows_text(columns, rows, truncated)
            return "Database %s query results:\n%s" % (rel, text)
        con.executescript(sql)
        con.commit()
        return "Executed write SQL against database %s, %d total changes" % (rel, con.total_changes)
    finally:
        con.close()


def tool_db_schema(root, args):
    """Agent tool entry: view a SQLite database's table/view structure."""
    rel = (args.get("path") or "").strip()
    if not rel:
        return "Error: missing path"
    return _agent_db_schema(root, rel)


def tool_db_query(root, args):
    """Agent tool entry: execute SQL against a SQLite database."""
    rel = (args.get("path") or "").strip()
    sql = (args.get("sql") or "").strip()
    if not rel:
        return "Error: missing path"
    if not sql:
        return "Error: missing sql"
    return _agent_db_query(root, rel, sql)
