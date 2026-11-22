"""Quota lookups: ChatGPT plan (cached + optional live) and opencode-go."""

import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

from .dbutil import mask_key, read_env_file

OPENCODE_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
KEY_NAMES = ("OPENCODE_GO_API_KEY", "OPENCODE_GO_KEY", "OPENCODE_ZEN_API_KEY", "OPENCODE_API_KEY")


def _proxy_from_env_file(path):
    values = read_env_file(path)
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        if values.get(key):
            return values[key]
    return None


def proxy_reachable(proxy_url, timeout=0.25):
    if not proxy_url:
        return False
    try:
        m = re.match(r"^[a-z]+://([^:/]+):(\d+)", proxy_url)
        if not m:
            return False
        host, port = m.group(1), int(m.group(2))
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_get_json(url, headers, timeout=5, proxy=None):
    """GET a JSON document. Uses curl (system certs, proxy aware) then urllib."""
    args = ["curl", "-sS", "-m", str(int(timeout)), "-H", "Accept: application/json"]
    for key, value in headers.items():
        args += ["-H", "%s: %s" % (key, value)]
    if proxy:
        args += ["-x", proxy]
    args.append(url)
    try:
        out = subprocess.run(args, capture_output=True, timeout=timeout + 2)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout.decode("utf-8", "replace")), None
        err = out.stderr.decode("utf-8", "replace").strip() or "curl exit %s" % out.returncode
    except Exception as exc:
        err = "%s: %s" % (type(exc).__name__, exc)

    try:
        import urllib.request

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")), None
    except Exception as exc:
        return None, "%s; urllib: %s" % (err, exc)


def resolve_opencode_key(project_dir=None, config_path=None):
    """Key precedence: project .env -> process env -> codex config.toml(s)."""
    candidates = []
    if project_dir:
        for name in (".env", ".env.local"):
            p = Path(project_dir) / name
            if p.is_file():
                candidates.append(p)
    home_env = Path(os.path.expanduser("~/.config/codexmon/.env"))
    if home_env.is_file():
        candidates.append(home_env)

    for path in candidates:
        values = read_env_file(path)
        for name in KEY_NAMES:
            if values.get(name):
                return values[name], "env:%s" % path.name

    for name in KEY_NAMES:
        if os.environ.get(name):
            return os.environ[name], "environment"

    configs = config_path if isinstance(config_path, (list, tuple)) else [config_path]
    for candidate in configs:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            text = Path(candidate).read_text(encoding="utf-8", errors="replace")
            block = text.split("[model_providers.opencode-go]", 1)
            if len(block) == 2:
                section = block[1].split("\n[", 1)[0]
                token = None
                for line in section.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue  # commented-out token of another account
                    m = re.search(r'^experimental_bearer_token\s*=\s*"([^"]+)"', line)
                    if m and m.group(1):
                        token = m.group(1)
                if token:
                    return token, "codex-config:%s" % Path(candidate).parent.name
        except OSError:
            continue
    return None, None


def fetch_opencode_usage(key, timeout=5, proxy=None):
    data, err = http_get_json(
        OPENCODE_USAGE_URL, {"Authorization": "Bearer %s" % key}, timeout=timeout, proxy=proxy
    )
    if not data:
        return {"ok": False, "error": err or "no data", "windows": []}
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return {"ok": False, "error": "unexpected payload", "windows": []}
    windows = []
    for name in ("rolling", "weekly", "monthly"):
        item = usage.get(name)
        if not isinstance(item, dict):
            continue
        windows.append(
            {
                "name": name,
                "percent": _num(item.get("percent")),
                "status": item.get("status") or "?",
                "resets_at": _epoch(item.get("resetsAt")),
            }
        )
    return {"ok": True, "error": None, "windows": windows, "raw": data}


def fetch_chatgpt_usage(home_paths, timeout=5):
    auth = home_paths.get("auth")
    if not auth or not Path(auth).is_file():
        return {"ok": False, "error": "no auth.json"}
    try:
        data = json.loads(Path(auth).read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": "auth.json: %s" % exc}
    tokens = data.get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        return {"ok": False, "error": "no access_token"}
    headers = {
        "Authorization": "Bearer %s" % token,
        "User-Agent": "codex-cli/0.169.0",
    }
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]

    proxy = _proxy_from_env_file(Path(os.path.expanduser("~/.codex/.env")))
    if proxy and not proxy_reachable(proxy):
        return {"ok": False, "error": "proxy %s unreachable" % proxy}

    payload, err = http_get_json(CHATGPT_USAGE_URL, headers, timeout=timeout, proxy=proxy)
    if not payload:
        return {"ok": False, "error": err or "no data"}
    limits = extract_rate_limits(payload)
    if not limits:
        return {"ok": False, "error": "no rate limits in payload"}
    limits["ok"] = True
    limits["fetched_at"] = time.time()
    return limits


def extract_rate_limits(obj, depth=0):
    """Find the first dict that looks like a codex rate-limit block."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if ("primary" in obj or "secondary" in obj) and (
            "used_percent" in (obj.get("primary") or {})
            or "used_percent" in (obj.get("secondary") or {})
        ):
            return normalize_limits(obj)
        for value in obj.values():
            found = extract_rate_limits(value, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = extract_rate_limits(value, depth + 1)
            if found:
                return found
    return None


def normalize_limits(raw):
    def window(item):
        if not isinstance(item, dict):
            return None
        if item.get("used_percent") is None and item.get("percent") is None:
            return None
        return {
            "used_percent": _num(item.get("used_percent", item.get("percent"))),
            "window_minutes": item.get("window_minutes") or item.get("window"),
            "resets_at": _epoch(item.get("resets_at") or item.get("resetsAt")),
        }

    credits = raw.get("credits") if isinstance(raw.get("credits"), dict) else None
    return {
        "primary": window(raw.get("primary")),
        "secondary": window(raw.get("secondary")),
        "credits": credits,
        "plan_type": raw.get("plan_type"),
        "limit_id": raw.get("limit_id"),
        "limit_name": raw.get("limit_name"),
    }


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        s = str(value).replace("Z", "+00:00")
        import datetime

        return int(datetime.datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def key_fingerprint(key):
    return mask_key(key)
