[English](README.md) | **简体中文**

# codexmon

**盯住 codex 的 subagent：谁在跑、跑了多久、是不是卡在死循环里空转烧额度。**

codexmon 是 macOS 终端里的只读运行态监视器。它把 codex 的线程树、subagent 生命周期、工具调用和 token 用量放在一屏，重点回答一个问题：

> 这条 subagent 到底还在干活，还是已经陷入「等 → 超时 → 唤醒 → 再等」的死循环，正在无意义地消耗用量？

纯 Python 标准库，零依赖，**只读**——不写 codex 的任何库、不动配置、不杀进程。

**环境要求：** macOS、Python 3.9+，无第三方依赖；`/usr/bin/python3` 即可运行。

## 为什么专门盯 subagent

codex 0.169 的 subagent 有一个很贵的失败模式：父 turn 结束后，子线程可能不退出，变成孤儿。它不在 `/subagents` 里显示，也没法从那个界面手动停，但会继续跑：

```text
父 turn 结束
  └─ subagent 还活着
       wait_agent 10s 超时
         → send_message 唤醒
           → wait_agent 又超时
             → send_message 再唤醒 …
```

每循环一次都在消耗 token / 套餐额度，却没有任何产出。另一种形态更隐蔽：subagent 只反复 `list_agents` / `wait_agent` 轮询，一次 `close_agent` 都不调，也不写文件、不执行命令。

最典型的使用时机：一个大任务跑完、父 turn 已经结束，但套餐用量还在掉；或者 `/subagents` 里看不到子代理，进程却还活着。这时打开 codexmon，先看有没有 `ORPHAN` / `SPIN!`。

codexmon 把这两种情况显式标出来：

- `ORPHAN`：subagent 没有退出事件、turn 未完成、超过 15 分钟无活动，但宿主进程还活着——真正的孤儿。
- `SPIN!`：最近 10 分钟内有活动，`close_agent` = 0，且满足「`wait_agent` 超时 ≥3 次且产出 ≤2 次」或「编排类调用 ≥15 次且产出 = 0」——空转告警。
- `WAIT` + `LAST` 长时间不动：可能正在等一个永远不会回来的子代理。

**先看 `ST` 和 `SPIN!`，再看 token 和额度**——这就是这个工具的主要用法。

## 快速开始

```bash
cd codex-tui-monitor
./codexmon                 # 常驻 TUI（2s 刷新）
./codexmon --once          # 打一屏就退出（无 TTY 也能跑）
./codexmon --json          # 完整快照 JSON
./codexmon --no-usage      # 不联网，只看本地
./codexmon --interval 1 --window 24
```

首次想填 opencode-go 的 key：

```bash
cp .env.example .env
# 编辑 .env：OPENCODE_GO_API_KEY=oc_sk_...
```

没有 `.env` 时会自动回落到任意已发现 home 的 `config.toml` 里 `opencode-go` 的 token（面板上会标注 `codex-config:<home>`），所以不填也能看到额度。

## 屏幕怎么读

```
AGENTS
ST     ROLE      SESSION        MODEL                CTX      TOKENS   AGE      LAST    PID    TOOLS
RUN    ● MAIN    修复登录重试    deepseek-v4.1-flas…  12% 48k  12.3M    39m10s   now     12345  exec_command×70 apply_…
WAIT   └ SUB     补回归测试      ~deepseek-v4.1-fla… 8% 32k   2.4M     1h12m    42s     12345  SPIN! wait_agent×9 send_message×12
       turn inProgress 1h12m
       父 turn 已结束，子线程还在 wait_agent → send_message 循环。
```

第二行就是最该处理的形态：`WAIT` + `SPIN!`，父线程已经结束，子线程还在反复等待和唤醒。如果它一直不动，状态会从 `WAIT` / `IDLE` 一路走到 `ORPHAN`——那时候已经空转很久了。

