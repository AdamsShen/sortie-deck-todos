# DSH（DeepSeek Harness）harness provider 接入

- 日期：2026-11-09
- 状态：已实现（本地验证通过；镜像验收见文末「状态」）
- 关联：`src/sortie_deck/harness_providers/dsh.py`、`docs/zh/modules/harness.md`

## 1. 背景

Agent harness 抽象已经支持 `claude_code` / `opencode` / `codex` / `cursor` / `mock`，但本机与云端实际可用的只有 `claude_code` 与 `codex`。DeepSeek Harness（命令名 `dsh`，仓库 `deepseek-ai/deepseek-harness`，MIT）提供了官方 Python SDK，目标是把它接成与 `claude_code` 同级的 provider，而不是最小可用版本。

## 2. 接入面调研（三选一）

| 形态 | 能力 | 结论 |
|---|---|---|
| **官方 Python SDK** `deepseek-harness-sdk` | `DeepSeekHarness.run()` / `resume_session()`、通知流、`dsh_home` 隔离 | **采用**：直接对齐 `HarnessProvider` 契约，Python 侧零桥接 |
| headless CLI `dsh --profile headless "<task>"` | 只打印最后一条 assistant 文本、无事件流、无 resume | 信息量不足，放弃 |
| web profile（`dsh-host-webserver` + Typert RPC） | 完整但需要按生成契约复刻客户端，且服务无鉴权/TLS | 成本过高，放弃 |

来源：

- PyPI `https://pypi.org/project/deepseek-harness-sdk/`（最新 `0.2.1rc2`，2026-10-17，MIT，依赖 `deepseek-harness-runtime-bin==0.2.1rc2` + `pydantic>=2.12,<3`）
- 官方文档 `https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/python-sdk.md`（`DeepSeekHarness(provider=, model=, cwd=, dsh_home=, profile=, patches=)`、`run(prompt, session_id=)`、`resume_session()`、`dsh_home` 必须显式传）
- 本机安装的 0.2.1rc2 包内 README 与 `--dump-default-config` 输出（见下）

## 3. PoC 实测（2026-11-09，macOS arm64）

| 项 | 结果 |
|---|---|
| 安装 | `deepseek-harness-sdk==0.2.1rc2` + runtime 90MB；项目 venv（pydantic 2.14.1）零冲突；直连 pypi 超时，需镜像源 |
| 单轮 | 6.2s，`finish_reason=completed`，文件正确落盘，29 events / 2 次 tool call |
| 同进程第二轮 | 1.0s，上下文正确 |
| **跨进程 `resume_session()`** | 6.9s，历史恢复并继续改文件成功 |
| 3 会话并发（同实例） | wall 4.0s，全部 completed，无串话 |
| 冷启动 | 首个通知 0.73s |
| 通知实时性 | `on_notification` 实时（+0.73s turn/start → +2.98s tool/call → +3.78s turn/end） |
| 鉴权失败 | 不抛异常：`finish_reason=error`，`turn/end.data.reason.error.code=AUTH`、`status=401` |
| 写 workspace 外 | 被拒（`Operation not permitted` + `[sandbox: file access denied under workspace-write mode]`），`/tmp` 例外 |
| 读 workspace 外 | **不受限**（能读 `~/.dsh/.credentials.yaml`） |
| `session-log-deepseek` | 默认开启并每轮 ack；patch `disabled: true` 后事件消失 |
| token 用量 | 事件中不存在 |

## 4. M0 spike 结论

