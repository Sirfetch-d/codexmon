# codexmon — Codex 运行态 TUI 监视器

本文件是本项目的**唯一事实来源（spec）**。任何改动先看这里，改实现必须同步改这里。

## 1. 目标

在 macOS 终端里提供一个常驻 TUI，实时回答四个问题：

1. 现在有哪些 codex 线程在跑？谁是主 agent、谁是 subagent、谁是谁派生出来的？
2. 每个线程的工作状态、已运行多久、tokens 用量、占用哪个系统进程（pid）？
3. ChatGPT 套餐用量：5 小时窗口 / 周窗口用了多少、剩多少、什么时候重置、现在是否在用？
4. opencode-go 套餐用量：rolling / weekly / monthly 用了多少、剩多少、什么时候重置、现在是否在调用？

**背景动机**（来自一次实际事故复盘）：codex 0.169.0 的 subagent 会在父 turn 结束后变成孤儿，`wait_agent` 10 秒超时 → `send_message` 唤醒 → 再等，形成 livelock，`/subagents` 里看不到、无法手动停、持续计费。所以本工具必须显式暴露：**孤儿线程 / 空转（SPIN）检测**。这是核心功能，不是彩蛋。

## 2. 非目标

- 不写 codex 的任何数据库 / rollout / 配置文件。
- 不调用 codex 的 app-server、不改 codex 配置、不杀进程（只读展示；杀进程这件事留给用户）。
- 不联网查 ChatGPT 网页；只做本地已落地数据的聚合 + 可选的一次 HTTP 探测。
- 不做历史报表 / 导出（后续可加，v1 不做）。

## 3. 运行形态

```
./codexmon              # 常驻 TUI，2s 刷新
./codexmon --once       # 打印一屏静态快照后退出（无 TTY / 测试用）
./codexmon --json       # 输出完整快照 JSON（脚本/调试用）
./codexmon --interval 1 # 自定义刷新间隔
./codexmon --window 24  # 回看窗口（小时），默认 12
./codexmon --no-usage   # 不联网，只读本地缓存
./codexmon --homes ~/.codex,/path/to/another-codex-home   # 指定 home（逗号分隔）
./codexmon --project-dir /path/to/dir     # 从该目录的 .env 读 opencode key
```

零第三方依赖：只用 Python 3.9+ 标准库（`curses` / `sqlite3` / `json` / `urllib`）。`/usr/bin/python3` 必须能跑。

## 4. 数据源（全部只读）

每台机器可能有多个 `CODEX_HOME`（例如一个 home 走 ChatGPT 套餐，另一个走 opencode-go）。
自动发现：`~/.codex` 加上 `~/.codex-*` 中带 `state_5.sqlite` 或 `logs_2.sqlite` 的目录；其它命名需用 `CODEX_MONITOR_HOMES`（`:` 分隔）或 `--homes`（`,` 分隔）显式指定。

| 源 | 路径 | 提供 |
|---|---|---|
| threads 元数据 | `<home>/state_5.sqlite` → `threads` | id / created_at / updated_at / model_provider / model / reasoning_effort / cwd / title / **tokens_used**（累计）/ agent_path / agent_nickname / agent_role / thread_source / sandbox_policy / rollout_path |
| 派生关系 | `<home>/state_5.sqlite` → `thread_spawn_edges` | parent_thread_id → child_thread_id → status |
| turn 生命周期 | `<home>/thread_history_1.sqlite` → `thread_turns` | status / started_at / completed_at / duration_ms / error_json（含 400 报错原文）|
| 实时活动 | `<home>/logs_2.sqlite` → `logs` | ts / target / thread_id / process_uuid / feedback_log_body |
| 会话明细 | `<home>/sessions/YYYY/MM/DD/rollout-*.jsonl` | session_meta（provider/cwd/thread_source）、token_usage_record（逐请求用量）、token_count（**ChatGPT rate_limits**）、turn_context（model/effort）|
| 进程 | `ps -Ao pid,ppid,etime,rss,pcpu,command` | pid / 存活 / RSS / CPU / 命令行 |
| opencode-go 配额 | `GET https://opencode.ai/zen/go/v1/usage` | `{usage:{rolling,weekly,monthly:{status,percent,resetsAt}}}` |

