"""Merge every source into one snapshot dict."""

import threading
import time
from pathlib import Path

from . import logsdb, paths, procs, rollout, state_db, usage

STATE_ORDER = {"RUN": 0, "WORK": 1, "WAIT": 2, "IDLE": 3, "ORPHAN": 4, "EXIT": 5}

RUN_WINDOW = 15
WORK_WINDOW = 60
WAIT_WINDOW = 300
IDLE_WINDOW = 900
SPIN_WINDOW = 600
SPIN_MIN_WAITS = 3


def _short(tid):
    # uuidv7 ids share their leading timestamp, so 8 chars collides within a
    # minute; the first 13 chars (timestamp + first random nibbles) stay unique.
    return (tid or "?")[:13]


class UsageRefresher(threading.Thread):
    """Background quota fetcher so the UI never blocks on the network."""

    def __init__(self, homes, project_dir, ttl=60, net=True):
        threading.Thread.__init__(self)
        self.daemon = True
        self.homes = homes
        self.project_dir = project_dir
        self.ttl = ttl
        self.net = net
        self.lock = threading.Lock()
        self.state = {
            "chatgpt": {"available": False, "error": "pending", "source": "none"},
            "opencode": {"configured": False, "ok": False, "error": "pending", "windows": []},
        }
        self._stop = threading.Event()
        self._wake = threading.Event()

    def refresh_now(self):
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self._refresh()
            except Exception as exc:  # never die
                with self.lock:
                    self.state["error"] = str(exc)
            self._wake.wait(self.ttl)
            self._wake.clear()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def read(self):
        with self.lock:
            return {
                "chatgpt": dict(self.state.get("chatgpt") or {}),
                "opencode": dict(self.state.get("opencode") or {}),
            }

    # -- internals -------------------------------------------------------
    def _refresh(self):
        chatgpt = self._chatgpt()
        opencode = self._opencode()
        with self.lock:
            self.state["chatgpt"] = chatgpt
            self.state["opencode"] = opencode

    def _chatgpt(self):
        base = {"available": False, "ts": time.time()}
        if not self.homes:
            base["error"] = "no codex home"
            return base
        # prefer the home that actually authenticates with ChatGPT
        chosen = None
        for home in self.homes:
            auth = home["auth"]
            if Path(auth).is_file():
                try:
                    import json

                    data = json.loads(Path(auth).read_text(encoding="utf-8"))
                    if (data.get("auth_mode") or "") == "chatgpt" or (data.get("tokens") or {}).get("access_token"):
                        chosen = home
                        break
                except Exception:
                    continue
        if chosen is None:
            base["error"] = "no chatgpt auth"
            return base
        if not self.net:
            base["error"] = "network disabled"
            return base
        live = usage.fetch_chatgpt_usage({k: chosen[k] for k in ("auth",)}, timeout=5)
        if live.get("ok"):
            base.update(live)
            base["available"] = True
            base["source"] = "live"
            base["home"] = chosen["home"].name
            return base
        base["error"] = live.get("error") or "live query failed"
        return base

    def _opencode(self):
        configs = [h["config"] for h in self.homes]
        key, source = usage.resolve_opencode_key(self.project_dir, configs)
        out = {
            "configured": bool(key),
            "key": usage.key_fingerprint(key) if key else None,
            "key_source": source,
            "ok": False,
            "windows": [],
            "ts": time.time(),
        }
        if not key:
            out["error"] = "未配置 key"
            return out
        if not self.net:
            out["error"] = "network disabled"
            return out
        data = usage.fetch_opencode_usage(key, timeout=5)
        out.update({k: v for k, v in data.items() if k != "raw"})
        out["ts"] = time.time()
        return out


def home_defaults(home):
    """Top-level model settings from a codex home's config.toml (no TOML lib)."""
    path = home.get("config")
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            break  # only top-level keys
        for key in ("model", "model_provider", "model_reasoning_effort"):
            if line.startswith(key + " ") or line.startswith(key + "="):
                _, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if value:
                    out[key] = value
    return out