1. **`@deepseek-ai/dsh-mcp-client` 可经 patch 插入**：`sdk` profile 本身只有 `mcp-resources`，没有 mcp-client；patch 里 `insert` 一行后，真实 stdio MCP server 的工具以 `mcp__<serverName>__<tool>` 出现在模型工具表并可调用（实测 `mcp__poc__echo_poc` 返回 `poc-mcp:hi`）。config 契约：`serverName` / `transport`（`stdio` \| `streamable-http`）/ `command` / `args` / `env` / `url` / `headers`，**不支持 sse**。
2. **技能根隔离有效**：`skill-filesystem` 设 `includeDefaultRoots: false` + `customSkillDirs: []` 后，任务提示里不再出现宿主机 `~/.agents/skills` 的技能清单。
3. **工具粒度限制**：`dsh-tools` 只有 `mode` 配置，`tool-fs` 没有按工具开关 → 只能整行禁用。映射：Bash→`tool-bash`、Grep/Glob→`tool-fs-search`、Task→`tool-subagent`、WebFetch/WebSearch→`tool-web`、TodoWrite→`tool-todo`、Write/Edit→`tool-fs`；**Read 无法单独禁用**（禁用 `tool-fs` 会连 write/edit 一起关掉），记 warn 后保留。
4. **Linux 可用**：PyPI 提供 `manylinux_2_28_x86_64` / `aarch64` wheel（约 95MB），Debian 12（glibc 2.36）满足要求。
5. **无回合上限**：`agent-loop` 只有 `agents: []`，没有 maxTurns → 只能靠超时兜底。
6. **单可写根**：`dsh-sandbox-policy` 文档明确「extra writable roots are not part of SandboxExecutionPolicy」→ `extra_dirs` 无法兑现。

## 5. 设计

### 5.1 进程生命周期：每轮新进程 + resume

执行器全文不调用 `provider.close()`，`Session.run()` 阻塞且无 cancel。因此 provider 在 `send()` 内用 `asyncio.to_thread` 新建 `DeepSeekHarness`，`native_id` 非空时 `resume_session()`，否则 `start_session()`，`finally` 关闭并清理临时 patch。代价 0.73s 冷启动；收益是每轮可以按阶段生成 patch，能力不会跨阶段泄漏。

超时用 `threading.Timer` 看门狗：到点直接 `harness.close()` 杀子进程，让阻塞的 `run()` 抛错后映射成 `fail(meta.timeout=True)`。

### 5.2 patch 分层

| patch | 内容 | 生效范围 |
|---|---|---|
| `dsh_patches/base.yml`（随包静态） | 关 `session-log-deepseek`；`skill-filesystem` 设 `includeDefaultRoots: false`、`customSkillDirs: []` | 每轮 |
| `log-on.yml` / `skills-on.yml`（按配置生成） | 显式打开上传或恢复宿主机技能根时，用后置 patch 覆盖 base | 每轮 |
| `tools-off.yml`（按 `disallowed_tools` 生成） | `- id: tool-*` + `disabled: true` | 每轮 |
| `mcp.yml`（按 `mcp_servers` 生成） | `insert @deepseek-ai/dsh-mcp-client`，stdio / streamable-http | 每轮 |

动态 patch 写在 `<dsh_home>/_patches/<session>-<uuid>/`，目录 0700、文件 0600（MCP 凭据可能在里面），`finally` 递归删除。

### 5.3 环境与安全默认

| 变量 | 值 |
|---|---|
| `DSH_HOME` | `<data_dir>/dsh_home/<initiative_id>`（由 SDK 写入，禁用 `~/.dsh`） |
| `DSH_AGENTS_HOME` | `<dsh_home>/agents`（切断 `~/.agents/skills`） |
| `DSH_PERMISSION_MODE` | `TDT_DSH_PERMISSION_MODE`，默认 `workspace-write` |
| `DEEPSEEK_API_KEY` | `TDT_DSH_API_KEY` 或进程环境，只经 env 注入，不落盘不打印 |
| `HOME` | 默认不隔离（`git commit` 需要 `~/.gitconfig`），`TDT_DSH_ISOLATE_HOME=true` 可开 |

### 5.4 线程到事件循环

`on_notification` 在 `to_thread` 的 worker 线程触发；`tool/call` 的 `arguments` 是 JSON 字符串，解析后按 `file_path`/`command`/`pattern` 顺序取短目标，0.8s 节流，协程经 `asyncio.run_coroutine_threadsafe` 回投主循环；任何异常只记 debug。

