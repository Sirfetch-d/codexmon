"""Process table lookups via ps(1)."""

import subprocess
import time

_CACHE = {"ts": 0.0, "rows": []}
CACHE_TTL = 1.5


def _ps_rows():
    now = time.time()
    if _CACHE["rows"] and (now - _CACHE["ts"]) < CACHE_TTL:
        return _CACHE["rows"]
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid,ppid,etime,rss,pcpu,command"],
            capture_output=True,
            timeout=3,
        )
        text = out.stdout.decode("utf-8", "replace")
    except Exception:
        return []
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid, ppid, etime, rss, pcpu, command = parts
        try:
            pid_i = int(pid)
            ppid_i = int(ppid)
            rss_i = int(rss)
            cpu_f = float(pcpu)
        except ValueError:
            continue
        rows.append(
            {
                "pid": pid_i,
                "ppid": ppid_i,
                "etime": etime,
                "rss_kb": rss_i,
                "cpu": cpu_f,
                "command": command,
            }
        )
    _CACHE["ts"] = now
    _CACHE["rows"] = rows
    return rows


def _classify(command):
    cmd = command or ""
    if "app-server daemon pid-update-loop" in cmd:
        return "daemon-loop"
    if "--managed-daemon" in cmd:
        return "daemon"
    if "app-server --listen" in cmd:
        return "app-server"
    if "Codex Computer Use" in cmd or "SkyComputerUse" in cmd:
        return "helper"
    if " exec " in " %s " % cmd or cmd.strip().startswith("codex exec"):
        return "exec"
    if "/codex" in cmd or cmd.strip() == "codex" or cmd.startswith("codex "):
        return "tui"
    if "codex" in cmd:
        return "other"
    return "other"


def snapshot(extra_pids=(), ignore_pid=None):
    """All codex-ish processes plus a pid -> process map."""
    rows = _ps_rows()
    wanted = set(extra_pids or ())
    keep = []
    for row in rows:
        cmd = row["command"]
        if ignore_pid and row["pid"] == ignore_pid:
            continue
        if "codexmon" in cmd:
            continue
        if "codex" not in cmd and row["pid"] not in wanted:
            continue
        row["kind"] = _classify(cmd)
        keep.append(row)
    by_pid = {}
    for row in rows:
        by_pid[row["pid"]] = row
    return {"list": keep, "by_pid": by_pid}


def alive(by_pid, pid):
    return bool(pid) and pid in by_pid


def describe(row, width=90):
    cmd = row.get("command") or ""
    if len(cmd) > width:
        cmd = "…" + cmd[-width:]
    return cmd
