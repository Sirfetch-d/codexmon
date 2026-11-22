**English** | [简体中文](README.zh-CN.md)

# codexmon

**Monitor codex subagents: what is running, how long it has been alive, and whether it is stuck in a livelock burning quota.**

codexmon is a read-only runtime monitor for codex on macOS. It puts the thread tree, subagent lifecycle, tool calls, and token usage on one screen, and answers one question:

> Is this subagent still doing real work, or is it stuck in a "wait → timeout → wake → wait" loop, consuming quota for nothing?

**Requirements:** macOS, Python 3.9+, no third-party packages. `/usr/bin/python3` is enough.

It is read-only: it never writes codex databases, rollout files, or config, and never kills processes.

## Why focus on subagents

codex 0.169 has an expensive failure mode: after the parent turn ends, a subagent may keep running as an orphan. It does not appear in `/subagents`, cannot be stopped from that UI, and keeps going:

```text
parent turn ends
  └─ subagent is still alive
       wait_agent times out after 10s
         → send_message wakes it up
           → wait_agent times out again
             → send_message wakes it up again …
```

Each loop burns tokens and plan quota with no output. A second, quieter shape: the subagent only polls `list_agents` / `wait_agent`, never calls `close_agent`, and never writes files or runs commands.

The most common time to open codexmon: a big task has just finished and the parent turn is over, but plan usage keeps draining; or `/subagents` is empty while the codex process is still alive. Look for `ORPHAN` / `SPIN!`.

codexmon surfaces both:

- `ORPHAN`: the subagent has no exit event, its turn is unfinished, it has been inactive for 15+ minutes, but the host process is still alive — a real orphan.
- `SPIN!`: there has been activity in the last 10 minutes, `close_agent` = 0, and either "`wait_agent` timed out ≥3 times with productive calls ≤2" or "orchestration calls ≥15 with productive calls = 0" — the spin alarm.
- `WAIT` with a stale `LAST`: it may be waiting for a subagent that will never come back.

**Check `ST` and `SPIN!` first, then tokens and quota.** That is the main workflow.

## Quick start

```bash
cd codex-tui-monitor
./codexmon                 # live TUI (2s refresh)
./codexmon --once          # print one frame and exit (works without a TTY)
./codexmon --json          # full snapshot as JSON
./codexmon --no-usage      # no network; local data only
./codexmon --interval 1 --window 24
```

To configure an opencode-go key:

```bash
cp .env.example .env
# then set OPENCODE_GO_API_KEY=oc_sk_...
```

If `.env` is absent, codexmon falls back to the `opencode-go` token in any discovered home's `config.toml` and labels the source `codex-config:<home>`; the panel works without a key.

## Reading the screen

```
AGENTS
ST     ROLE      SESSION        MODEL                CTX      TOKENS   AGE      LAST    PID    TOOLS
RUN    ● MAIN    fix-login-retry  deepseek-v4.1-flas…  12% 48k  12.3M    39m10s   now     12345  exec_command×70 apply_…
WAIT   └ SUB     add-regression  ~deepseek-v4.1-fla… 8% 32k   2.4M     1h12m    42s     12345  SPIN! wait_agent×9 send_message×12
       turn inProgress 1h12m
       parent turn finished; the subagent is still looping on wait_agent → send_message.
```

The second row is the shape you want to catch: `WAIT` + `SPIN!`, with the parent turn already finished while the child keeps waiting and waking up. If it never moves again, the state goes from `WAIT` / `IDLE` to `ORPHAN` — by then it has been spinning for a long time.

