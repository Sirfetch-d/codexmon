"""Read-only helpers shared by every source module."""

import os
import sqlite3
from pathlib import Path


def connect_ro(path):
    """Open a sqlite database strictly read-only."""
    uri = "file:%s?mode=ro" % str(path)
    con = sqlite3.connect(uri, uri=True, timeout=0.4)
    con.row_factory = sqlite3.Row
    return con


def safe_query(path, sql, params=()):
    """Run a query, returning (rows, error_string). Never raises."""
    p = Path(path)
    if not p.exists():
        return [], "missing"
    try:
        con = connect_ro(p)
        try:
            return [dict(r) for r in con.execute(sql, params)], None
        finally:
            con.close()
    except Exception as exc:  # locked / corrupt / schema drift
        return [], "%s: %s" % (type(exc).__name__, exc)


def safe_value(path, sql, params=(), default=None):
    rows, err = safe_query(path, sql, params)
    if err or not rows:
        return default
    row = rows[0]
    for key in row:
        return row[key]
    return default


def read_env_file(path):
    """Parse a KEY=VALUE .env file; ignores comments and export prefixes."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    out[key] = val
    except OSError:
        pass
    return out


def mask_key(key):
    if not key:
        return "—"
    if len(key) <= 12:
        return key[:3] + "…"
    return key[:6] + "…" + key[-4:]


def home_dir():
    return Path(os.path.expanduser("~"))
