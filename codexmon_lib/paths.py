"""Discover CODEX_HOME directories and their data files."""

import os
from pathlib import Path

MARKERS = ("state_5.sqlite", "logs_2.sqlite", "sessions")


def _looks_like_home(path):
    p = Path(path)
    if not p.is_dir():
        return False
    return any((p / m).exists() for m in MARKERS)


def discover_homes(explicit=None):
    """Return the list of codex homes to monitor.

    Priority:
      1. explicit CLI/API list
      2. CODEX_MONITOR_HOMES (colon separated)
      3. ~/.codex plus sibling ~/.codex-* dirs that look like a codex home
    """
    if explicit:
        return [Path(p).expanduser() for p in explicit if p]
    env = os.environ.get("CODEX_MONITOR_HOMES", "").strip()
    if env:
        return [Path(p).expanduser() for p in env.split(":") if p]
    root = Path(os.path.expanduser("~"))
    homes = []
    default = root / ".codex"
    if _looks_like_home(default):
        homes.append(default)
    for p in sorted(root.glob(".codex-*")):
        if p == default:
            continue
        if _looks_like_home(p):
            homes.append(p)
    return homes


def home_paths(home):
    home = Path(home)
    return {
        "home": home,
        "state_db": home / "state_5.sqlite",
        "history_db": home / "thread_history_1.sqlite",
        "logs_db": home / "logs_2.sqlite",
        "sessions": home / "sessions",
        "config": home / "config.toml",
        "auth": home / "auth.json",
    }
