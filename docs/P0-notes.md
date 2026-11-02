# P0 笔记

结论先说：

- 仓库是 Python 3.12+ 的 MCP→ACP 网关（`dot-gateway` 0.2.0）。对外是无状态 Streamable HTTP，默认 `127.0.0.1:8765/mcp`；对内每个活会话一个 stdio ACP 子进程。本阶段没有改业务代码。
- 现有 MCP 工具 **12** 个，名字已经对齐 [kxress/acp-mcp](https://github.com/kxress/acp-mcp) 的会话子集。对照的是该仓库 **0.2.6**（`master` @ `0b49e3451cdd4caded242a0458857a5cbe0044d7`，2026-11-01，README 写明 16 个工具）。建议 **P1 不改名、不加兼容别名**；缺的 `run_task` / `list_models` / `set_mode` 不是 P1 阻塞项，工作区清理类工具不要移植。
- 本环境 `uv run pytest -q`：**45 passed / 45**，66.32 秒。没跑 `scripts/smoke_dsh.py` 和 `scripts/test_auto_review.py`（要真实 `dsh`，会花模型调用）。没在 macOS 上跑，也没跑 cloudflared。
- P1 最大的三个风险：子进程默认继承全部环境变量；`cwd` 只有 `realpath`、没有根白名单，`/tmp` 与 `/private/tmp` 不能按字面前缀比；Host 校验已经由 MCP SDK 在 `127.0.0.1` 上默认打开，和 cloudflared 的 `httpHostHeader` 对不上就会 421，而对得太宽会拆掉 DNS rebinding 防护。

## 1. 仓库现在长什么样

| 路径 | 职责 |
|---|---|
| `src/dot_gateway/server.py` | 12 个 MCP 工具、`INSTRUCTIONS`、camelCase 紧凑 JSON、`make_app`、CLI |
| `src/dot_gateway/sessions.py` | 会话生命周期、事件视图、权限、重启恢复、子进程编排 |
| `src/dot_gateway/acp.py` | 换行分隔 JSON-RPC stdio 客户端 |
| `src/dot_gateway/agents.py` | 内置 `dsh` 和 JSON/`DOT_GATEWAY_AGENTS_JSON` 注册表 |
| `src/dot_gateway/config.py` | 环境变量、CLI、`<state_dir>/config.json` 的 `defaults` |
| `src/dot_gateway/events.py` | `events.jsonl`、截断、原子写 |
| `src/dot_gateway/gitinfo.py` | 每轮开始/结束的 git 摘要 |
| `tests/fake_acp_agent.py` | 假 ACP agent |
| `docs/local.dot-gateway.plist.example` | LaunchAgent 示例 |
| `scripts/start.sh` | `uv run --project … --frozen --no-dev dot-gateway` |

依赖：`pyproject.toml` 要求 `mcp>=2.3,<3`。这次锁到并装上的是 **mcp 2.3.0**。配置文件注释写明：这些配置项都不是安全限制（`config.py` 模块说明）。

状态目录默认 `$HOME/Library/Application Support/dot-gateway/`（可用 `DOT_GATEWAY_STATE_DIR` / `--state-dir` 改）。每个会话一个目录：

```text
<state_dir>/sessions/<sessionId>/
  session.json          # 元数据、每一轮、投递游标、工具摘要；先写 tmp、fsync、再 rename
  events.jsonl          # 只追加事件，sequence 从 1 起
  tool-calls/<id>.json  # 单次工具完整记录，每个字符串最多 1 MiB
  agent-stderr.log
```

网关 `session_id` 今天等于 agent 返回的 `sessionId`，另外存 `acp_session_id`。调 ACP 时用 `_acp_id()`，也就是后者。agent 自己的对话历史不在这个目录里（dsh 在它自己的 `~/.dsh/sessions/`，本仓库不读那份文件）。

## 2. MCP 工具（12）

返回值是一段不缩进的 JSON 文本，字段 camelCase。`DOT_GATEWAY_STRUCTURED_OUTPUT=1` 时同一对象再放进 `structuredContent`。错误变成 MCP `ToolError`，正文是 `CODE: message`。

| 工具 | 参数（默认） | 落到 |
|---|---|---|
| `list_agents` | 无 | `SessionManager.list_agents` |
| `start_session` | `cwd`；`agentId="dsh"`；`prompt=None`；`permissionMode=None`；`permissionPolicy=None`；`model=None`；`config=None` | `start_session` |
| `prompt` | `sessionId`，`text`；`permissionMode=None`；`permissionPolicy=None`；`waitMs=0`（实现里封顶 60000） | `prompt` |
| `get_events` | `sessionId`；`afterSequence=0`；`view="final"`；`waitMs=0`（封顶 30000）；`includeText=false`；`includeToolOutput=false`；`limit=100`（1..500）；`maxBytes=200000`（封顶 1 MiB）；`includeThoughts=false`；`replyBytes=65536` | `get_events` |
| `get_tool_call` | `sessionId`，`toolCallId`；`offset=0`；`maxBytes=32768`（封顶 256 KiB） | `get_tool_call` |
| `list_sessions` | `cwd=None`；`state=None`；`limit=20`（封顶 200）；`source="gateway"`；`agentId="dsh"`；`cursor=None` | `list_sessions` |
| `get_session` | `sessionId`；`detail="summary"`；`promptsLimit=10`；`replyBytes=65536` | `get_session` |
| `load_session` | `sessionId`；`agentId=None`；`cwd=None`；`permissionMode=None`；`permissionPolicy=None` | `load_session` |
| `cancel_prompt` | `sessionId`；`promptId=None`；`force=false` | `cancel_prompt` |
| `set_config_option` | `sessionId`，`configId`，`value` | `set_config_option` |
| `resolve_permission` | `sessionId`，`permissionId`，`optionId` | `resolve_permission`（同步） |
| `close_session` | `sessionId` | `close_session` |

权限是两层，不要和后面 P1 的 `agents.toml` `policy` 混成一个字段：

- `permissionMode`：agent 进程自己的沙箱。内置 dsh 是 `read-only` / `workspace-write`（默认）/ `danger-full-access`，通过 `DSH_PERMISSION_MODE` 在**进程启动时**生效。改模式会停进程再用 `session/resume` 拉起来。
- `permissionPolicy`：只回答 agent 自己发来的 `session/request_permission`。取值 `read_only`（默认）/ `allow_all` / `deny_all` / `operator`。`read_only` 只放行 `kind` 属于 `read|search|fetch|think` 的请求；dsh 升级请求常常没有 `kind`，于是落到拒绝。

## 3. 会话怎么活、怎么被重新接上

`SessionManager.recover()` 在 `main()` 里、开始听端口之前跑一次：

1. 读每个 `session.json`。坏 JSON 跳过并打日志。
2. 若记下的 `agent_pid` 仍在，且进程组 id 等于该 pid、命令行能对上当时的 `agent_argv`，就 TERM 再 KILL 整个进程组（`_kill_orphan`）。对不上就不动。
3. `state==busy` 的当前轮改成 `failed`，原因写明网关重启。会话改回 `idle`，`pending_permissions` 清空。
4. 追加事件 `gateway_restarted`。

磁盘上的 `session.json` / `events.jsonl` 还在。下一次 `prompt` 或 `load_session` 走 `_attach`：

- `initialize` 的 `agentCapabilities.sessionCapabilities` 里有 `resume` → `session/resume`。
- 否则看**顶层** `agentCapabilities.loadSession` 是否为真 → `session/load`，并把这段时间的 `session/update` 标上 `replay: true`。
- 两个都没有 → `NOT_RESUMABLE`。
- `load_session` 对网关不认识的 id：必须同时给 `agentId` 和 `cwd`（一般来自 `list_sessions(source="agent")` 的 `session/list`）。

`close_session` 会取消正在跑的轮、在能力里有 `close` 时发 `session/close`，然后停进程。记录留着，`load_session` 可以再打开。

最终答复只投递一次，状态在 `session.json` 的 `delivered_at_sequence` / `delivered_from_sequence`。`final` 第一次看到轮次结束就给正文，之后再查只给 `textBytes`。`messages`/`full` 只在盖住 `prompt_finished` 的那一页给；用交付时同一个 `afterSequence` 再要那一页会再给一次。`includeText=true` 或 `get_session(detail="full")` 总能再取。

## 4. `prompt` 的 60 秒和 `get_events` 的长轮询

这是两段不同的等待，都没有包住整个工具调用的硬超时。

`prompt`（`sessions.py` `prompt`，`MAX_PROMPT_WAIT_MS = 60_000`）：

1. 同一会话一把 `asyncio.Lock`。忙则 `SESSION_BUSY`。关着则 `SESSION_CLOSED`。
2. 进程不在，或这一轮的 `permissionMode` 和活进程不一致：先停再 `_attach`（`initialize` 和 `session/resume|load` **各**最多 `startup_timeout`，默认 25 秒）。
3. 记一轮 `running`，`prompt_started` 只存 200 字预览和总长度（没有 sha256）。
4. `send_request("session/prompt", …)` 把 JSON-RPC 发出去就拿 future，后台任务 `_run_prompt` 等结果。锁在这里放开。
5. `waitMs>0` 时，剩余预算 = `min(waitMs, 60000)/1000` 减去进入 `prompt()` 以来的时间。`asyncio.wait` 等到后台任务结束或预算耗尽。预算已经用完就不再等。
6. 若这一轮在返回前结束，`_turn(..., deliver=True)` 把最终正文放进响应，并记成已投递。否则 `prompt.state` 仍是 `running`。

所以 60 秒限制的是「从进入 `prompt()` 起，最多再堵到满 60 秒」，不是「agent 最多跑 60 秒」。冷启动的 25+25 秒算在这 60 秒里，剩下的才给 `wait`。另外 `start_session` 若带 `model`/`config`，会在返回前调用 `_set_config`，那次 ACP 请求超时是 **30 秒**，不在 `prompt()` 的预算里。`INSTRUCTIONS` 写每个工具 60 秒内返回，代码没有用一个 60 秒的 `wait_for` 包住整个工具。

`get_events`（`MAX_WAIT_MS = 30_000`）：

- 会话 `busy` 且 `waitMs>0` 时循环：`final` 一直等到 `state` 不再是 `busy`，或 `pending_permissions` 非空，或时间到。`messages`/`full` 只要日志出现比 `afterSequence` 新的事件就停。
- 等待用每个会话一个 `asyncio.Event`（`_changed`）。`_emit` 追加日志后 `pop` 并 `set`。检查条件和 `setdefault` 之间没有 `await`，单线程里不会丢唤醒。
- `final` 不按事件分页：`nextSequence` 恒等于 `latestSequence`，`hasMore` 恒为 `false`。
- 只有 `messages`/`full` 用 `EventLog.read(after, limit, maxBytes, include_thoughts)`。`limit` 把被藏起来的 thought 也算进扫描条数，和 acp-mcp 的注释一致。

acp-mcp 0.2.6 的 `get_events` 不是这套：第一次读到空页就 `setTimeout(waitMs)` 睡满再读一次，中间来事件也不会提前返回。不要为了对齐把它换过来。

## 5. operator：`pendingPermissions` 和 `resolve_permission`

数据流（`sessions.py` `_on_request` → `_on_permission` → `resolve_permission`）：

1. 子进程发来带 `id` 的 `session/request_permission`。别的客户端请求直接 `-32601`（网关声明了 `fs.readTextFile/writeTextFile=false`、`terminal=false`）。
2. 请求里的 `toolCall` 若只有 `toolCallId`，用本会话 `tool-calls/<id>.json` 和内存摘要补 `title/kind/status/rawInput`。
3. 生成 `perm-` + 12 hex。本轮 `permission_requests` 加一。写事件 `permission_request`。
4. `policy != "operator"`：按 `kind` 在 agent 给的 `options` 里选 `allow_once`/`allow_always` 或 `reject_once`/`reject_always`。选不中就 `cancelled`。马上回 ACP。
5. `operator`：future 放进 `Runtime.pending`，`_sync_pending` 把条目写入 `session.json` 的 `pending_permissions`。`_on_permission` **一直 `await` 这个 future**。没有 10 分钟超时，也没有默认拒绝。
6. `get_events`（任何 view）和 `get_session` 在有挂起项时带 `pendingPermissions`。对外字段包括 `permissionId`、`promptId`、`toolCallId`、一行 `tool`、`command`（最多 16 KiB）、`sandboxPermissions`、`justification`、`approvalSource`、Auto review 的 `review`、以及 `options[].optionId`。
7. `resolve_permission` 不拿会话锁。`optionId` 必须是该请求里 agent 给出的某一个（**含 `allow_always`，只要 agent 提供了**）。`future.set_result` 后，`_on_permission` 写 `permission_resolved`，并把 `{outcome: selected, optionId}` 回给 ACP。
8. `cancel_prompt`、轮次结束、`_resolve_all_pending` 会把还挂着的 future 收成 `cancelled`。网关重启时磁盘上的 pending 被清空，但对应的 ACP 请求已经随进程没了。

`approvalSource` 是网关推断的：audit 里 `decision=="ask"` → `auto-review`；工具输入带 `sandbox_permissions` → `sandbox-escalation`；否则 `agent`。

## 6. ACP 子进程生命周期

`AcpConnection`（`acp.py`）= 一个进程。`start_new_session=True`，停的时候对进程组发信号。

| 步骤 | 行为 |
|---|---|
| 启动 | `create_subprocess_exec`，cwd 为会话目录，env 为下面第 7 节的整份环境。stdout 一行一条 JSON-RPC；非 JSON 行记入 `agent-stderr.log`。单行上限 64 MiB。 |
| `initialize` | `protocolVersion: 1`，客户端能力关 fs/terminal。超时 `startup_timeout`（默认 25 秒）。能力存在 `Runtime.caps`。 |
| `session/new` | 仅 `start_session`。agent 必须返回匹配 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$` 且目录里还没有的 `sessionId`。 |
| `session/resume` 或 `session/load` | `_attach`。优先 `sessionCapabilities.resume`，否则顶层 `loadSession`。**会读 `loadSession`。** 假 agent 和 `test_load_falls_back_to_session_load_with_replay` 覆盖了这条。 |
| 轮次 | `session/prompt`，正文是一个 `[{type:text}]`。更新经 `session/update` 进事件日志。 |
| 取消 | 通知 `session/cancel`（无 id）。`cancel_prompt` 最多等 5 秒。`force=true` 对进程组 `SIGKILL`。会话仍可再 `prompt`（会重新拉起）。 |
| 退出 | stdin 关掉，最多 5 秒，然后 TERM，再 5 秒，然后 KILL，最后再 KILL 一次清掉同组子孙。意外退出写 `agent_exited`，下一轮 `prompt` 会 `_attach`。 |
| 网关 SIGTERM / 进程退出 | `lifespan` 的 `finally` 调 `shutdown()`：标记停止原因，3 秒grace 停所有 runtime。 |

`session/set_config_option` 成功后的值存在 `config_values`，重新 attach 时再设一次。`session/list` 只在 `list_sessions(source="agent")` 用，列完就停掉那个临时进程。

## 7. 现有安全措施（以代码为准）

| 项 | 现状 |
|---|---|
| 监听地址 | 默认 `127.0.0.1:8765`。这只是 bind，不是 Host 白名单。 |
| Host / Origin | **应用代码没有自己的校验。** `make_app` 把 `host=c.host` 传进 MCP SDK。mcp 2.3.0 的 `Server.streamable_http_app`（`mcp/server/lowlevel/server.py`）在 `transport_security is None` 且 host 是 `127.0.0.1` / `localhost` / `::1` 时自动打开 DNS rebinding 防护：允许的 Host 是 `127.0.0.1:*`、`localhost:*`、`[::1]:*`；允许的 Origin 是对应的 `http://` 这三项。缺 Host 或对不上 → **421**；Origin 对不上 → **403**；没有 Origin 则放行。通配写在 `transport_security.py`：`127.0.0.1:*` 要求冒号后面有端口，光写 `127.0.0.1` 不匹配。host 不是那三个字面量时，这段自动配置**不启用**（SDK 默认把防护关掉）。 |
| 鉴权 | 没有 bearer，没有 OAuth。`make_app` 注释写明不广告任何鉴权。未知路径（含 `/.well-known/*`）返回 JSON `{"error":"not_found"}`。 |
| 请求体 | 应用没改上限。SDK 默认 `max_request_body_size = 4 MiB`，超出 413。只看 `Content-Length` 和实际 body。 |
| HTTP 会话数 | SDK 默认 `max_sessions=10000`，但 `stateless_http=True`，这段不生效。它限制的是 MCP HTTP 会话，不是 ACP 编码会话。 |
| ACP 会话数 | `SessionManager` 没有上限。不同会话可以同时 `prompt`。 |
| cwd | `_check_cwd`：必须是绝对路径、必须是已存在的目录，返回 `os.path.realpath`。没有根白名单，不拒绝符号链接逃出某棵树（realpath 之后落在哪就用哪）。 |
| 环境变量 | `_base_env` 是 `dict(os.environ)`，再把 `DOT_GATEWAY_EXTRA_PATH` 接到 `PATH` 前面。然后叠 agent 定义里的 `env` 和 permission mode 的 `env`。没有白名单，没有按名字拒绝密钥。 |
| 审计 | `prompt_started` 只留 200 字预览和 `textChars`，没有 sha256。事件日志单字符串 8 KiB、单事件 32 KiB。工具全文在 `tool-calls/*.json`，最多 1 MiB。这是会话日志，不是单独的审计 JSONL。 |
| 其他 | 不隔离工作区，不限制 git 推送，没有任务级超时，没有停用开关。和 `AGENTS.md` 一致。 |

cloudflared：官方 origin 参数页（2026-04-17 更新）写 `httpHostHeader` 默认是空字符串，**设置之后**作为发给源站的 `Host`。默认空的时候到底转发哪个 Host，该页没有写死，这次也没有在本环境跑 cloudflared，不能当成已测事实。P2/P4 要用一条真实请求确认。若那个 Host 不是 `127.0.0.1:<port>` / `localhost:<port>` / `[::1]:<port>`，在现有 SDK 默认下会 421。把 `httpHostHeader` 设成 `127.0.0.1:8765` 可以落进现有允许列表；把公网主机名加进允许列表也能通，但允许列表变宽。浏览器跨站请求若带 `Origin: https://<公网主机名>`，对不上现有 `http://127.0.0.1:*` 等三条，会 403。面板若在 `http://127.0.0.1:8766`，Origin 能匹配 `http://127.0.0.1:*`。

## 8. launchd 示例

只有 `docs/local.dot-gateway.plist.example`：

- Label：`local.dot-gateway`
- `ProgramArguments`：`/bin/sh` + `scripts/start.sh`
- `RunAtLoad` 和 `KeepAlive` 都是 true
- stdout/stderr 指到同一份日志路径
- 没有 `EnvironmentVariables`，没有 cloudflared，没有 8766，没有密钥

`scripts/start.sh` 用绝对路径找仓库，再 `uv run --frozen --no-dev`。注释要求把占位路径换成绝对路径，launchd 不展开 `~`。

## 9. 和 acp-mcp 0.2.6 的工具对照

来源：`https://github.com/kxress/acp-mcp` 的 `README.md` 与 `src/mcp/server.ts`（上面的 commit）。16 个 `registerTool` 和 README 的分组一致。参数取自 `inputSchema`，不是 README 的转述。

| dot-gateway 0.2.0 | acp-mcp 0.2.6 | 参数差异 | 建议 |
|---|---|---|---|
| `list_agents()` | `list_agents(cwd?)` | 对方按 cwd 做配置发现，并报告可执行文件在不在 | 保持无参。P1 的注册表以 `agents.toml` 为准，不要跟着做多层 `.acp-mcp.json` 发现 |
| `start_session(cwd, agentId="dsh", prompt?, permissionMode?, permissionPolicy?, model?, config?)` | `start_session(agentId, cwd?, config?, model?, modelSize?, thinkingEffort?, modeId?, permissionPolicy?, prompt?)` | 对方 `agentId` 必填、`cwd` 可选；多 `modelSize`（`small\|medium\|large\|xlarge`）、`thinkingEffort`（`off\|low\|medium\|high\|xhigh\|max\|ultra`）、`modeId`。没有 `permissionMode`。带 prompt 时对方会等到一轮结束才返回 | 保持现名和「发出去就返回」。`permissionMode` 是本仓库的进程沙箱，不要改叫 `modeId` |
| 无 | `run_task(agentId, task, cwd?, config?, model?, modelSize?, thinkingEffort?, modeId?, permissionPolicy?, idleTimeoutMs?=600000, permissionLeaseMs?=120000)` | 一次性任务，默认 `permissionPolicy=read_only`，做完就关会话 | P1 不必做。要用也是 `start_session` + `prompt(waitMs)` + `close_session` 的薄封装，且空闲超时不能照搬 10 分钟以上，否则顶破隧道 |
| 无 | `list_models(agentId, cwd?, enrich?=false, timeoutMs?=30000)` | 会拉起一次活会话问模型 | 已有 `get_session(detail="full").configOptions`。不单独立项，除非某个 agent 的模型不在 config option 里 |
| `load_session(sessionId, agentId?, cwd?, permissionMode?, permissionPolicy?)` | `load_session(agentId, acpSessionId, cwd, permissionPolicy?)` | 对方三个必填，且只走 agent 的 load；本仓库 `sessionId` 就是网关 id，能 resume 就 resume | **不要**把参数改成 `acpSessionId`。不需要别名 |
| `prompt(sessionId, text, permissionMode?, permissionPolicy?, waitMs=0` 最大 60000`)` | `prompt(sessionId, text, wait?=true, waitMs?=60000` 最大 120000`)` | 对方默认一直等到结束，上限 120 秒 | 保持 `waitMs` 默认 0、上限 60000。不要加 `wait=true` 的默认行为 |
| `get_events(sessionId, afterSequence=0, view="final", waitMs=0, includeText=false, includeToolOutput=false, limit=100, maxBytes=200000, includeThoughts=false, replyBytes=65536)` | `get_events(sessionId, afterSequence?, limit? 1..500, waitMs? 最大 30000, includeRaw?=false, includeThoughts?=false)` | 对方没有 view / 答复只投递一次 / 工具输出摘录；等待实现是睡满 | 保持现有 view。这是调用方契约，别名没有好处 |
| `get_tool_call(sessionId, toolCallId, offset=0, maxBytes=32768)` | 无 | 本仓库多出来的分页全文 | 保留。acp-mcp 不需要这条 |
| `list_sessions(cwd?, state?, limit=20, source="gateway", agentId="dsh", cursor?)` | `list_sessions()` 无参 | 对方只列自己内存里的活会话 | 保持。`source=agent` 是 dsh `session/list` 接回所依赖的 |
| `get_session(sessionId, detail="summary", promptsLimit=10, replyBytes=65536)` | `get_session(sessionId)` | 对方附带当前 prompt 正文、modes、authMethods、acpProtocol | 保持 detail。P1 不必为了字段对齐去改 |
| `set_config_option(sessionId, configId, value)` | 同名同参 | 对方对模型选项还有 model policy | 保持 |
| 无 | `set_mode(sessionId, modeId)` | ACP `session/set_mode`。本仓库换沙箱是重启进程 | P1 不做。dsh / `codebuddy --permission-mode` 都是启动参数，不是会话中途的 mode |
| `cancel_prompt(sessionId, promptId?, force=false)` | `cancel_prompt(sessionId, promptId)` `promptId` 必填 | 本仓库多 `force` 杀进程组 | 保持可选 `promptId` 和 `force`。不要收紧成必填 |
| `resolve_permission(sessionId, permissionId, optionId)` | 同名同参 | 对方 operator 租约在 `run_task` 里默认 120 秒；本仓库无租约 | 名字保持。P1 的 10 分钟超时做在 `_on_permission`，不要只存在 `run_task` |
| `close_session(sessionId)` | 同名同参 | 一致 | 保持 |
| 无 | `list_stale_workspaces(olderThanMs?)`、`cleanup_stale_workspaces(olderThanMs?)` | 清理 acp-mcp 自己建的一次性工作区 | **不移植。** 本仓库不创建那种目录 |

别名：不建议加。12 个名字已经是对外契约（`INSTRUCTIONS`、README、既有测试都用这套）。acp-mcp 多出来的参数若做成同名工具的可选参数，旧调用不受影响，但 `prompt` 的默认等待和 `load_session` 的必填参数一旦「对齐」就会破坏现在的调用方。P1 的 `policy=full|operator` 是 agent 配置项，不是把 `permissionPolicy` 改名。

## 10. 测试

环境：Linux，CPython 3.12.3，uv 0.13.6，`uv sync` 后执行。工作目录是仓库根。

```text
$ uv run pytest -q --tb=no
.............................................                            [100%]
45 passed in 66.32s (0:01:06)
```

45 来自 `tests/test_sessions.py`（其中 `test_permission_policies` 有 5 组参数）和 `tests/test_mcp.py`。假 agent 在进程内，不访问网络，不调用模型。

没跑、因此不能当作通过：

- `uv run python scripts/smoke_dsh.py`
- `uv run python scripts/test_auto_review.py`
- macOS、launchd、cloudflared、Access、钥匙串

## 11. P1–P4：改哪里、风险、模块怎么切

P1 的 `policy` 和今天的 `permissionPolicy` 不是同一个词。今天默认 `read_only`，会自动拒绝非读类权限请求；P1 要求默认 `full`，operator 默认关。若只把默认值改成 `allow_all` 而不改启动参数，dsh 仍是 `workspace-write`，和「full」字面不是一回事。`codebuddy --acp --permission-mode bypassPermissions` 这种「进程已经是 full」的 agent，网关侧 `read_only` 本来就拦不住它不来问权限的操作。

### P1 配置、cwd、环境、审计、并发

| 需求 | 先改这些 | 风险 |
|---|---|---|
| `agents.toml`：`id/command/args/policy/approval_timeout/env_allow/secret_env/cwd_roots/max_sessions` | 新解析放在 `config.py` 或单独的加载函数；`agents.py` 的 `AgentDefinition` / `load_registry` / `builtin_dsh` 今天只认 JSON 和 `DOT_GATEWAY_AGENTS_JSON`。`server.py` `parse_args` 的 `--config` 也是 JSON。stdlib `tomllib` 够用，不必新依赖 | 两套注册表会分叉。内置 dsh 若仍默认 `workspace-write` + 网关 `read_only`，和「所有 agent 默认 policy=full」矛盾。建议 toml 覆盖内置项，旧 JSON 只在明确兼容期内读，并写进测试 |
| `policy=full\|operator`，默认 full；operator 默认关 | `sessions.py` `_validate_policy`、`prompt`、`start_session`、`load_session`；`server.py` 工具描述和 `INSTRUCTIONS`；`config.py` `PERMISSION_POLICIES` | **调用方现在可以在 `prompt(permissionPolicy=allow_all)` 里盖掉会话策略。** 若 toml 是上限，这个参数不能把 `operator` 降成自动放行。`full` 建议映射为「自动回答 allow，且只在 agent 自己会发权限请求时有意义」，不要假装它是 OS 沙箱 |
| operator 10 分钟未答则 reject；只接受 `allow_once` / `reject` | `sessions.py` `_on_permission`、`resolve_permission`、`_pending` | 今天 `await fut` 没有超时；`optionId` 只要 agent 给了就收，包括 `allow_always`。超时必须 `set_result` 成 reject，不能只从 `pending` 里删掉（否则 ACP 请求一直挂着，轮次不结束）。面板和 `resolve_permission` 会同时完成同一个 future，要用 `fut.done()` 挡第二次。`approval_timeout` 按 agent 配置，缺省 600 秒 |
| cwd 白名单，`realpath`，`/tmp`→`/private/tmp` | `sessions.py` `_check_cwd`；`list_sessions` 里另一处 `os.path.realpath`；`start_session` / `load_session` / `prompt` 的 attach | 今天已经 `realpath`，所以 macOS 上 `/tmp` 这个符号链接会变成 `/private/tmp`。**还没有白名单。** 用字符串 `startswith("/tmp")` 会漏掉 `/private/tmp`，也会误放 `/tmp-evil`。应先 `realpath` 白名单根和目标，再比「相等或目录前缀 + 分隔符」。`/tmp` 不是符号链接的系统（这份 Linux CI 就是）上，`/tmp` 的 realpath 仍是 `/tmp`，硬编码替换会把合法目录改没。符号链接指向白名单外时，比的必须是 realpath 之后的路径 |
| 子进程环境白名单 + 密钥名兜底拒绝 | `sessions.py` `_base_env`；`agents.py` `AgentDefinition.launch`（会 `env.update(self.env)`） | **今天原样继承 `os.environ`。** launchd 环境可能很短，从登录 shell 或带密钥的父进程拉起时，`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`GITHUB_TOKEN` 等会进 agent。白名单要同时卡：父环境只留 `env_allow`，agent `env` 里的值也不能绕过 `secret_env`。名字兜底建议大小写不敏感匹配 `KEY`/`TOKEN`/`SECRET`/`PASSWORD`/`CREDENTIAL`，命中则丢，除非明确列在 allow。`PATH` 现在会强制前置 `extra_path`，收紧时要单独决定留不留 |
| JSONL 审计：prompt 截断 500 字 + 长度 + sha256 | 新写入器。现有 `events.py` `EventLog.append` 和 `prompt()` 里的 `prompt_started`（200 字、无 hash）不要直接加长 | 会话日志和审计要分开。把全文写进 `events.jsonl` 会进 `get_events`。hash 用规范化后的 UTF-8。工具输出里同样可能有密钥，审计只记 id、状态、长度、hash，不记全文 |
| 并发和请求体上限 | 会话数：`SessionManager.start_session` / `_spawn`。请求体：`server.py` `make_app` 已可把 `max_request_body_size` 传给 SDK（现在用默认 4 MiB） | 见下一节 |

### 并发下会话状态会怎样

已经有的保护：每个 `session_id` 一把 `asyncio.Lock`，`prompt` / `load_session` / `close_session` / `set_config_option` 会拿。事件循环是单线程，`_save` 和 `EventLog.append` 中间没有 `await` 时不会互相插队。`get_events` 的等待在检查和注册 Event 之间没有 `await`。

没有挡住的点：

- `cancel_prompt` 和 `resolve_permission` **不拿锁**。`prompt` 在 `send_request` 处会 `await`，此时 `state` 已经是 `busy`，但 `prompt_task` 还没赋上。这段窗口里 `cancel_prompt` 看到 `prompt_task is None` 就返回「没有在跑的轮」，取消丢失。随后 `prompt` 把 `cancel_requested` 写成 `False`。
- `get_events` 不拿锁。和 `_run_prompt` 写的是同一份 dict，单线程下最后一次 `_save` 会带上两边的字段；但 `tool-calls/<id>.json` 用 `write_text`，不是原子替换，`get_tool_call` 可能读到半截。
- 同一会话的权限 future 挂在 `Runtime.pending`。两个 `resolve_permission` 里只有第一个 `set_result` 生效，第二个应明确返回已处理，而不是再选一次。
- 没有「全局最多 N 个活 agent」。P1 的 `max_sessions` 要放在 `_spawn` 前，按 agent 计数，超时或启动失败要把名额还回去。不要拿 SDK 那个 stateless 下不生效的 HTTP `max_sessions` 充数。
- `_run_prompt` 不拿锁就把 `state` 改回 `idle`。和 `prompt` 的锁配合，正常不会双开；新增的超时任务、面板请求、审计写入如果也改 `session.json`，必须走同一把锁，或者只改自己的文件。

### P2 bearer 和 Host

| 需求 | 先改 | 风险 |
|---|---|---|
| bearer：钥匙串或 `0600` 文件；`hmac.compare_digest` | `server.py` `make_app` / `main`。新的小模块读密钥，中间件只接 `/mcp`。比较对象是 header 里的 token 和本地那一份 | 失败路径不能把 token 写进日志或 401 正文。文件权限在复制、备份之后要再检查。Linux CI 没有钥匙串，测试要用文件后端。本机没有 bearer 时，任何能访问 `127.0.0.1:8765` 的进程都能开会话 |
| Host 白名单 | 优先显式传入 SDK 的 `transport_security`，不要依赖「host 参数恰好是 `127.0.0.1` 才自动开启」 | 见第 7 节。`httpHostHeader` 必须带端口才匹配 `127.0.0.1:*`。公网名放进 `allowed_hosts` 之前，先确认 cloudflared 实际送的 Host。Origin 白名单要同时放下面板源和隧道会带上的 Origin；空 Origin 今天是放行的，浏览器攻击通常会带 Origin，非浏览器客户端往往不带 |

P2 不替代 P1 的 env/cwd 限制。隧道和 Access 挡的是外面的人；本机进程、以及 agent 继承到的密钥，是另一条路。

### P3 面板 `127.0.0.1:8766`

今天没有第二个端口，也没有 sqlite、SSE、HTML。`pyproject.toml` 没有 sqlite 驱动依赖（stdlib `sqlite3` 可用）。

建议新模块挂第二个 ASGI app 或同一进程里第二个 uvicorn，**不要**把面板路由塞进 MCP 的 `streamable_http_app` 再靠路径区分。批准按钮只调用和 `resolve_permission` 同一条路径，并且只能提交 `allow_once` 或 `reject`。SSE 若直接转发 `events.jsonl`，会把工具输出推到浏览器；面板用审计流和 `pendingPermissions` 的摘要即可。

历史库和 `session.json` 会双写。崩溃时以 `session.json` 的轮次状态为准，sqlite 只做面板查询，避免两套状态抢 `state==busy`。

### P4 launchd / cloudflared

在 `docs/local.dot-gateway.plist.example` 之外加 cloudflared 的配置示例和第二个 plist。`scripts/start.sh` 仍不该写死密钥。

plist 里不要放 API key。`httpHostHeader: 127.0.0.1:8765` 要和 P2 的允许列表一起写，并注明这是为了满足现有 SDK 规则，不是猜测 cloudflared 的默认 Host。Access 在边缘；源站仍要 bearer，因为隧道配置一旦没套上 Access，或本机直接访问 8765，边缘策略不生效。

### 建议的模块

保持 `acp.py` 只做 stdio。`sessions.py` 已经 1500 行，P1 不要再把策略、环境和审计堆进去。

| 模块 | 内容 |
|---|---|
| `config.py` | 监听、状态目录、超时；加载 `agents.toml` |
| `agents.py` | `AgentDefinition` 增加 policy、cwd_roots、env_allow、secret_env、max_sessions、approval_timeout；`launch` 只接收已经过滤的 env |
| `procenv.py`（新） | 环境白名单和密钥名拒绝；单测覆盖「父进程有密钥 / toml 又写了密钥 / PATH」 |
| `cwd.py`（新） | `check_cwd(path, roots)`：expanduser、绝对路径、存在、realpath、前缀。单测含普通目录、符号链接、`/tmp` 与 `/private/tmp`、前缀粘连 |
| `audit.py`（新） | 只追加的审计 JSONL：时间、session、agent、prompt 的 500 字、长度、sha256、结果。与 `events.py` 分开 |
| `sessions.py` | 继续管生命周期；`_base_env` / `_check_cwd` 改调用上面两个模块；`_on_permission` 加超时和 option 过滤；`_spawn` 前数 `max_sessions` |
| `auth.py`（新，P2） | 读钥匙串或 0600 文件；`compare_digest` |
| `server.py` | 工具契约保持 12 个名字；显式 `transport_security` 和 body 上限；挂 bearer |
| `panel.py`（新，P3） | 8766、SSE、sqlite、批准 |
| `docs/` + `scripts/`（P4） | plist、cloudflared 示例、`httpHostHeader` |

P5 联调不在这次改动清单里。P0 没有 macOS 机器、没有 cloudflared、没有钥匙串，上面凡是依赖那三样的句子都标了「代码如此」或「没跑过」。