### 4.1 logs_2.sqlite 里能解析出的东西（实现依赖，勿随意改）

- 工具调用：`target=codex_core::tools::parallel`，正文尾部
  `tool call completed ... tool_name=<N> call_id=<id> ... handler_duration_ms=<ms> total_duration_ms=<ms>`
  工具名带命名空间前缀，如 `collaborationspawn_agent` / `collaborationswait_agent` → 归一化去掉前缀。
- 派生事件：`target=codex_otel.agent_communication`，
  `kind="spawn" ... sender_thread_id=<parent> receiver_thread_id=<child> content="unused"`；
  另有 `kind="result"|"followup"|"interrupt"` 的 `sender_thread_id` / `receiver_thread_id`。
- 结束事件：`target=codex_core::session::handlers`，正文含 `Agent loop exited` / `Shutting down Codex instance`。
- 上下文 tokens：`target=codex_core::session::turn`，
  `post sampling token usage turn_id=... total_usage_tokens=<n> ... full_context_window_limit=Some(<n>)`。
- 单 turn 用量（部分行）：`codex.turn.token_usage.{input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens}=<n>`。
- 进程：`process_uuid` 形如 `pid:12345:<uuid>`。

### 4.2 ChatGPT 套餐用量

rollout 的 `event_msg/token_count` 里带 `rate_limits`：

```json
{"limit_id":"codex","primary":{"used_percent":5.0,"window_minutes":300,"resets_at":1794744802},
 "secondary":{"used_percent":51.0,"window_minutes":10080,"resets_at":1794992715},
 "credits":{"has_credits":false,"unlimited":false,"balance":"0"}}
```

`primary` = 5 小时窗口，`secondary` = 周窗口。取所有 home 里**最新一条非空**记录作为缓存值。
opencode-go / deepseek 等第三方 provider 的这个字段是 null，不能当成"没用量"。

可选在线刷新：`GET https://chatgpt.com/backend-api/wham/usage`（Bearer = `~/.codex/auth.json.tokens.access_token`，需代理）。本机代理端口来自 `~/.codex/.env`。失败一律回落到缓存，并标注 `source=cache`。

### 4.3 opencode-go 配额

`GET https://opencode.ai/zen/go/v1/usage`，`Authorization: Bearer <key>`，返回：

```json
{"usage":{"rolling":{"status":"ok","percent":0,"resetsAt":"..."},
          "weekly":{"status":"ok","percent":24,"resetsAt":"..."},
          "monthly":{"status":"ok","percent":12,"resetsAt":"..."}}}
```

Key 来源优先级：`.env`（`OPENCODE_GO_API_KEY` / `OPENCODE_GO_KEY`）→ 环境变量 → `<home>/config.toml` 里 `opencode-go` 的 `experimental_bearer_token`（回落，UI 标注来源）。**Key 永不明文显示**，只显示 `oc_sk_…abcd` 形式。

## 5. 状态机（agent.state）

判定顺序，先命中先返回：

| state | 条件 |
|---|---|
| `EXIT` | 该 thread 最后一条事件就是 `Agent loop exited` / `Shutting down`（允许 5s 误差，resume 会把它顶掉）；或 rollout 里 `task_complete` 且 pid 已不在 |
| `RUN` | 存活，最后活动 ≤ 15s（正在发请求/收流） |
| `WORK` | 存活，最后活动 ≤ 60s |
| `WAIT` | 存活，最后活动 ≤ 300s（大概率在等 subagent） |
| `IDLE` | 存活，最后活动 ≤ 900s |
| `ORPHAN` | **只对 subagent**：没有退出事件、turn 未完成、无活动 > 900s，且宿主进程（pid）还活着 → 真正的孤儿，需要人工处理。主线程长时间空闲只算 `IDLE`（可能只是人还没回）；宿主进程已死的 sub 记 `EXIT` |