class Collector(object):
    def __init__(self, homes=None, window_hours=12, project_dir=".", net=True, usage_ttl=60):
        self.homes = [paths.home_paths(h) for h in (homes or paths.discover_homes())]
        self.window_hours = window_hours
        self.project_dir = project_dir
        self.scanner = rollout.RolloutScanner()
        self.net = net
        self.usage = UsageRefresher(self.homes, project_dir, ttl=usage_ttl, net=net)
        self._usage_started = False

    def start(self):
        if not self._usage_started:
            self.usage.start()
            self._usage_started = True

    def stop(self):
        self.usage.stop()

    def sync_usage(self):
        """Blocking quota refresh, used by --once / --json."""
        if not self.net:
            return
        try:
            self.usage._refresh()
        except Exception:
            pass

    def snapshot(self, now=None):
        now = now or time.time()
        if self.net:
            self.start()
        snap = build_snapshot(self.homes, self.scanner, self.window_hours, now)
        quota = self.usage.read() if self.net else {
            "chatgpt": {"available": False, "error": "network disabled", "source": "none"},
            "opencode": {"configured": None, "ok": False, "windows": [], "error": "network disabled"},
        }
        snap["chatgpt"] = self._merge_chatgpt(quota.get("chatgpt") or {}, snap)
        snap["opencode"] = quota.get("opencode") or {}
        snap["usage"] = quota
        return snap

    def _merge_chatgpt(self, live, snap):
        """Live data wins; otherwise fall back to the newest cached rate_limits."""
        cached = snap.pop("_rate_limits", None)
        out = dict(live or {})
        if out.get("available"):
            out["source"] = "live"
        elif cached:
            live_error = out.get("error")
            out.update(cached)
            out["available"] = True
            out["source"] = "cache"
            out["cache_age_s"] = int(max(0, snap["ts"] - (cached.get("ts") or snap["ts"])))
            out["error"] = None
            if live_error:
                out["live_error"] = live_error
        else:
            out.setdefault("source", "none")
        # usage activity comes from local thread traffic, never from the quota API
        active = []
        for agent in snap.get("agents", []):
            if agent.get("provider_hint") in ("chatgpt", "openai") and agent.get("idle_s", 1e9) <= 60:
                active.append(agent["id"])
        out["active"] = bool(active)
        out["active_threads"] = active
        return out


