"""Incremental reader for codex rollout jsonl files.

Rollouts are append-only, so we keep a byte offset per file and only consume new
lines. They give us per-request token usage (main threads only) and the ChatGPT
plan rate limits.
"""

import json
import os
import time

MAX_WINDOW_HOURS = 72
REQUEST_HISTORY_SECONDS = 900


class RolloutScanner(object):
    def __init__(self):
        self.files = {}
        self.rate_limits = None
        self.errors = []

    # -- file bookkeeping -------------------------------------------------
    def _new_state(self):
        return {
            "offset": 0,
            "thread_id": None,
            "provider": None,
            "cwd": None,
            "thread_source": None,
            "originator": None,
            "model": None,
            "effort": None,
            "usage": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
            },
            "requests": 0,
            "request_ts": [],
            "first_ts": None,
            "last_ts": None,
            "ctx_total": None,
            "ctx_limit": None,
            "last_input_tokens": None,
            "turn_started": None,
            "turn_completed": None,
            "task_state": None,
            "errors": 0,
            "last_error": None,
        }

    def _reset(self, state):
        keep = self._new_state()
        state.clear()
        state.update(keep)

    def scan(self, homes, window_hours=48, max_files=40, now=None):
        now = now or time.time()
        cutoff = now - min(window_hours, MAX_WINDOW_HOURS) * 3600
        paths = []
        for home in homes:
            sessions = home["sessions"]
            if not sessions.is_dir():
                continue
            try:
                for path in sessions.glob("*/*/*/*.jsonl"):
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    if st.st_mtime < cutoff:
                        continue
                    paths.append((st.st_mtime, st.st_size, path))
            except OSError as exc:
                self.errors.append("%s: %s" % (sessions, exc))
        paths.sort(reverse=True)
        paths = paths[:max_files]

        seen = set()
        for _, size, path in paths:
            key = str(path)
            seen.add(key)
            state = self.files.get(key)
            if state is None:
                state = self._new_state()
                self.files[key] = state
            try:
                if size < state["offset"]:
                    self._reset(state)
                if size == state["offset"]:
                    continue
                self._consume(path, state, now)
            except Exception as exc:
                self.errors.append("%s: %s" % (path.name, exc))

        # drop files that fell out of the window
        for key in list(self.files):
            if key not in seen:
                del self.files[key]

        threads = {}
        for state in self.files.values():
            tid = state["thread_id"]
            if not tid:
                continue
            entry = threads.get(tid)
            if entry is None:
                entry = dict(state)
                entry["request_ts"] = list(state["request_ts"])
                entry["usage"] = dict(state["usage"])
                threads[tid] = entry
            else:
                # a subagent rollout can reuse the parent's session id; merge
                # the fields the first file may have left empty
                entry["requests"] += state["requests"]
                entry["request_ts"] = sorted(set(entry["request_ts"]) | set(state["request_ts"]))[-200:]
                for k, v in state["usage"].items():
                    entry["usage"][k] = entry["usage"].get(k, 0) + v
                for key in (
                    "thread_id", "provider", "cwd", "model", "effort", "thread_source",
                    "originator", "last_input_tokens", "ctx_total", "ctx_limit", "task_state",
                ):
                    if not entry.get(key) and state.get(key):
                        entry[key] = state[key]
                if state["first_ts"] and (not entry["first_ts"] or state["first_ts"] < entry["first_ts"]):
                    entry["first_ts"] = state["first_ts"]
                if state["last_ts"] and (not entry["last_ts"] or state["last_ts"] > entry["last_ts"]):
                    entry["last_ts"] = state["last_ts"]
                if state.get("task_state") == "completed":
                    entry["task_state"] = "completed"
                elif state.get("task_state") == "running" and entry.get("task_state") != "completed":
                    entry["task_state"] = "running"
        return {"threads": threads, "rate_limits": self.rate_limits, "errors": self.errors}

    # -- parsing ----------------------------------------------------------
    def _consume(self, path, state, now):
        with open(path, "rb") as fh:
            fh.seek(state["offset"])
            chunk = fh.read()
        if not chunk:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return  # partial line, wait for more
        usable = chunk[: end + 1]
        state["offset"] += len(usable)
        text = usable.decode("utf-8", "replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            try:
                self._one(rec, state, now)
            except Exception:
                continue

    def _one(self, rec, state, now):
        rtype = rec.get("type")
        payload = rec.get("payload") or {}
        ts = _iso_to_epoch(rec.get("timestamp")) or state["last_ts"]

        if rtype == "session_meta":
            state["thread_id"] = payload.get("session_id") or payload.get("id")
            state["provider"] = payload.get("model_provider")
            state["cwd"] = payload.get("cwd")
            state["thread_source"] = payload.get("thread_source")
            state["originator"] = payload.get("originator")
            if payload.get("timestamp"):
                state["first_ts"] = _iso_to_epoch(payload.get("timestamp"))
            return

        if rtype == "turn_context":
            state["model"] = payload.get("model") or state["model"]
            collab = payload.get("collaboration_mode") or {}
            settings = collab.get("settings") or {}
            state["effort"] = (
                settings.get("reasoning_effort")
                or payload.get("reasoning_effort")
                or state["effort"]
            )
            return

        if rtype == "token_usage_record":
            usage = payload.get("usage") or {}
            for key in state["usage"]:
                state["usage"][key] += int(usage.get(key) or 0)
            state["requests"] += 1
            if ts:
                state["request_ts"].append(ts)
                state["request_ts"] = [t for t in state["request_ts"] if t >= now - REQUEST_HISTORY_SECONDS]
            cum = payload.get("thread_token_usage") or {}
            if cum.get("total_tokens"):
                state["ctx_total"] = int(cum["total_tokens"])
            if usage.get("input_tokens"):
                # last request's input size is the best context estimate here
                state["last_input_tokens"] = int(usage["input_tokens"])
            if ts and (not state["last_ts"] or ts > state["last_ts"]):
                state["last_ts"] = ts
            return

        if rtype != "event_msg":
            return

        etype = payload.get("type")
        if etype == "token_count":
            info = payload.get("info") or {}
            if info.get("model_context_window"):
                state["ctx_limit"] = int(info["model_context_window"])
            total = info.get("total_token_usage") or {}
            if total.get("total_tokens"):
                state["ctx_total"] = int(total["total_tokens"])
            limits = payload.get("rate_limits") or {}
            if limits and (limits.get("primary") or limits.get("secondary")):
                if self.rate_limits is None or (ts or 0) >= self.rate_limits.get("ts", 0):
                    self.rate_limits = {
                        "ts": ts or now,
                        "payload": limits,
                        "path": str(getattr(self, "_current_path", "")),
                    }
        elif etype == "task_started":
            state["turn_started"] = ts
            state["turn_completed"] = None
            state["task_state"] = "running"
        elif etype in ("task_complete", "task_completed", "turn_completed"):
            state["turn_completed"] = ts
            state["task_state"] = "completed"
        elif etype in ("turn_aborted", "task_aborted", "error"):
            state["turn_completed"] = ts
            state["task_state"] = "aborted"
            state["errors"] += 1
            err = payload.get("message") or payload.get("error")
            if err:
                state["last_error"] = str(err)[:300]

        if ts and (not state["last_ts"] or ts > state["last_ts"]):
            state["last_ts"] = ts


def _iso_to_epoch(value):
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        # python 3.9 fromisoformat does not accept 6-digit fractions
        if "." in s:
            head, rest = s.split(".", 1)
            frac = ""
            i = 0
            while i < len(rest) and rest[i].isdigit():
                frac += rest[i]
                i += 1
            s = head + "." + frac[:6] + rest[i:]
        import datetime

        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:
        return None