- **ST**: `RUN` actively sending/receiving / `WORK` activity within 60s / `WAIT` activity within 5 min (likely waiting for a subagent) / `IDLE` idle for up to 15 min (for a main thread, maybe the user has not replied) / `ORPHAN` no exit event, turn unfinished, inactive for 15+ min, but the host process is alive → a real orphan subagent / `EXIT` agent loop exited or process is gone.
- **ROLE**: `● MAIN` main thread, `└ SUB` a subagent spawned by a parent, indented under its parent (up to 6 levels).
- **SESSION**: human-readable name — codex `name` / `title` / first user message; for a subagent without a title, the first meaningful task message it received (e.g. "check the three PR branches"); fall back to `#last6` only if there is nothing better. The line below the selected row shows the full thread id, current turn state and duration, title, and cwd.
- **`~` before MODEL**: subagent threads do not write rollouts or record a model; the value is inferred from the home's `config.toml`, not measured.
- **CTX**: current context usage / window limit; shows `100%+` when it exceeds the reported window (codex's own window value is conservative).
- **TOKENS**: cumulative tokens for the thread (the larger of `state_5.sqlite.threads.tokens_used` and rollout totals; includes cache re-sends).
- **AGE / LAST**: time since creation / time since last activity.
- **PID**: process hosting the thread (usually `codex app-server --managed-daemon`; subagents share it).
- **TOOLS**: most-called tools and counts; a spinning thread gets a red `SPIN!` badge.

### Check these three first

1. **ROLE**: is there a chain of `└ SUB` threads under `● MAIN`, and are they still alive?
2. **ST / SPIN!**: prioritize `ORPHAN` and a red `SPIN!`; also look at a `WAIT` that has not moved for a long time.
3. **TOKENS / PLAN**: if `TOKENS` keeps rising across refreshes while `TOOLS` only shows wait / list / send and no `apply_patch` / `exec_command`, it is probably spinning; compare that with the current 5h / weekly quota windows and their reset times.

Quota panel:

```
CHATGPT  source=cache age 2m10s  credits 0  plan plus                          ○ 空闲
  5h   ██░░░░░░░░░░░░░░░░  12.0%  left  88.0%  reset 23:00 (in 2h15m)
  week ██████░░░░░░░░░░░░  34.0%  left  66.0%  reset 12-01 09:00 (in 5d02h)
OPENCODE GO  oc_sk_…abcd (codex-config:example)                                ● 调用中
  rolling  ░░░░░░░░░░░░░░░░     3%  left    97%  reset 21:00 (in 1h30m)  [ok]
  weekly   ███░░░░░░░░░░░░░    18%  left    82%  reset 12-01 00:00 (in 3d04h)  [ok]
  monthly  █░░░░░░░░░░░░░░░     9%  left    91%  reset 12-31 00:00 (in 29d12h)  [ok]
```

> The TUI currently uses Chinese for the activity badges: `使用中` = in use, `调用中` = calling, `空闲` = idle.

- `source=live` means the quota was just fetched over the network; `source=cache` is the last snapshot found in a rollout (used when the live query fails). `age` tells you how old it is.
- `● 使用中 / ● 调用中` means a thread on that provider has been active in the last 60 seconds; it is unrelated to the quota API.

## How spin detection works

"Dead loop" here does not mean 100% CPU. It is an agent-level livelock: waiting, timing out, waking up, with no output.

codex 0.169's subagent has a known problem: after the parent turn ends, the child thread may not exit. `wait_agent` times out after 10 seconds → `send_message` wakes it up → it waits again, over and over, while remaining invisible in `/subagents`.

`SPIN!` catches that pattern. It fires when there has been activity in the last 10 minutes, `close_agent` = 0, and either condition holds:

- `wait_agent` timed out (≥9s) at least 3 times, and productive calls (`apply_patch` + `exec_command` and their aliases) ≤2 → the classic "wait → timeout → poke" loop;
- orchestration calls (e.g. `wait_agent` / `list_agents` / `send_message` / `followup_task` / `interrupt_agent`) ≥15, and productive calls = 0 → pure polling with no output.

The second rule requires zero productive calls, so a thread that did a lot of `apply_patch` work and then waits once for a child is not flagged.

When it fires, the row gets a red `SPIN!` badge and the detail line reports `wait_timeout / poll / close / patch`. If `TOKENS` keeps rising across refreshes while `TOOLS` still only shows wait / list / send, it is not doing work — it is burning usage.

### After you see `SPIN!` / `ORPHAN`

codexmon detects; it does not act.

1. Check the detail line: high `wait_timeout`, high `poll`, `close=0`, `patch=0` — likely spinning, not normal orchestration.
2. Check the parent: has the parent turn finished? Is the child still `turn inProgress`?
3. If there is no output, decide whether to `close_agent` or end the host process. The same daemon may host other threads, so check the blast radius first. codexmon never kills anything.

Normal orchestration can sit in `WAIT` briefly, so do not judge by state alone. Use `TOOLS`, `LAST`, and `TOKENS` across refreshes.

## Keyboard shortcuts

| Key | Action |
|---|---|
| `q` / `Esc` | quit |
| `↑ ↓` / `j k` | select an agent |
| `PgUp` / `PgDn` | jump 8 rows |
| `g` / `G` | first / last |
| `r` | refresh quota now |
| `p` / `space` | pause auto-refresh |
| `?` | help |

## Where the data comes from (read-only)

| Source | What it provides |
|---|---|
| `~/.codex*/state_5.sqlite` → `threads` / `thread_spawn_edges` | thread metadata, tokens, parent/child links |
| `~/.codex*/thread_history_1.sqlite` → `thread_turns` | turn status / duration / failure reason |
| `~/.codex*/logs_2.sqlite` → `logs` | live activity, tool calls, spawn/exit events, context tokens |
| `~/.codex*/sessions/**/rollout-*.jsonl` | per-request usage, ChatGPT `rate_limits` |
| `ps` | pid / RSS / command line |
| `GET opencode.ai/zen/go/v1/usage` | opencode-go rolling / weekly / monthly percentages and reset times |
| `GET chatgpt.com/backend-api/wham/usage` | live ChatGPT quota (uses `HTTPS_PROXY` when configured; falls back to cache) |

Multiple homes are discovered automatically: `~/.codex` plus any `~/.codex-*` that contains `state_5.sqlite`. Homes with other names are not auto-discovered; pass them explicitly with `--homes` (comma-separated) or a colon-separated `CODEX_MONITOR_HOMES`, for example:

```bash
CODEX_MONITOR_HOMES="$HOME/.codex:/path/to/another-codex-home" ./codexmon
```

SQLite is always opened with `mode=ro`; rollouts are read incrementally. Apart from the two quota endpoints, codexmon makes no network requests and uploads nothing.

## Common cases

- **ChatGPT panel shows `source=cache`**: the live query did not succeed (proxy off, expired token, network unavailable), so it uses the last quota snapshot from a rollout. The numbers are still accurate, just not live. If `HTTPS_PROXY` is set in `~/.codex/.env`, codexmon uses it; otherwise it tries a direct connection.
- **opencode panel says `未配置 API key`**: fill in `.env`, or check that the relevant `<CODEX_HOME>/config.toml` has an uncommented `experimental_bearer_token` under `[model_providers.opencode-go]`.
- **A subagent has no PID**: one-shot processes such as `codex exec` exit and their pid disappears; the thread remains in the database. This is normal.
- **`AGE` is large but `LAST` is small**: the thread was resumed with `codex resume`; `AGE` is measured from its original creation.

## Development

```bash
python3 -m unittest discover -s tests      # 21 tests, including curses under a pty
./codexmon --once --no-usage               # fastest smoke test
```

See `AGENTS.md` for the architecture and spec.

## License

MIT. See [LICENSE](LICENSE).
