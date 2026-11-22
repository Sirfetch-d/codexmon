"""threads / spawn edges / turn history from a codex home."""

import json
import time

from .dbutil import safe_query

THREAD_COLUMNS = (
    "id", "rollout_path", "created_at", "updated_at", "source", "model_provider", "cwd",
    "title", "sandbox_policy", "approval_mode", "tokens_used", "archived", "git_branch",
    "cli_version", "agent_nickname", "agent_role", "model", "reasoning_effort",
    "agent_path", "thread_source", "originator", "name", "is_pinned",
    "first_user_message", "preview",
)


def _thread_columns(home):
    """Older codex homes have fewer columns; only select what exists."""
    rows, err = safe_query(home["state_db"], "PRAGMA table_info(threads)")
    if err or not rows:
        return list(THREAD_COLUMNS), err
    available = {row.get("name") for row in rows}
    return [c for c in THREAD_COLUMNS if c in available], None


def _parse_json(raw, default=None):
    if not raw:
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except Exception:
        return default if default is not None else {}


def load_threads(home, limit=80):
    """Most recently updated threads for one home."""
    columns, col_err = _thread_columns(home)
    if "id" not in columns:
        return [], col_err or "no threads table"
    rows, err = safe_query(
        home["state_db"],
        "SELECT %s FROM threads ORDER BY updated_at DESC LIMIT ?" % ", ".join(columns),
        (limit,),
    )
    err = err or col_err
    out = []
    for row in rows:
        sandbox = _parse_json(row.get("sandbox_policy"))
        if isinstance(sandbox, dict):
            sandbox_kind = sandbox.get("type") or ""
        else:
            sandbox_kind = ""
        row["sandbox_kind"] = sandbox_kind
        row["thread_source"] = row.get("thread_source") or "user"
        out.append(row)
    return out, err


def load_spawn_edges(home):
    rows, err = safe_query(
        home["state_db"],
        "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges",
    )
    edges = {}
    for row in rows:
        child = row.get("child_thread_id")
        parent = row.get("parent_thread_id")
        if child and parent:
            edges[child] = {"parent": parent, "status": row.get("status") or "open"}
    return edges, err


def load_turns(home, thread_ids, per_thread=6):
    """Latest turns per thread plus a short per-thread tally."""
    if not thread_ids:
        return {}, None
    placeholders = ",".join("?" for _ in thread_ids)
    rows, err = safe_query(
        home["history_db"],
        "SELECT thread_id, turn_id, status, started_at, completed_at, duration_ms, "
        "error_json, root_turn_id FROM thread_turns WHERE thread_id IN (%s) "
        "ORDER BY started_at DESC LIMIT 4000" % placeholders,
        tuple(thread_ids),
    )
    turns = {}
    for row in rows:
        tid = row["thread_id"]
        bucket = turns.setdefault(
            tid, {"latest": None, "count": 0, "failed": 0, "interrupted": 0, "runs": []}
        )
        bucket["count"] += 1
        status = (row.get("status") or "").lower()
        if status in ("failed", "error"):
            bucket["failed"] += 1
        if status in ("interrupted", "aborted", "cancelled"):
            bucket["interrupted"] += 1
        if bucket["latest"] is None:
            row["error_message"] = _error_message(row.pop("error_json", None))
            bucket["latest"] = row
        if len(bucket["runs"]) < per_thread:
            bucket["runs"].append(row)
    return turns, err


def _error_message(raw):
    if not raw:
        return None
    data = _parse_json(raw, {})
    msg = data.get("message") if isinstance(data, dict) else str(data)
    if not msg:
        return None
    # unwrap the nested provider JSON: {"error":{"message":"..."}}
    try:
        inner = json.loads(msg)
        if isinstance(inner, dict):
            err = inner.get("error")
            if isinstance(err, dict) and err.get("message"):
                return err["message"]
    except Exception:
        pass
    return str(msg)[:300]


def turn_duration_ms(turn, now=None):
    """Duration of a turn in ms, live-computed for in-progress turns."""
    if not turn:
        return None
    if turn.get("duration_ms"):
        return int(turn["duration_ms"])
    started = turn.get("started_at")
    if started and (turn.get("status") or "").lower() == "inprogress":
        return max(0, int((now or time.time()) - started) * 1000)
    completed = turn.get("completed_at")
    if started and completed:
        return max(0, int((completed - started) * 1000))
    return None