- **ST**：`RUN` 正在发请求 / `WORK` 60s 内有动静 / `WAIT` 5 分钟内有动静（多半在等子代理）/ `IDLE` 主线程闲着（可能只是人还没回）/ `ORPHAN` 没有退出事件、turn 未结束、超过 15 分钟无活动但宿主进程还活着 → 真正的孤儿 subagent / `EXIT` agent loop 已退出或进程已消失。
- **ROLE**：`● MAIN` 主线程，`└ SUB` 由父线程 spawn 出来的子代理，按父子关系缩进排在父线程下面（最多 6 层）。
- **SESSION**：人可读的会话名——优先用 codex 自己的 `name`/`title`/首条用户消息；子代理没有标题时用**它收到的第一条任务消息**（比如「核查三个PR当前分支」），实在没有才退回 `#id末6位`。选中行下面一行给出完整 thread id、当前 turn 状态与耗时、标题、cwd。
- **MODEL 前面的 `~`**：子代理线程不落 rollout、不记模型，显示的是该 home `config.toml` 里的默认模型，属于推断值。
- **CTX**：当前上下文占用 / 窗口上限；超过报告窗口时显示 `100%+`（codex 自己上报的窗口值偏保守）。
- **TOKENS**：该线程累计 token（取 `state_5.sqlite.threads.tokens_used` 与 rollout 累计中的较大值；含 cache 重发）。
- **AGE / LAST**：线程创建至今 / 距最后一次活动的时长。
- **PID**：承载该线程的进程（一般是 `codex app-server --managed-daemon`，子代理都跑在同一个 daemon 里）。
- **TOOLS**：调用最多的工具和次数；命中空转会在行尾标红 `SPIN!`。

### 先看这三眼

1. **ROLE**：`● MAIN` 下面有没有挂着一串 `└ SUB`，它们是不是还活着。
2. **ST / SPIN!**：`ORPHAN`、红色 `SPIN!` 优先看；`WAIT` 很久且 `LAST` 不动也要看。
3. **TOKENS / PLAN**：连续几次刷新 `TOKENS` 在涨，但 `TOOLS` 里只有 wait / list / send，没有 `apply_patch` / `exec_command`——基本就是空转；再对照额度面板当前 5h / 周窗口的消耗和重置时间。

额度面板：

```
CHATGPT  source=cache age 2m10s  credits 0  plan plus                          ○ 空闲
  5h   ██░░░░░░░░░░░░░░░░  12.0%  left  88.0%  reset 23:00 (in 2h15m)
  week ██████░░░░░░░░░░░░  34.0%  left  66.0%  reset 12-01 09:00 (in 5d02h)
OPENCODE GO  oc_sk_…abcd (codex-config:example)                                ● 调用中
  rolling  ░░░░░░░░░░░░░░░░     3%  left    97%  reset 21:00 (in 1h30m)  [ok]
  weekly   ███░░░░░░░░░░░░░    18%  left    82%  reset 12-01 00:00 (in 3d04h)  [ok]
  monthly  █░░░░░░░░░░░░░░░     9%  left    91%  reset 12-31 00:00 (in 29d12h)  [ok]
```

- `source=live` 是刚联网查到的，`source=cache` 是最近一条 rollout 里落下的记录（代理不通时就是它，`age` 会告诉你是多久前的）。
- `● 使用中 / ● 调用中` 表示最近 60 秒内这个 provider 上真的有线程在跑，和额度接口无关。

## 死循环 / 空转是怎么判定的

这里说的“死循环”不是 CPU 打满，而是 agent 层面的 livelock：一直在等、一直在超时、一直在唤醒，但没有任何产出。

codex 0.169 的 subagent 有个已知毛病：父 turn 结束后子线程不退出，`wait_agent` 10 秒超时 → `send_message` 把它戳醒 → 再等，循环往复，`/subagents` 里还看不到。

`SPIN!` 就是把这个模式抓出来。最近 10 分钟内有活动、`close_agent` = 0，并且满足任一条件就算命中：

- `wait_agent` 超时（≥9s）≥ 3 次，且 `apply_patch` + `exec_command` ≤ 2 次 → 典型的「等 → 超时 → 催」循环；
- 编排类调用（`wait_agent` / `list_agents` / `send_message` / `followup_task` / `interrupt_agent`）≥ 15 次，同时 `apply_patch` + `exec_command` = 0 → 纯轮询不产出。