另有 `EXIT` 的第二条来源：rollout 里出现 `task_complete` 且进程已不在、空闲 > 60s 时也判 `EXIT`（`codex exec` 这类一次性会话）。
rollout 出现新活动则视为线程被 resume，退出标记自动失效。

`SPIN`（空转告警）为独立布尔标记，可与上面任一状态共存。最近 10 分钟内有活动、
`close_agent` = 0，且满足下面任一条即命中：
1. `wait_agent` 超时（`handler_duration_ms ≥ 9000`）≥ 3 次，且产出类调用（`apply_patch` + `exec_command`）≤ 2 次；
2. 编排类调用（`wait_agent/list_agents/send_message/followup_task/interrupt_agent`）≥ 15 次，
   同时产出类调用 = 0（纯轮询，不产出）。

第 1 条要求产出很少，是为了不冤枉「干了很多活、最后等一次子代理」的正常编排线程。

两种形态在 0.169 上都实测出现过：前者是「等子代理 → 超时 → 催」的循环，
后者是只 `list_agents` 轮询、一次 `close_agent` 都不调的浪费。命中即在行尾打红色 `SPIN`。

## 5.1 会话名与模型归属（可读性要求）

- 用户看的是**会话名**，不是 hex id。UI 主列必须是 `SESSION`（人可读），id 只作辅助信息。
- 子代理线程通常没有 `title`，此时用它在 `agent_communication` 里收到的第一条**有意义**的
  正文（过滤 `noop`/`Stop…`/`The NEW_TASK…` 这类噪声）作为名字。
- 子代理不落 rollout、也不记录 model，展示时按 **home 的 config.toml 顶层 `model`** 推断，
  并用 `~` 前缀标明是推断值（`~deepseek-v4.1-fla…`），不得当成实测值。

## 6. UI 规范

单屏垂直分四块，随终端高度自适应，最小 80x24 不崩：

1. **标题栏**：`codexmon` / 本地时间 / 刷新间隔 / 活跃线程数 / 主+子数量 / 暂停标记。
2. **AGENTS**：表格，列 = `ST | ROLE | THREAD | MODEL | CTX% | TOKENS | AGE | LAST | PID | TOOLS`。
   `ROLE` 用 `● MAIN` / `└ SUB`（按父子树缩进，子线程紧跟父线程），`THREAD` 显示 id 前 13 位
（uuidv7 前 8 位是毫秒时间戳，同一分钟内会撞，13 位才可区分）。
   行内颜色：RUN 绿、WORK 青、WAIT 黄、IDLE 蓝、ORPHAN/SPIN 红、EXIT 灰。
3. **PLAN**：左 ChatGPT（5h / 周条、剩余、重置倒计时、credits、`使用中/空闲`），右 OPENCODE GO（rolling / weekly / monthly 三条、重置倒计时、key 来源、`调用中/空闲`）。
4. **EVENTS**：最近工具调用流（时间 / 线程短 id / 工具名 / 耗时 / 失败）。
5. **页脚**：`q 退出  r 立即刷新  p 暂停  ↑↓ 滚动  ? 帮助`，右侧显示 daemon / TUI 的 pid。

列宽自适应：`LAST` 在 ≥82 列出现，`PID` 在 ≥98 列出现，`TOOLS` 在剩余 ≥12 列时出现；窄屏优先丢列而不是截断关键列。

滚动：`↑↓`/`jk` 移动 agent 列表，`g/G` 首尾，`PgUp/PgDn` 翻 event。

## 7. 性能与稳健