def build_snapshot(homes, scanner, window_hours, now):
    errors = []
    proc_info = procs.snapshot(ignore_pid=None)
    by_pid = proc_info["by_pid"]

    roll = scanner.scan(homes, window_hours=max(window_hours, 24), now=now)
    errors.extend(roll.get("errors") or [])
    roll_threads = roll.get("threads") or {}

    agents = {}
    all_edges = {}
    events_acc = []

    for home in homes:
        home_name = home["home"].name
        threads, terr = state_db.load_threads(home, limit=120)
        if terr:
            errors.append("%s/state: %s" % (home_name, terr))
        edges, eerr = state_db.load_spawn_edges(home)
        if eerr:
            errors.append("%s/edges: %s" % (home_name, eerr))
        activity = logsdb.load_activity(home, window_hours=window_hours, now=now)
        errors.extend("%s/logs: %s" % (home_name, e) for e in activity.get("errors") or [])
        events_acc.extend(activity.get("events") or [])

        for tid, edge in edges.items():
            all_edges[tid] = edge["parent"]
        for tid, edge in (activity.get("edges") or {}).items():
            all_edges.setdefault(tid, edge["parent"])

        turns = {}
        tid_list = [t["id"] for t in threads if t.get("id")]
        if tid_list:
            turns, herr = state_db.load_turns(home, tid_list)
            if herr:
                errors.append("%s/turns: %s" % (home_name, herr))

        defaults = home_defaults(home)
        seen_ids = set()
        for row in threads:
            tid = row.get("id")
            if not tid:
                continue
            seen_ids.add(tid)
            agents[tid] = _from_thread(home_name, row, turns.get(tid), activity["threads"].get(tid), roll_threads.get(tid), now)
            agents[tid]["defaults"] = defaults

        # threads that only exist in logs (in-memory subagents / orphans)
        for tid, act in (activity.get("threads") or {}).items():
            if tid in agents:
                continue
            agents[tid] = _from_logs(home_name, tid, act, roll_threads.get(tid), now)
            agents[tid]["defaults"] = defaults
            seen_ids.add(tid)

        # rollout-only threads (state db lagging behind)
        for tid, rt in roll_threads.items():
            if tid in agents:
                continue
            if rt.get("provider") is None and rt.get("requests", 0) == 0:
                continue
            if not _recent(rt.get("last_ts"), now, window_hours):
                continue
            agents[tid] = _from_rollout(home_name, tid, rt, now)

    # parent / depth
    for tid, agent in agents.items():
        agent["parent"] = all_edges.get(tid)
    for tid, agent in agents.items():
        depth, cur, guard = 0, agent.get("parent"), 0
        while cur and cur in agents and guard < 8:
            depth += 1
            cur = agents[cur].get("parent")
            guard += 1
        agent["depth"] = depth if agent.get("parent") else 0
        if agent.get("parent") and agent["parent"] not in agents:
            agent["parent_missing"] = True

    for agent in agents.values():
        if not agent.get("label") and agent.get("parent") in agents:
            agent["label"] = "↳ " + (agents[agent["parent"]].get("label") or agents[agent["parent"]].get("tail") or "sub")
        # subagent threads do not record their model; codex inherits the parent's
        parent = agents.get(agent.get("parent") or "")
        if parent:
            for key in ("model", "provider", "effort"):
                if not agent.get(key) and parent.get(key):
                    agent[key] = parent[key]
                    if key == "model":
                        agent["model_inherited"] = True

    # state, spin, process binding
    for agent in agents.values():
        _finalize(agent, by_pid, now)

    kept = [a for a in agents.values() if _keep(a, now, window_hours)]
    kept.sort(key=lambda a: (STATE_ORDER.get(a["state"], 9), -a["last_seen"]))
    kept = _tree_order(kept)[:28]
    # display depth must only count parents that are actually on screen,
    # otherwise a child renders with a "└" but no parent above it
    kept_ids = {a["id"] for a in kept}
    for agent in kept:
        depth, cur, guard = 0, agent.get("parent"), 0
        while cur in kept_ids and guard < 8:
            depth += 1
            cur = agents[cur].get("parent") if cur in agents else None
            guard += 1
        agent["depth"] = depth
    for agent in kept:
        # internal source rows are not part of the public snapshot
        for key in ("act", "roll", "turn", "defaults"):
            agent.pop(key, None)

    chatgpt_cache = _cached_rate_limits(scanner, now)
    events_acc.sort(key=lambda e: e["ts"], reverse=True)

    total_tokens = sum(a.get("tokens_total") or 0 for a in kept)
    tokens_partial = any(
        a.get("tokens_total") is None and (a.get("requests") or a.get("ctx_tokens")) for a in kept
    )
    return {
        "ts": now,
        "homes": [h["home"].name for h in homes],
        "agents": kept,
        "totals": {
            "agents": len(kept),
            "live": sum(1 for a in kept if a["state"] in ("RUN", "WORK")),
            "subs": sum(1 for a in kept if a.get("role") == "sub"),
            "tokens": total_tokens,
            "tokens_partial": tokens_partial,
            "spinning": sum(1 for a in kept if a.get("spin")),
        },
        "processes": proc_info["list"],
        "events": events_acc[:30],
        "sources": {"errors": [e for e in errors if e and not e.endswith(": missing")]},
        "_rate_limits": chatgpt_cache,
        "providers": _provider_activity(agents, now),
    }


def _provider_activity(agents, now):
    out = {}
    for agent in agents.values():
        key = agent.get("provider") or "?"
        slot = out.setdefault(key, {"last_seen": 0, "threads": 0, "active": False})
        slot["threads"] += 1
        slot["last_seen"] = max(slot["last_seen"], agent.get("last_seen") or 0)
    for slot in out.values():
        slot["active"] = bool(slot["last_seen"]) and (now - slot["last_seen"]) <= 60
    return out


def _recent(ts, now, window_hours):
    return bool(ts) and (now - ts) <= window_hours * 3600