第二条特意要求「产出 = 0」，所以「干了几十次 apply_patch、最后等一次子代理」的正常线程不会被误报。

命中后行尾标红 `SPIN!`，详情行给出 `wait_timeout / poll / close / patch` 四个计数。连续刷新时如果 `TOKENS` 还在涨、`TOOLS` 仍只有 wait / list / send，就说明它不是在干活，而是在消耗用量。

### 发现 `SPIN!` / `ORPHAN` 之后

codexmon 只负责发现，不负责处置：

1. 先看详情行：`wait_timeout` 高、`poll` 高、`close=0`、`patch=0`，基本可以确认是空转，不是正常编排。
2. 再看父线程：父 turn 是否已经结束、子线程是否还停在 `turn inProgress`。
3. 确认没有产出后，由你决定 `close_agent` 或结束对应宿主进程；注意同一 daemon 可能还承载别的线程，动手前先确认影响范围。codexmon 不会自动杀。

正常编排也可能短时间停在 `WAIT`，所以不要只看状态，结合 `TOOLS`、`LAST` 和连续几次的 `TOKENS` 变化判断。

## 快捷键

| 键 | 作用 |
|---|---|
| `q` / `Esc` | 退出 |
| `↑ ↓` / `j k` | 选 agent |
| `PgUp` / `PgDn` | 跳 8 行 |
| `g` / `G` | 首 / 尾 |
| `r` | 立刻重查额度 |
| `p` / `space` | 暂停自动刷新 |
| `?` | 帮助 |

## 数据从哪来（全只读）

| 来源 | 用途 |
|---|---|
| `~/.codex*/state_5.sqlite` → `threads` / `thread_spawn_edges` | 线程元数据、tokens、父子关系 |
| `~/.codex*/thread_history_1.sqlite` → `thread_turns` | turn 状态 / 耗时 / 失败原因 |
| `~/.codex*/logs_2.sqlite` → `logs` | 实时活动、工具调用、spawn 事件、退出事件、上下文 token |
| `~/.codex*/sessions/**/rollout-*.jsonl` | 逐请求用量、ChatGPT `rate_limits` |
| `ps` | pid / RSS / 命令行 |
| `GET opencode.ai/zen/go/v1/usage` | opencode-go 三个窗口的百分比与重置时间 |
| `GET chatgpt.com/backend-api/wham/usage` | ChatGPT 实时额度（配置了 `HTTPS_PROXY` 就走代理；失败回落缓存） |

多 home 自动发现：`~/.codex` 以及带 `state_5.sqlite` 的 `~/.codex-*`。其他命名的 home 不会被自动发现，需要用 `--homes`（逗号分隔）或 `CODEX_MONITOR_HOMES`（冒号分隔）显式传入，例如：

```bash
CODEX_MONITOR_HOMES="$HOME/.codex:/path/to/another-codex-home" ./codexmon
```

sqlite 一律 `mode=ro` 打开，rollout 只读增量；除两个额度接口外不产生任何网络请求，不上传任何内容。

## 常见情况

- **ChatGPT 面板显示 `source=cache`**：在线查询没成功（代理没开、token 过期、网络不可达等），用的是最后一次 rollout 里的额度快照。数字仍然准，只是不是实时。`~/.codex/.env` 里配了 `HTTPS_PROXY` 会走代理，否则尝试直连。
- **opencode 面板 `未配置 API key`**：填 `.env`，或确认对应的 `<CODEX_HOME>/config.toml` 里 `[model_providers.opencode-go]` 有未注释的 `experimental_bearer_token`。
- **子代理没有 PID**：`codex exec` 这类一次性进程结束后 pid 就没了，线程还在库里，这是正常的。
- **`AGE` 很大但 `LAST` 很小**：线程被 `codex resume` 接着用了，`AGE` 是从最初创建算的。

## 开发

```bash
python3 -m unittest discover -s tests      # 21 个测试，含 pty 里真跑 curses 与四种退出路径
./codexmon --once --no-usage               # 最快的冒烟
```

结构见 `AGENTS.md`（也是本项目的 spec）。

## License

MIT，见 [LICENSE](LICENSE)。
