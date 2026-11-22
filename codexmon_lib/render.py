"""Text formatting helpers + the non-curses renderer (--once / --json)."""

import datetime
import json
import time
import unicodedata

STATE_LABEL = {
    "RUN": "RUN",
    "WORK": "WORK",
    "WAIT": "WAIT",
    "IDLE": "IDLE",
    "ORPHAN": "ORPHAN",
    "EXIT": "EXIT",
}


def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_w(text):
    return sum(char_width(c) for c in (text or ""))


def clip(text, width):
    """Clip to `width` display cells, adding an ellipsis when truncated."""
    text = text if text is not None else ""
    if width <= 0:
        return ""
    if disp_w(text) <= width:
        return text
    out = []
    used = 0
    limit = max(0, width - 1)
    for ch in text:
        w = char_width(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def pad(text, width, align="left"):
    text = clip(text, width)
    gap = width - disp_w(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def fmt_int(n):
    try:
        return "{:,}".format(int(n))
    except (TypeError, ValueError):
        return "—"


def fmt_tokens(n):
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n >= 1e9:
        return "%.2fG" % (n / 1e9)
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%.0fk" % (n / 1e3)
    return "%d" % n


def fmt_dur(seconds):
    if seconds is None:
        return "—"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return "%dm%02ds" % (minutes, sec)
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh%02dm" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%dd%02dh" % (days, hours)


def fmt_ms(ms):
    if ms is None:
        return "—"
    return fmt_dur(ms / 1000.0)


def fmt_ago(ts, now=None):
    """Precise relative time for the LAST column: now / 7s / 3m12s / 2h05m."""
    if not ts:
        return "—"
    now = now or time.time()
    delta = int(max(0, now - ts))
    if delta < 3:
        return "now"
    return fmt_dur(delta)


def fmt_age(seconds):
    """Coarse age for the AGE column: 56s / 12m / 1h46m / 3d22h.

    Deliberately drops seconds once we are past the first minute: nobody needs
    to read "1h46m12s" to know a session has been alive for a while.
    """
    if seconds is None:
        return "—"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh%02dm" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%dd%02dh" % (days, hours)


def fmt_clock(ts):
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_reset(epoch, now=None):
    if not epoch:
        return "—"
    now = now or time.time()
    dt = datetime.datetime.fromtimestamp(epoch)
    delta = int(epoch - now)
    if delta <= 0:
        return "reset now"
    same_day = dt.date() == datetime.date.fromtimestamp(now)
    when = dt.strftime("%H:%M") if same_day else dt.strftime("%m-%d %H:%M")
    return "reset %s (in %s)" % (when, fmt_dur(delta))


def bar(percent, width=16):
    if percent is None:
        return "░" * width
    try:
        pct = max(0.0, min(100.0, float(percent)))
    except (TypeError, ValueError):
        return "░" * width
    filled = int(round(pct / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def pct(percent, digits=0):
    if percent is None:
        return "—"
    return ("%%.%df%%%%" % digits) % float(percent)


def session_label(agent):
    """Human session name: codex title/name, else a short unique tail."""
    return agent.get("label") or agent.get("tail") or ("#" + (agent.get("id") or "?")[-6:])


def role_label(agent):
    if agent.get("role") == "sub":
        depth = agent.get("depth") or 0
        if depth <= 0:
            return "· SUB"
        return "  " * max(0, depth - 1) + "└ SUB"
    return "● MAIN"


def model_label(agent):
    model = agent.get("model") or "?"
    if (agent.get("model_inferred") or agent.get("model_inherited")) and model != "?":
        model = "~" + model
    effort = agent.get("effort") or ""
    if effort:
        short = {"minimal": "min", "low": "low", "medium": "med", "high": "hi", "max": "max"}
        model = "%s/%s" % (model, short.get(effort, effort))
    return model


def ctx_label(agent):
    ctx = agent.get("ctx_tokens")
    limit = agent.get("ctx_limit")
    if ctx is None:
        return "—"
    if not limit:
        return fmt_tokens(ctx)
    shown = "100%+" if agent.get("ctx_over") else pct(agent.get("ctx_percent"), 0)
    return "%s %s" % (shown, fmt_tokens(ctx))


def tools_label(agent, width=40):
    counts = agent.get("tool_counts") or {}
    parts = []
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1])[:4]:
        parts.append("%s×%d" % (name, count))
    text = " ".join(parts) if parts else "—"
    if agent.get("spin"):
        # keep the badge visible even when the column is narrow
        text = "SPIN! " + text
    return clip(text, width)


def header_line(snap, width=120):
    totals = snap.get("totals") or {}
    stamp = datetime.datetime.fromtimestamp(snap["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    text = "codexmon · %s · homes %s · %d agents (%d live, %d sub) · %s tok" % (
        stamp,
        ",".join(snap.get("homes") or []) or "none",
        totals.get("agents", len(snap.get("agents") or [])),
        totals.get("live", 0),
        totals.get("subs", 0),
        fmt_tokens(totals.get("tokens")) + ("+" if totals.get("tokens_partial") else ""),
    )
    if totals.get("spinning"):
        text += " · %d SPIN" % totals["spinning"]
    return clip(text, width)


# ---------------------------------------------------------------- plain text
def plain(snap, width=118):
    lines = []
    lines.append(header_line(snap, width))
    lines.append("─" * min(width, 118))
    lines.append(plain_agents(snap, width))
    lines.append("─" * min(width, 118))
    lines.append(plain_plan(snap, width))
    lines.append("─" * min(width, 118))
    lines.append(plain_events(snap, width))
    procs = proc_line(snap, width)
    if procs:
        lines.append(procs)
    errs = (snap.get("sources") or {}).get("errors") or []
    if errs:
        lines.append("─" * min(width, 118))
        lines.append("SOURCES  " + clip("; ".join(errs[:4]), width - 9))
    return "\n".join(lines)


def plain_agents(snap, width):
    if width >= 165:
        name_w, model_w = 34, 19
    elif width >= 140:
        name_w, model_w = 30, 19
    elif width >= 118:
        name_w, model_w = 26, 19
    elif width >= 104:
        name_w, model_w = 22, 12
    elif width >= 90:
        name_w, model_w = 18, 12
    else:
        name_w, model_w = 14, 0
    with_pid = width >= 125
    headers = ["ST", "ROLE", "SESSION"] + (["MODEL"] if model_w else []) + [
        "CTX", "TOKENS", "AGE", "LAST",
    ] + (["PID"] if with_pid else [])
    widths = [6, 9, name_w] + ([model_w] if model_w else []) + [11, 8, 8, 7] + ([6] if with_pid else [])
    right = {headers.index("TOKENS")}  # survives optional MODEL/PID columns
    tool_w = max(0, width - sum(widths) - len(widths) - 1)

    def line(values, tail=""):
        cells = []
        for idx, (value, w) in enumerate(zip(values, widths)):
            cells.append(pad(str(value), w, "right" if idx in right else "left"))
        text = " ".join(cells)
        if tool_w >= 8 and tail:
            text += " " + pad(tail, tool_w)
        return clip(text, width).rstrip()

    out = ["AGENTS", line(headers, "TOOLS")]
    agents = snap.get("agents") or []
    if not agents:
        out.append("  (no codex thread in window)")
    for agent in agents:
        values = [STATE_LABEL.get(agent["state"], agent["state"]), role_label(agent), session_label(agent)]
        if model_w:
            values.append(model_label(agent))
        values += [
            ctx_label(agent),
            fmt_tokens(agent.get("tokens_total")),
            fmt_age(agent.get("age_s")),
            fmt_ago(agent.get("last_seen"), snap["ts"]),
        ]
        if with_pid:
            values.append(agent.get("pid") or "—")
        out.append(line(values, tools_label(agent, tool_w)))
        extra = turn_label(agent)
        if extra:
            out.append("      " + clip(extra, width - 8))
        if agent.get("title"):
            out.append("      " + clip(agent["title"], width - 8))
    return "\n".join(out)


def turn_label(agent):
    """Human summary of the thread's latest turn: state + duration + error."""
    bits = []
    status = agent.get("turn_status")
    dur = agent.get("turn_ms")
    if status:
        bits.append("turn %s %s" % (status, fmt_ms(dur) if dur else ""))
    elif dur:
        bits.append("turn %s" % fmt_ms(dur))
    if agent.get("turn_failed"):
        bits.append("failed×%d" % agent["turn_failed"])
    if agent.get("tokens_total") is None and agent.get("requests"):
        bits.append("tokens unknown (req=%d)" % agent["requests"])
    if agent.get("error"):
        bits.append(clip(agent["error"], 90))
    if agent.get("spin"):
        spread = agent.get("spin_detail") or {}
        bits.append(
            "SPIN wait_timeout=%s poll=%s close=%s patch=%s"
            % (
                spread.get("wait_timeouts"),
                spread.get("orch_calls"),
                spread.get("close_calls"),
                spread.get("patch_calls"),
            )
        )
    return "  ".join(b for b in bits if b)


def plain_plan(snap, width):
    now = snap["ts"]
    out = []
    cg = snap.get("chatgpt") or {}
    label = "CHATGPT"
    src = cg.get("source") or "none"
    active = "● 使用中" if cg.get("active") else "○ 空闲"
    if not cg.get("available"):
        out.append("%s  %s  %s  (%s)" % (label, "unavailable", active, cg.get("error") or "no data"))
    else:
        out.append("%s  source=%s age=%s  %s" % (label, src, fmt_dur(cg.get("cache_age_s")), active))
        for key, name in (("primary", "5h  "), ("secondary", "week")):
            win = cg.get(key)
            if not win:
                out.append("  %s  —" % name)
                continue
            out.append(
                "  %s %s %6s  left %6s  %s"
                % (
                    name,
                    bar(win.get("used_percent"), 18),
                    pct(win.get("used_percent"), 1),
                    pct(100 - (win.get("used_percent") or 0), 1),
                    fmt_reset(win.get("resets_at"), now),
                )
            )
        credits = cg.get("credits") or {}
        out.append(
            "  credits: %s   plan: %s"
            % (
                ("unlimited" if credits.get("unlimited") else credits.get("balance", "—")),
                cg.get("plan_type") or "—",
            )
        )

    oc = snap.get("opencode") or {}
    active = "● 调用中" if (snap.get("providers") or {}).get("opencode-go", {}).get("active") else "○ 空闲"
    if oc.get("configured") is None:
        out.append("OPENCODE GO  --no-usage（不联网，未查额度）  %s" % active)
    elif not oc.get("configured"):
        out.append("OPENCODE GO  key 未配置（.env 里填 OPENCODE_GO_API_KEY）  %s" % active)
    elif not oc.get("ok"):
        out.append(
            "OPENCODE GO  key %s (%s)  %s  err=%s"
            % (oc.get("key") or "—", oc.get("key_source") or "?", active, clip(oc.get("error") or "?", 40))
        )
    else:
        out.append("OPENCODE GO  key %s (%s)  %s" % (oc.get("key") or "—", oc.get("key_source") or "?", active))
        for win in oc.get("windows") or []:
            out.append(
                "  %-8s %s %6s  left %6s  %s  [%s]"
                % (
                    win["name"],
                    bar(win.get("percent"), 18),
                    pct(win.get("percent"), 0),
                    pct(100 - (win.get("percent") or 0), 0),
                    fmt_reset(win.get("resets_at"), now),
                    win.get("status") or "?",
                )
            )
    return "\n".join(out)


def proc_line(snap, width=118):
    items = []
    for row in snap.get("processes") or []:
        if row.get("kind") in ("daemon", "app-server", "tui"):
            items.append("%s:%s" % (row["kind"], row["pid"]))
    if not items:
        return ""
    return clip("PROCESSES  " + "  ".join(items[:6]), width)


def plain_events(snap, width):
    out = ["EVENTS"]
    events = snap.get("events") or []
    if not events:
        out.append("  (nothing in the last few hours)")
    for ev in events[:12]:
        text = "  %s %-9s %-18s %8s  %s" % (
            fmt_clock(ev.get("ts")),
            (ev.get("thread") or "?")[:8],
            clip(ev.get("tool") or "?", 18),
            fmt_ms(ev.get("ms")),
            "" if ev.get("ok", True) else "FAIL " + clip(ev.get("text") or "", max(10, width - 60)),
        )
        out.append(clip(text.rstrip(), width))
    return "\n".join(out)


def to_json(snap):
    return json.dumps(snap, ensure_ascii=False, indent=2, default=str)