def _from_thread(home_name, row, turn_bucket, act, rt, now):
    created = row.get("created_at") or 0
    agent = {
        "id": row.get("id"),
        "short": _short(row.get("id")),
        "home": home_name,
        "role": "sub" if (row.get("agent_path") or all_hint(row)) else "main",
        "title": (row.get("title") or row.get("name") or "").strip(),
        "name": (row.get("name") or "").strip(),
        "preview": (row.get("preview") or "").strip(),
        "first_user_message": (row.get("first_user_message") or "").strip(),
        "cwd": row.get("cwd") or "",
        "provider": row.get("model_provider") or "",
        "model": row.get("model") or "",
        "effort": row.get("reasoning_effort") or "",
        "agent_path": row.get("agent_path"),
        "agent_nickname": row.get("agent_nickname"),
        "agent_role": row.get("agent_role"),
        "thread_source": row.get("thread_source") or "user",
        "sandbox": row.get("sandbox_kind") or "",
        "source": row.get("source") or "",
        "cli_version": row.get("cli_version") or "",
        "created_at": created,
        "updated_at": row.get("updated_at") or 0,
        "tokens_state": int(row.get("tokens_used") or 0),
        "rollout_path": row.get("rollout_path"),
        "turn": turn_bucket,
        "act": act or {},
        "roll": rt or {},
        "archived": bool(row.get("archived")),
    }
    return agent


def all_hint(row):
    return (row.get("thread_source") or "") in ("subagent", "agent")


def _from_logs(home_name, tid, act, rt, now):
    return {
        "id": tid,
        "short": _short(tid),
        "home": home_name,
        "role": "sub",
        "title": "",
        "cwd": (rt or {}).get("cwd") or "",
        "provider": (rt or {}).get("provider") or "",
        "model": (rt or {}).get("model") or "",
        "effort": (rt or {}).get("effort") or "",
        "agent_path": None,
        "agent_nickname": None,
        "agent_role": "subagent",
        "thread_source": "subagent",
        "sandbox": "",
        "source": "logs",
        "cli_version": "",
        "created_at": act.get("first_ts") or 0,
        "updated_at": act.get("last_ts") or 0,
        "tokens_state": 0,
        "rollout_path": None,
        "turn": None,
        "act": act,
        "roll": rt or {},
        "archived": False,
    }


def _from_rollout(home_name, tid, rt, now):
    return {
        "id": tid,
        "short": _short(tid),
        "home": home_name,
        "role": "main",
        "title": "",
        "cwd": rt.get("cwd") or "",
        "provider": rt.get("provider") or "",
        "model": rt.get("model") or "",
        "effort": rt.get("effort") or "",
        "agent_path": None,
        "agent_nickname": None,
        "agent_role": None,
        "thread_source": rt.get("thread_source") or "user",
        "sandbox": "",
        "source": rt.get("originator") or "rollout",
        "cli_version": "",
        "created_at": rt.get("first_ts") or 0,
        "updated_at": rt.get("last_ts") or 0,
        "tokens_state": 0,
        "rollout_path": None,
        "turn": None,
        "act": {},
        "roll": rt,
        "archived": False,
    }