### 5.5 错误映射

| 条件 | 结果 |
|---|---|
| `error.code in {AUTH, UNAUTHORIZED, INVALID_API_KEY}` 或 `status in {401,403}` | `needs_auth`（走 HITL auth_block） |
| 其它 `turn/end.reason.kind=error` | `fail`（即使有正文也不当成功交付） |
| 看门狗触发 | `fail` + `meta.timeout=True` |
| `resume_session` 失败 | 记 warn，回退新会话（id 追加 6 位随机后缀） |
| SDK 缺失 | probe `missing`；send `fail(meta.sdk_missing=True)` |

### 5.6 能力对照（claude_code vs dsh）

| 能力 | claude_code | dsh |
|---|---|---|
| 会话续跑 | `resume=native_id` | `resume_session(native_id)` ✅ |
| 工具级进度 | `AssistantMessage` ToolUseBlock | `tool/call` 通知 ✅ |
| 鉴权中断 | `needs_auth` | `reason.error.code=AUTH` → `needs_auth` ✅ |
| 阶段 MCP | `mcp_servers` + `strict_mcp_config` | 动态 patch 挂 `dsh-mcp-client` ✅（sse 不支持 ⚠️） |
| 禁用工具 | `disallowed_tools` 精确到工具 | 整行禁用；Read 无法单独禁用 ⚠️ |
| 多可写目录 | `add_dirs` | 不支持（读不受限，产物走归档）⚠️ |
| 回合上限 | `max_turns` | 无，靠超时 ⚠️ |
| 上下文压缩信息 | `compact_boundary` | 未提供 ⚠️ |
| token 用量/成本 | `ResultMessage.usage` | 无 ❌ |

## 6. 文件改动

| 文件 | 变更 |
|---|---|
| `src/sortie_deck/harness_providers/dsh.py` | 新增 provider（probe/send/close、patch 生成、线程桥、看门狗、错误映射） |
| `src/sortie_deck/harness_providers/dsh_patches/base.yml` | 新增静态安全 patch |
| `src/sortie_deck/harness_providers/base.py` | `HARNESS_NAMES` / `HARNESS_LABELS` 加 `dsh`，注册 `DshProvider` |
| `src/sortie_deck/harness_providers/executor.py` | `_send_turn` 增加 `supports_extra_write_dirs` 能力位判断 |
| `src/sortie_deck/settings.py` | 新增 `dsh_*` 配置；`review_lens_timeout_by_harness` 加 `dsh=900` |
| `pyproject.toml` | 新增 `harness-dsh = ["deepseek-harness-sdk==0.2.1rc2"]` |
| `Dockerfile` | `runtime-harness` 安装 `[harness-claude,harness-dsh]`，`PIP_INDEX_URL` 可传镜像 |
| `.env.example` / `.env.docker.example` | 补 DSH 变量与凭据说明 |
| `scripts/verify_collab.py` | `--harness` 白名单加 `dsh` |
| `tests/test_harness_dsh.py` | provider 单测（probe 三态、ok/auth/fail/timeout、resume 回退、活动桥、能力位） |
| `tests/test_harness_dsh_patches.py` | patch 生成单测（工具映射、MCP 转换、base patch、清理） |
| `docs/zh|en/modules/harness.md` | provider 表格 + 「DSH 说明」小节 |
| `CHANGELOG.md` | Unreleased/Added 条目 |

## 7. 测试

- 新增 25 条单测，全部通过（`pytest tests/test_harness_dsh*.py`）。
- 既有 harness 回归 67 条通过（`test_harness*.py`、`test_executors.py`）。
- `ruff check` / `ruff format` 对改动文件无告警。

## 8. 验收标准与结果

