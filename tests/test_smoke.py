"""Unit + integration smoke tests. Run: python3 -m unittest discover tests"""

import json
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from codexmon_lib import collect, dbutil, logsdb, paths, render, rollout, state_db, usage  # noqa: E402


class FormatTests(unittest.TestCase):
    def test_clip_counts_wide_chars(self):
        self.assertEqual(render.disp_w("中文abc"), 7)
        clipped = render.clip("中文中文中文", 7)
        self.assertLessEqual(render.disp_w(clipped), 7)
        self.assertTrue(clipped.endswith("…"))

    def test_pad_align(self):
        self.assertEqual(render.pad("ab", 5), "ab   ")
        self.assertEqual(render.pad("ab", 5, "right"), "   ab")

    def test_fmt_helpers(self):
        self.assertEqual(render.fmt_tokens(2_000_000), "2.0M")
        self.assertEqual(render.fmt_tokens(341_000), "341k")
        self.assertEqual(render.fmt_dur(0), "0s")
        self.assertEqual(render.fmt_dur(90), "1m30s")
        self.assertEqual(render.fmt_dur(3700), "1h01m")
        self.assertEqual(render.fmt_ago(time.time() - 45), "45s")
        # LAST keeps seconds at every scale
        self.assertEqual(render.fmt_ago(time.time() - 192), "3m12s")
        self.assertEqual(render.fmt_ago(time.time() - 3700), "1h01m")
        # AGE stays coarse once past a minute
        self.assertEqual(render.fmt_age(45), "45s")
        self.assertEqual(render.fmt_age(192), "3m")
        self.assertEqual(render.fmt_age(3700), "1h01m")
        self.assertEqual(render.fmt_age(340920), "3d22h")
        self.assertEqual(render.bar(50, 10), "█████░░░░░")

    def test_ctx_label_marks_overflow(self):
        agent = {"ctx_tokens": 271000, "ctx_limit": 258400, "ctx_percent": 104.9, "ctx_over": True}
        self.assertTrue(render.ctx_label(agent).startswith("100%+"))

    def test_session_label_prefers_human_name(self):
        self.assertEqual(render.session_label({"label": "核查三个PR当前分支", "tail": "#abc123"}), "核查三个PR当前分支")
        self.assertEqual(render.session_label({"tail": "#abc123", "id": "01a1de3b-63d1-7373-8499-3cfd30572dbf"}), "#abc123")
        self.assertEqual(render.session_label({"id": "01a1de3b-63d1-7373-8499-3cfd30572dbf"}), "#572dbf")

    def test_role_label_depth(self):
        self.assertEqual(render.role_label({"role": "main"}), "● MAIN")
        self.assertEqual(render.role_label({"role": "sub", "depth": 0}), "· SUB")
        self.assertEqual(render.role_label({"role": "sub", "depth": 2}).strip(), "└ SUB")


