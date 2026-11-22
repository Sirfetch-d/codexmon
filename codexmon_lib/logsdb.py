"""Live activity extracted from a codex home's logs_2.sqlite.

Everything in here is derived from log text, which is the only source that sees
in-memory (non-persisted) subagent threads.
"""

import re
import time

from .dbutil import safe_query

TOOL_TARGET = "codex_core::tools::parallel"
COMM_TARGET = "codex_otel.agent_communication"
TURN_TARGET = "codex_core::session::turn"
HANDLER_TARGET = "codex_core::session::handlers"
ROUTER_TARGET = "codex_core::tools::router"

TOOL_RE = re.compile(
    r"tool call completed .*?tool_name=(\S+) .*?total_duration_ms=(\d+)"
)
TOOL_RE_LOOSE = re.compile(r"tool_name=(\S+)")
HANDLER_RE = re.compile(r"handler_duration_ms=(\d+)")
SPAWN_RE = re.compile(
    r'kind="(?P<kind>\w+)" state="(?P<state>\w+)" sender_thread_id=(?P<sender>\S+) '
    r'receiver_thread_id=(?P<receiver>\S+)'
)
SPAWN_PAIRS_RE = re.compile(r"sender_thread_id=(\S+) receiver_thread_id=(\S+)")
CTX_RE = re.compile(
    r"post sampling token usage .*?total_usage_tokens=(\d+).*?full_context_window_limit=Some\((\d+)\)"
)
TURN_USAGE_RE = re.compile(r"codex\.turn\.token_usage\.(\w+)=(\d+)")
PID_RE = re.compile(r"^pid:(\d+):")
EXIT_MARKERS = ("Agent loop exited", "Shutting down Codex instance")

# tool calls that indicate an agent is orchestrating rather than producing
ORCHESTRATION_TOOLS = {
    "spawn_agent",
    "send_message",
    "followup_task",
    "interrupt_agent",
    "wait_agent",
    "close_agent",
    "list_agents",
    "resume_agent",
    "send_input",
}
WAIT_TIMEOUT_MS = 9000