def _finalize(agent, by_pid, now):
    act = agent.get("act") or {}
    roll = agent.get("roll") or {}
    turn_bucket = agent.get("turn") or {}

    last_candidates = [
        act.get("last_ts") or 0,
        agent.get("updated_at") or 0,
        roll.get("last_ts") or 0,
    ]
    last_seen = max(last_candidates)
    agent["last_seen"] = last_seen
    agent["idle_s"] = int(max(0, now - last_seen)) if last_seen else None
    agent["age_s"] = int(max(0, now - (agent.get("created_at") or last_seen or now))) if (agent.get("created_at") or last_seen) else None

    pid = None
    pids = act.get("pids") or []
    live_pids = [p for p in pids if p in by_pid]
    if live_pids:
        pid = live_pids[0]
    elif pids:
        pid = pids[-1]
    agent["pid"] = pid
    agent["pid_alive"] = bool(pid and pid in by_pid)
    agent["proc"] = by_pid.get(pid) if pid else None

    # context / tokens
    ctx = act.get("ctx_tokens")
    ctx_limit = act.get("ctx_limit")
    if ctx is None:
        # cumulative thread usage is NOT context fill; last request input is
        ctx = roll.get("last_input_tokens")
    if ctx_limit is None:
        ctx_limit = roll.get("ctx_limit")
    usage_sum = roll.get("usage") or {}
    turn_usage = act.get("turn_usage") or {}
    total = agent.get("tokens_state") or 0
    roll_total = int(usage_sum.get("total_tokens") or 0)
    # absence of data is not zero: in-memory subagents have neither a rollout
    # nor a state-db tokens_used, so report "unknown" instead of a fake 0
    agent["tokens_total"] = max(total, roll_total) if (total or roll_total) else None
    agent["tokens_breakdown"] = {
        "input": int(usage_sum.get("input_tokens") or turn_usage.get("input_tokens") or 0),
        "cached": int(usage_sum.get("cached_input_tokens") or turn_usage.get("cached_input_tokens") or 0),
        "output": int(usage_sum.get("output_tokens") or turn_usage.get("output_tokens") or 0),
        "reasoning": int(usage_sum.get("reasoning_output_tokens") or turn_usage.get("reasoning_output_tokens") or 0),
    }
    agent["ctx_tokens"] = int(ctx) if ctx else None
    agent["ctx_limit"] = int(ctx_limit) if ctx_limit else None
    agent["ctx_percent"] = round(100.0 * ctx / ctx_limit, 1) if (ctx and ctx_limit) else None
    agent["ctx_over"] = bool(ctx and ctx_limit and ctx > ctx_limit)
    agent["requests"] = int(roll.get("requests") or act.get("sampling") or 0)

    # turn info
    latest = (turn_bucket or {}).get("latest") or {}
    agent["turn_status"] = latest.get("status")
    agent["turn_id"] = latest.get("turn_id")
    agent["turn_ms"] = state_db.turn_duration_ms(latest, now)
    agent["turn_failed"] = (turn_bucket or {}).get("failed") or 0
    agent["turn_interrupted"] = (turn_bucket or {}).get("interrupted") or 0
    agent["turn_count"] = (turn_bucket or {}).get("count") or 0
    agent["error"] = latest.get("error_message")
    if not agent["error"] and roll.get("last_error"):
        agent["error"] = roll.get("last_error")

    # tools
    counts = dict(act.get("tool_counts") or {})
    agent["tool_counts"] = counts
    agent["tool_total"] = act.get("tool_total") or 0
    agent["tool_ms"] = act.get("tool_ms") or 0
    agent["last_tool"] = act.get("last_tool") or "—"
    agent["last_tool_ts"] = act.get("last_tool_ts")

    # orchestration health: two livelock shapes seen in the wild
    #   a) wait_agent keeps timing out and nothing ever calls close_agent
    #   b) pure polling (list_agents/wait/send) with zero productive tools
    waits = act.get("wait_timeouts") or 0
    orch_tools = ("wait_agent", "list_agents", "send_message", "followup_task",
                  "interrupt_agent", "resume_agent", "send_input")
    orch_calls = sum(counts.get(name, 0) for name in orch_tools)
    productive = (act.get("patch_calls") or 0) + (act.get("exec_calls") or 0)
    fresh = bool(last_seen) and (now - last_seen) <= SPIN_WINDOW
    spins = bool(
        fresh
        and (act.get("close_calls") or 0) == 0
        and (
            (waits >= SPIN_MIN_WAITS and productive <= 2)
            or (orch_calls >= 15 and productive == 0)
        )
    )
    agent["spin"] = spins
    agent["spin_detail"] = {
        "wait_timeouts": waits,
        "close_calls": act.get("close_calls") or 0,
        "patch_calls": act.get("patch_calls") or 0,
        "orch_calls": orch_calls,
        "errors": act.get("errors") or 0,
    }

    # state machine (AGENTS.md §5)
    task_done = (roll.get("task_state") == "completed")
    # the exit marker only counts while nothing newer arrived (resume clears it)
    if act.get("exited_at") and (act["exited_at"] or 0) >= (last_seen - 5):
        state = "EXIT"
    elif task_done and not agent["pid_alive"] and (agent["idle_s"] or 0) > 60:
        state = "EXIT"
    elif last_seen and (now - last_seen) <= RUN_WINDOW and (agent["pid_alive"] or not pid):
        state = "RUN"
    elif last_seen and (now - last_seen) <= WORK_WINDOW:
        state = "WORK"
    elif last_seen and (now - last_seen) <= WAIT_WINDOW:
        state = "WAIT"
    elif last_seen and (now - last_seen) <= IDLE_WINDOW:
        state = "IDLE"
    else:
        # an orphan is a subagent that never reported an exit but is still
        # hosted by a live process (the 0.169 livelock shape). Dead subs and
        # idle main threads are not alarms.
        is_sub = (agent.get("role") == "sub") or bool(act.get("spawned_from"))
        already_done = (agent.get("turn_status") or "").lower() in (
            "completed", "failed", "interrupted", "aborted",
        )
        if is_sub and not already_done and (agent["pid_alive"] or agent["spin"]):
            state = "ORPHAN"
        elif is_sub:
            state = "EXIT"
        else:
            state = "IDLE"
    if agent["spin"] and state not in ("RUN", "WORK"):
        agent["state_note"] = "SPIN"
    agent["state"] = state

    # human-readable session label: codex's own title/name beats a hex id
    label = (
        agent.get("name")
        or agent.get("title")
        or agent.get("first_user_message")
        or agent.get("preview")
        or (act.get("first_msg") if act else "")
        or ""
    )
    label = " ".join(str(label).split())
    if not agent.get("model"):
        defaults = agent.get("defaults") or {}
        if defaults.get("model"):
            agent["model"] = defaults["model"]
            agent["model_inferred"] = True
        if not agent.get("provider") and defaults.get("model_provider"):
            agent["provider"] = defaults["model_provider"]
        if not agent.get("effort") and defaults.get("model_reasoning_effort"):
            agent["effort"] = defaults["model_reasoning_effort"]
    if not label and agent.get("agent_path"):
        label = str(agent["agent_path"])
    if not label and agent.get("agent_nickname"):
        label = str(agent["agent_nickname"])
    agent["label"] = label
    agent["tail"] = "#" + (agent.get("id") or "")[-6:]

    # provider hint for quota activity
    provider = (agent.get("provider") or "").lower()
    if provider in ("openai", "chatgpt") or "chatgpt" in provider:
        agent["provider_hint"] = "chatgpt"
    else:
        agent["provider_hint"] = provider

    agent["children"] = act.get("spawned_children") or []
    if not agent["role"] or agent["role"] == "main":
        agent["role"] = "sub" if (agent.get("parent") and agent["thread_source"] != "user") else ("main" if not agent.get("parent") else "sub")