- 全量 refresh 目标 < 200ms（本机 30k 行 logs 实测 ~60ms/查询）。
- rollout 增量解析：记录 `(size, offset, 累加器)`，只读新增字节；只扫 mtime 48h 内、最多 40 个文件。
- 所有 sqlite 以 `mode=ro` 打开，`timeout=0.3`，任何异常都要降级为空数据而不是崩溃。
- 单个 home/DB/文件坏掉 → 该源标 `err`，其余照常显示。
- 网络请求 4s 超时，失败静默回落。TUI 主循环绝不因 I/O 阻塞超过 0.5s：网络请求放后台线程。
- 不使用任何第三方库。

## 8. 目录结构

```
AGENTS.md               # 本文件
README.md               # English README (default)
README.zh-CN.md         # 中文说明
LICENSE                 # MIT
.env.example            # OPENCODE_GO_API_KEY 占位
codexmon                # 可执行入口（python3 shebang）
codexmon_lib/
  __init__.py
  paths.py              # CODEX_HOME 发现
  dbutil.py             # 只读 sqlite 封装
  state_db.py           # threads / spawn_edges / turns
  logsdb.py             # 实时活动 / 工具 / spawn / 退出 / ctx
  rollout.py            # rollout 增量解析 + rate_limits
  usage.py              # chatgpt 缓存+在线、opencode-go HTTP
  procs.py              # ps 解析
  collect.py            # 合并成 Snapshot
  render.py             # --once / --json 的纯文本渲染
  tui.py                # curses 界面
tests/
  test_smoke.py         # 无 TTY 冒烟：快照结构 + 关键不变量
  test_tui_pty.py       # pty 里真跑 curses：启动 / 绘制 / q 退出 / 小屏不崩
```

`README.md` 是默认英文版，`README.zh-CN.md` 是中文版；两者内容保持同步。

## 9. 验收标准（Definition of Done）

1. `./codexmon --once` 在无 TTY 环境打印完整快照，退出码 0。
2. `./codexmon --json` 输出可解析 JSON，至少含 `agents / chatgpt / opencode / processes / events / totals / providers` 顶层键。
3. 主 agent 与 subagent 能区分：subagent 有 `parent`，且出现在父线程下面（缩进 + depth）。
4. 每个 agent 有：状态、运行时长、tokens（累计）、进程 pid、最近工具调用次数。
5. ChatGPT 面板能显示 5h / 周窗口的已用百分比 + 重置倒计时；在线查不到时代理不可用就回落 rollout 缓存并标 `source=cache`；**绝不显示错误数字**。
6. opencode-go 面板：`--no-usage` 下不联网；有 key 时能显示三条窗口的百分比与重置时间；无 key 时明确提示"未配置 key"。
7. SPIN 检测：对 wait_agent 10s 超时循环能命中。
8. curses TUI 在 pty 里能启动、响应 `q` 退出、resize 不崩。
9. `python3 -m unittest discover tests` 全绿。
10. 只读：运行前后 `~/.codex*` 下任何文件 mtime 不变（除 codex 自己写的）。

## 10. 已知坑

- `~/.codex/auth.json` 的 access_token 会过期；在线用量查询失败是常态，缓存才是主路径。
- 子代理线程**不落 rollout 文件**、也常常没有 `state_5.sqlite` 行（实际事故已确认），所以它们的 tokens 只能从 `threads.tokens_used` + logs 里的 `codex.turn.token_usage.*` 拿；两者都没有时**必须显示 `—`（未知），绝不能显示 0**——0 会被误读成「没花钱」。
- `logs_2.sqlite` 会轮转（本机 ~30k 行），跨小时的空转只能靠当前窗口内的事件判断。
- `threads.tokens_used` 是累计值（含 cache 重发），和 ChatGPT 套餐的 "used_percent" 不是一回事，不要混在同一栏比较。
- 时间戳：`threads.created_at` / `thread_turns.started_at` 是 **epoch 秒**，`logs.ts` 也是秒，rollout 是 ISO8601 UTC。
- 终端宽度 < 80 时截断而不是换行，避免 curses 花屏。