class ParserTests(unittest.TestCase):
    def test_pid_from_process_uuid(self):
        self.assertEqual(logsdb.parse_pid("pid:12345:c95470e9"), 12345)
        self.assertIsNone(logsdb.parse_pid("nope"))

    def test_tool_regex(self):
        body = (
            'session_loop{thread_id=x}: turn{otel.name="session_task.turn"}: '
            'tool call completed event.name="codex.tool_call" trace_id= '
            "conversation.id=01a1 turn_id=01a1 tool_name=exec_command "
            'call_id=call_1 tool_source="direct" execution_started=true '
            "dispatch_duration_ms=0 handler_duration_ms=30338 total_duration_ms=30338"
        )
        m = logsdb.TOOL_RE.search(body)
        self.assertEqual(m.group(1), "exec_command")
        self.assertEqual(m.group(2), "30338")

    def test_spawn_regex(self):
        body = (
            'agent communication event.name="codex.agent_communication" '
            'communication_id=01a1 kind="spawn" state="send" '
            "sender_thread_id=01parent receiver_thread_id=01child content=\"unused\""
        )
        m = logsdb.SPAWN_RE.search(body)
        self.assertEqual(m.group("kind"), "spawn")
        self.assertEqual(m.group("sender"), "01parent")
        self.assertEqual(m.group("receiver"), "01child")

    def test_ctx_regex(self):
        body = (
            "post sampling token usage turn_id=x total_usage_tokens=341947 "
            "auto_compact_scope_tokens=341947 auto_compact_scope_limit=Some(943718) "
            "full_context_window_limit=Some(996147) token_limit_reached=false"
        )
        m = logsdb.CTX_RE.search(body)
        self.assertEqual(m.group(1), "341947")
        self.assertEqual(m.group(2), "996147")

    def test_iso_to_epoch(self):
        self.assertIsNotNone(rollout._iso_to_epoch("2026-11-15T06:56:31.462Z"))
        self.assertIsNone(rollout._iso_to_epoch("garbage"))

    def test_turn_duration_live(self):
        turn = {"status": "inProgress", "started_at": 1000}
        self.assertGreater(state_db.turn_duration_ms(turn, now=1060), 50_000)
        self.assertEqual(state_db.turn_duration_ms({"duration_ms": 4200}), 4200)

    def test_rate_limit_normalisation(self):
        raw = {
            "limit_id": "codex",
            "primary": {"used_percent": 5.0, "window_minutes": 300, "resets_at": 1794744802},
            "secondary": {"used_percent": 51.0, "window_minutes": 10080, "resets_at": 1794992715},
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        }
        norm = usage.normalize_limits(raw)
        self.assertEqual(norm["primary"]["used_percent"], 5.0)
        self.assertEqual(norm["secondary"]["resets_at"], 1794992715)
        found = usage.extract_rate_limits({"nested": {"rate_limits": raw}})
        self.assertEqual(found["primary"]["used_percent"], 5.0)


TID_PARENT = "01aaaaaa-parent-000000000000000000"
TID_CHILD = "01bbbbbb-child--000000000000000000"
TID_EXIT = "01cccccc-exit---000000000000000000"


def _make_home(tmp):
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    logs = tmp / "logs_2.sqlite"
    con = sqlite3.connect(str(logs))
    con.execute(
        "CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "ts_nanos INTEGER NOT NULL, level TEXT NOT NULL, target TEXT NOT NULL, "
        "feedback_log_body TEXT, module_path TEXT, file TEXT, line INTEGER, "
        "thread_id TEXT, process_uuid TEXT, estimated_bytes INTEGER NOT NULL DEFAULT 0)"
    )
    now = int(time.time())
    rows = []
    parent, child, exited = TID_PARENT, TID_CHILD, TID_EXIT
    puid = "pid:12345:uuid"

    def tool(tid, name, ms):
        body = (
            "session_loop{thread_id=%s}: turn: tool call completed "
            'event.name="codex.tool_call" tool_name=%s call_id=c1 '
            "dispatch_duration_ms=0 handler_duration_ms=%d total_duration_ms=%d" % (tid, name, ms, ms)
        )
        rows.append((now - 5, "codex_core::tools::parallel", tid, puid, body))

    rows.append(
        (
            now - 30,
            "codex_otel.agent_communication",
            parent,
            puid,
            'agent communication kind="spawn" state="send" sender_thread_id=%s '
            'receiver_thread_id=%s content="unused"' % (parent, child),
        )
    )
    for _ in range(4):
        tool(parent, "collaborationswait_agent", 10001)
    tool(parent, "collaborationssend_message", 3)
    tool(child, "exec_command", 1200)
    rows.append(
        (
            now - 40,
            "codex_otel.agent_communication",
            child,
            puid,
            'agent communication kind="message" state="send" sender_thread_id=%s '
            'receiver_thread_id=%s content="核查三个PR当前分支"' % (child, child),
        )
    )
    rows.append(
        (
            now - 41,
            "codex_otel.agent_communication",
            child,
            puid,
            'agent communication kind="message" state="send" sender_thread_id=%s '
            'receiver_thread_id=%s content="noop"' % (child, child),
        )
    )
    rows.append((now - 2, "codex_core::session::turn", parent, puid,
                 "post sampling token usage turn_id=t total_usage_tokens=42000 "
                 "full_context_window_limit=Some(200000)"))
    rows.append((now - 3, "codex_core::session::handlers", exited, puid, "Agent loop exited"))
    for ts, target, tid, puid_v, body in rows:
        con.execute(
            "INSERT INTO logs (ts,ts_nanos,level,target,feedback_log_body,thread_id,process_uuid,estimated_bytes)"
            " VALUES (?,0,'INFO',?,?,?,?,0)",
            (ts, target, body, tid, puid_v),
        )
    con.commit()
    con.close()
    return {
        "home": tmp,
        "state_db": tmp / "state_5.sqlite",
        "history_db": tmp / "thread_history_1.sqlite",
        "logs_db": logs,
        "sessions": tmp / "sessions",
        "config": tmp / "config.toml",
        "auth": tmp / "auth.json",
    }


class ActivityIntegrationTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.mkdtemp(prefix="codexmon-test-")
        self.home = _make_home(self.tmp)

    def test_load_activity_counts(self):
        data = logsdb.load_activity(self.home, window_hours=1)
        parent = data["threads"][TID_PARENT]
        self.assertEqual(parent["wait_timeouts"], 4)
        self.assertEqual(parent["close_calls"], 0)
        self.assertEqual(parent["ctx_tokens"], 42000)
        self.assertEqual(parent["ctx_limit"], 200000)
        self.assertIn(TID_CHILD, data["edges"])

    def test_snapshot_flags_spin_and_parent(self):
        scanner = rollout.RolloutScanner()
        snap = collect.build_snapshot([self.home], scanner, 1, time.time())
        agents = {a["id"]: a for a in snap["agents"]}
        parent = agents[TID_PARENT]
        child = agents[TID_CHILD]
        exited = agents[TID_EXIT]
        self.assertTrue(parent["spin"], "wait_agent timeouts with no close_agent must flag SPIN")
        self.assertEqual(child["label"], "核查三个PR当前分支")
        self.assertEqual(child["parent"], parent["id"])
        self.assertEqual(child["role"], "sub")
        self.assertEqual(exited["state"], "EXIT")

    def test_snapshot_shape(self):
        snap = collect.build_snapshot([self.home], rollout.RolloutScanner(), 1, time.time())
        for key in ("ts", "homes", "agents", "processes", "events", "sources", "providers"):
            self.assertIn(key, snap)
        json.dumps(snap, default=str)  # must be serialisable
        text = render.plain(snap, width=100)
        self.assertIn("AGENTS", text)
        self.assertIn("CHATGPT", text)
        self.assertIn("OPENCODE GO", text)


class RealHomeTests(unittest.TestCase):
    """Runs against this machine's real codex homes when present."""

    def test_real_collect_does_not_crash(self):
        homes = paths.discover_homes()
        self.assertTrue(homes, "no ~/.codex* home found")
        snap = collect.build_snapshot(
            [paths.home_paths(h) for h in homes], rollout.RolloutScanner(), 12, time.time()
        )
        self.assertIsInstance(snap["agents"], list)
        for agent in snap["agents"]:
            for field in (
                "id", "short", "role", "state", "last_seen", "tokens_total", "ctx_tokens",
                "pid", "tool_total", "spin",
            ):
                self.assertIn(field, agent)
            self.assertIn(agent["state"], ("RUN", "WORK", "WAIT", "IDLE", "ORPHAN", "EXIT"))
            self.assertLessEqual(len(agent["short"]), 13)

    def test_env_file_roundtrip(self):
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp(prefix="codexmon-env-")
        try:
            env_path = Path(tmp) / ".env"
            env_path.write_text("# comment\nOPENCODE_GO_API_KEY=oc_sk_test\n")
            values = dbutil.read_env_file(env_path)
            self.assertEqual(values["OPENCODE_GO_API_KEY"], "oc_sk_test")
            key, source = usage.resolve_opencode_key(project_dir=tmp, config_path=[])
            self.assertEqual(key, "oc_sk_test")
            self.assertTrue(source.startswith("env:"))
            self.assertEqual(dbutil.mask_key("oc_sk_1234567890"), "oc_sk_…7890")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