def _tree_order(agents):
    """Emit parents immediately followed by their descendants."""
    by_id = {a["id"]: a for a in agents}
    children = {}
    roots = []
    for agent in agents:
        parent = agent.get("parent")
        if parent and parent in by_id and parent != agent["id"]:
            children.setdefault(parent, []).append(agent)
        else:
            roots.append(agent)

    ordered = []
    seen = set()

    def emit(agent, depth=0):
        if agent["id"] in seen or depth > 6:
            return
        seen.add(agent["id"])
        ordered.append(agent)
        for child in children.get(agent["id"], []):
            emit(child, depth + 1)

    for root in roots:
        emit(root)
    for agent in agents:  # cycles / orphans
        emit(agent)
    return ordered


def _keep(agent, now, window_hours):
    if agent.get("archived"):
        return False
    if agent.get("pid_alive"):
        return True
    if agent["state"] in ("RUN", "WORK", "WAIT"):
        return True
    last = agent.get("last_seen") or 0
    if not last or (now - last) > window_hours * 3600:
        return False
    # probe sessions (`codex exec` one-shots) leave empty EXIT rows; drop the stale ones
    if (
        agent["state"] == "EXIT"
        and not agent.get("title")
        and not agent.get("tokens_total")
        and (now - last) > 600
    ):
        return False
    return True


def _cached_rate_limits(scanner, now):
    limits = scanner.rate_limits
    if not limits:
        return None
    payload = limits.get("payload") or {}
    normalized = usage.normalize_limits(payload)
    normalized["ts"] = limits.get("ts") or now
    normalized["age_s"] = int(max(0, now - (limits.get("ts") or now)))
    return normalized