def _norm_tool(name):
    # log names are namespaced as `collaborationswait_agent`; longest prefix wins
    for prefix in ("collaborations", "collaboration"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _comm_content(body):
    """Pull the human-readable payload out of an agent_communication line."""
    marker = 'content="'
    idx = body.find(marker)
    if idx < 0:
        return None
    raw = body[idx + len(marker):]
    if raw.endswith('"'):
        raw = raw[:-1]
    raw = " ".join(raw.replace('\\n', ' ').split())
    if not raw or raw.startswith("[") or raw in ("unused", "null"):
        return None
    return raw[:160]


NOISE_PREFIXES = ("noop", "unused", "stop", "the new_task", "ping", "ok", "done")


def _meaningful(text):
    low = text.strip().lower()
    if len(low) < 6:
        return False
    return not any(low.startswith(p) for p in NOISE_PREFIXES)


def parse_pid(process_uuid):
    if not process_uuid:
        return None
    m = PID_RE.match(process_uuid)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def load_activity(home, window_hours=12, now=None, max_rows=6000):
    """Return per-thread live activity plus spawn edges and a recent event feed."""
    now = now or time.time()
    since = int(now - window_hours * 3600)
    logs_db = home["logs_db"]

    threads = {}
    rows, err = safe_query(
        logs_db,
        "SELECT thread_id, process_uuid, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
        "COUNT(*) AS lines FROM logs WHERE ts >= ? AND thread_id IS NOT NULL "
        "GROUP BY thread_id, process_uuid",
        (since,),
    )
    if err:
        return {"threads": {}, "edges": {}, "events": [], "errors": [err]}

    for row in rows:
        tid = row["thread_id"]
        entry = threads.setdefault(
            tid,
            {
                "last_ts": 0,
                "first_ts": now,
                "lines": 0,
                "pids": set(),
                "tool_counts": {},
                "tool_total": 0,
                "tool_ms": 0,
                "wait_timeouts": 0,
                "close_calls": 0,
                "patch_calls": 0,
                "exec_calls": 0,
                "errors": 0,
                "sampling": 0,
                "ctx_tokens": None,
                "ctx_limit": None,
                "turn_usage": {},
                "exited_at": None,
                "spawned_from": None,
                "spawned_children": [],
                "last_tool": None,
                "last_tool_ts": None,
                "first_msg": None,
                "first_msg_ts": None,
            },
        )
        entry["last_ts"] = max(entry["last_ts"], row["last_ts"] or 0)
        entry["first_ts"] = min(entry["first_ts"], row["first_ts"] or now)
        entry["lines"] += row["lines"] or 0
        pid = parse_pid(row.get("process_uuid"))
        if pid:
            entry["pids"].add(pid)

    detail_rows, detail_err = safe_query(
        logs_db,
        "SELECT ts, target, thread_id, feedback_log_body FROM logs "
        "WHERE ts >= ? AND thread_id IS NOT NULL AND target IN (?,?,?,?,?) "
        "ORDER BY id DESC LIMIT ?",
        (since, TOOL_TARGET, COMM_TARGET, TURN_TARGET, HANDLER_TARGET, ROUTER_TARGET, max_rows),
    )
    if detail_err:
        err = err + "; " + detail_err if err else detail_err

    edges = {}
    events = []
    for row in detail_rows:
        tid = row["thread_id"]
        ts = row["ts"] or 0
        target = row["target"]
        body = row["feedback_log_body"] or ""
        entry = threads.get(tid)
        if entry is None:
            continue

        if target == TOOL_TARGET:
            m = TOOL_RE.search(body)
            name = _norm_tool(m.group(1)) if m else None
            if not name:
                lm = TOOL_RE_LOOSE.search(body)
                name = _norm_tool(lm.group(1)) if lm else "?"
            total_ms = int(m.group(2)) if m else 0
            hm = HANDLER_RE.search(body)
            handler_ms = int(hm.group(1)) if hm else total_ms
            entry["tool_counts"][name] = entry["tool_counts"].get(name, 0) + 1
            entry["tool_total"] += 1
            entry["tool_ms"] += total_ms
            if name == "wait_agent" and handler_ms >= WAIT_TIMEOUT_MS:
                entry["wait_timeouts"] += 1
            if name == "close_agent":
                entry["close_calls"] += 1
            if name in ("apply_patch", "write_file", "edit_file"):
                entry["patch_calls"] += 1
            if name in ("exec_command", "shell", "run_command"):
                entry["exec_calls"] += 1
            if entry["last_tool_ts"] is None or ts >= entry["last_tool_ts"]:
                entry["last_tool"] = name
                entry["last_tool_ts"] = ts
            if len(events) < 80 and name not in ORCHESTRATION_TOOLS:
                events.append(
                    {
                        "ts": ts,
                        "thread": tid,
                        "tool": name,
                        "ms": total_ms,
                        "ok": True,
                        "text": "",
                    }
                )
        elif target == COMM_TARGET:
            # the first message an agent receives is the best human label
            content = _comm_content(body)
            if content and _meaningful(content):
                prev = entry.get("first_msg")
                if prev is None or not _meaningful(prev) or ts < (entry.get("first_msg_ts") or ts):
                    entry["first_msg"] = content
                    entry["first_msg_ts"] = ts
            m = SPAWN_RE.search(body)
            if m:
                kind = m.group("kind")
                sender = m.group("sender")
                receiver = m.group("receiver")
                if kind == "spawn":
                    edges[receiver] = {"parent": sender, "status": "open"}
                    parent_entry = threads.get(sender)
                    if parent_entry is not None and receiver not in parent_entry["spawned_children"]:
                        parent_entry["spawned_children"].append(receiver)
                    child_entry = threads.get(receiver)
                    if child_entry is not None:
                        child_entry["spawned_from"] = sender
            else:
                pm = SPAWN_PAIRS_RE.search(body)
                if pm and threads.get(pm.group(2)) is not None:
                    threads[pm.group(2)]["spawned_from"] = pm.group(1)
        elif target == TURN_TARGET:
            cm = CTX_RE.search(body)
            if cm:
                entry["ctx_tokens"] = int(cm.group(1))
                entry["ctx_limit"] = int(cm.group(2))
                entry["sampling"] += 1
            for key, val in TURN_USAGE_RE.findall(body):
                entry["turn_usage"][key] = max(entry["turn_usage"].get(key, 0), int(val))
        elif target == HANDLER_TARGET:
            if any(marker in body for marker in EXIT_MARKERS):
                if entry["exited_at"] is None or ts > entry["exited_at"]:
                    entry["exited_at"] = ts
        elif target == ROUTER_TARGET:
            if "error=" in body:
                entry["errors"] += 1
                if len(events) < 80:
                    em = re.search(r"error=(.{0,160})", body, re.S)
                    events.append(
                        {
                            "ts": ts,
                            "thread": tid,
                            "tool": (TOOL_RE_LOOSE.search(body).group(1) if TOOL_RE_LOOSE.search(body) else "?"),
                            "ms": 0,
                            "ok": False,
                            "text": " ".join((em.group(1) if em else "").split())[:160],
                        }
                    )

    for tid, entry in threads.items():
        entry["pids"] = sorted(entry["pids"])
        entry["last_tool"] = entry["last_tool"] or "—"

    events.sort(key=lambda e: e["ts"], reverse=True)
    return {
        "threads": threads,
        "edges": edges,
        "events": events[:40],
        "errors": [err] if err else [],
    }