| 标准 | 结果 |
|---|---|
| `/api/harnesses` 返回 `dsh`（label=DeepSeek Harness） | ✅ 注册表冒烟通过（本机未装 SDK 时 probe=`missing`） |
| 选 dsh 的任务能跑完文档阶段与编码阶段 | ◑ provider 级：文档阶段落盘 + 编码阶段 MCP 落盘通过；完整链路待部署环境 |
| 技术方案/自审轮工具禁用生效 | ✅ 真实运行时工具表验证（Read 记 warn） |
| 错误密钥进入 HITL 鉴权中断 | ✅ 单测覆盖 |
| 第二轮能引用第一轮内容 | ✅ 本机与镜像内跨进程 resume 实测 |
| 无会话日志上传、无宿主机技能泄漏 | ✅ PoC 实测 |
| 镜像内安装与 probe | ✅ `runtime-harness` 构建成功，镜像内 `probe dsh=ok` |

## 8.1 首次真机任务暴露的问题（已修复）

2026-11-09 首次用 dsh 跑真实任务（`ini_db3b6603109d`）时发现：eng_implement 的 5 轮自审全部
报 `JsonRpcError: session "hs_dsh_..." already exists`，但阶段仍记为 `ok`，属静默降级。

根因：执行器只在「小群讨论」路径把 `reply.native_id` 回写到 session，主回合与自审轮不写；
provider 在 `native_id` 为空时直接 `start_session(同一 id)`，而 DSH 的创建发生在 `run()`，
同一 id 已落盘就报「已存在」。

修复：`_open_session()` 改为「先 resume、失败再按同一 id 新建」，并在 `run()` 抛冲突类错误
（already exists / occupied / locked）时换独立后缀会话重试一次。真实运行时验证：不回写
`native_id` 的连续两轮、以及两轮并发都能成功。

## 9. 风险与回滚

| 风险 | 处理 |
|---|---|
| rc 版本 breaking change | 精确 pin `0.2.1rc2`；`_sdk_available()` 失败降级 `missing`，走 `harness_fallback` |
| 读不受限（可读宿主机凭据） | 容器隔离为主；文档标注；不把密钥放在 agent 可读路径 |
| 阶段 MCP 只支持 stdio / streamable-http | `sse` 跳过并记 warn + `meta.mcp_skipped` |
| 每轮 0.7s 冷启动 | 可接受；备选「按 initiative 缓存 + 空闲回收」留档未实现 |
| 无 token 用量 | 文档标注，成本统计不纳入本期 |

回滚：`default_harness` 仍为 `claude_code`；改动都是新增文件 + 注册表一行 + 配置追加，单 commit 可整体 revert。

## 10. 状态

- M0 spike：完成（结论见第 4 节）
- M1–M4（骨架 / 安全默认 / 能力对齐 / 测试与文档）：完成
- M5 真机验收：
  - **本机真实 SDK（provider 级）**：`probe=ok`；文档阶段传 `disallowed_tools=["Bash"]`，agent 改用 `write` 落 `prd.md`（3 条要点）；同会话跨进程 `resume_session` 追加 `## Result` 成功；MCP stdio server 的 `mcp__poc__echo_poc` 返回 `poc-mcp:acceptance` 并写入 `note.txt`；`_patches` 目录自动清理、`dsh_home` 会话日志正常生成。
  - **工具禁用实测**：`["Bash"]` → 工具表无 `bash`；`["Read","Bash","Grep","Glob"]` → 无 `bash`/`grep`，`read` 保留（warn 已记录）。
  - **镜像**：`docker build --target runtime-harness`（本地 node/python 镜像 + 清华 pip 源 + npmmirror）成功，产物 1.85GB；镜像内 `probe dsh=ok`，provider 两轮（含跨进程 resume）落盘通过。
  - **未覆盖**：完整任务链路（PRD → 编码 → 自审）需要 LLM 编排密钥与队列 worker，本机未跑，留待部署环境验收；provider 级等价行为（文档阶段禁工具、编码阶段 MCP、自审轮禁用清单）已单独验证。
