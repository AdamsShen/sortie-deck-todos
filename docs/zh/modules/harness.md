# Agent Harness

## 1. 概述

两层相关但不同的能力：

1. **进程内公用 Agent 循环** —— Sortie Deck 内部流程使用的循环
   （工具、轮次限制、确认门禁、瀑布流轨迹），参考 AWS **Strands / AgentCore
   harness** 思想，不绑定厂商 SDK。
2. **外部 Harness Provider**（新增）—— 系统级全局能力：接入外部 agent
   harness（Claude Code / OpenCode / Codex / Cursor），让一个任务的**全部阶段
   对话**都在其中运行。

| 项 | 值 |
|----|-----|
| 进程内循环 | `harness/__init__.py`（首个消费者：`mission_briefing.py`，`/api/briefing/sessions`） |
| Provider | `harness_providers/`（`base.py` 注册表 + 各厂商一个模块） |
| 阶段执行器 | `harness_providers/executor.py::HarnessStageExecutor`（`harness_stage`） |
| 任务记忆 | `mission_memory.py` → `data/missions/<id>/memory/` |
| 共享记忆 | `memory.py`（`MemoryStore` + 可选 mem0） |
| API | `GET /api/harnesses`、`PATCH /api/initiatives/{id}/harness`、HITL `switch_harness` |
| 瀑布图编排 | `harness/waterfall.py`（五步确定性 `StateGraph`） |
| LLM 接入层 | `llm.py` 门面 + `llm_chain.py`（LangChain `ChatOpenAI` 单一实现） |

### 1.1 建队瀑布图（LangGraph）

建队规划的五步瀑布已从接口层收回 `harness/` 目录，由 `harness/waterfall.py`
的 LangGraph 图驱动：

```text
START → search_history → retrieve_knowledge → propose_pipeline
      → propose_loadout → ensure_missing_roles → END
```

- **固定边，不用 `create_react_agent`**：前端依赖固定的 `data.step` 序列
  （`history` / `knowledge` / `pipeline` / `loadout` / `roles`）渲染瀑布动画，
  且 `ensure_missing_roles` 有「创建角色」的副作用，不能让模型决定调用次数。
- **先 emit 事件、再调工具**，事件文案与顺序与旧实现逐字一致。
- **工具失败不中断链路**：`call_tool` 把异常收成 `ok=False`，只是不写
  `context`，后续节点读到空值继续跑。
- **不挂 checkpointer**：会话持久化由 `PlannerSessionStore`（`missions/<id>/chat/planner.*`）承担，避免双写。
- `harness/__init__.py` 的 `HarnessEvent` / `ToolSpec` / `Harness` 契约不变，
  前端、存储格式与 `harness_providers/` 零改动。

## 2. Provider

```text
HarnessProvider(Protocol)
  probe() → ProbeStatus             # 可用性 / 凭据探测，TTL 缓存
  start_session() → HarnessSession  # 多会话
  send(session, prompt, cwd, context_pack) → HarnessReply
  close(session)
```

| 名称 | 接入方式 | 说明 |
|------|----------|------|
| `claude_code` | `claude-agent-sdk`（`query()` + `resume`） | 权限模式取 `TDT_CODING_PERMISSION_MODE`，任务级 override 优先（见 2.2）；需 `ANTHROPIC_API_KEY` |
| `dsh` | 官方 `deepseek-harness-sdk`（`DeepSeekHarness` + `resume_session`，stdio JSON-RPC + 随包运行时） | 凭据 `TDT_DSH_API_KEY` 或 `DEEPSEEK_API_KEY`；profile `sdk`；沙箱 `TDT_DSH_PERMISSION_MODE`（默认 `workspace-write`）；每轮新进程靠 resume 续跑，见「2.1 DSH 说明」 |
| `opencode` | HTTP 调云端部署的 `opencode serve`（`TDT_OPENCODE_BASE_URL`） | 服务端接入，不起本地 CLI |
| `codex` | 云端容器内 `codex exec --json` | 会话续用 `codex exec resume`；凭据认 `OPENAI_API_KEY` / `CODEX_API_KEY` 或 `$CODEX_HOME/auth.json`（`codex login`）；沙箱取 `TDT_CODEX_SANDBOX`（默认 `workspace-write`）；兼容 `item.completed` 新版事件与旧版 `agent_message` |
| `cursor` | `cursor-agent -p --output-format json`（云端容器） | `CURSOR_API_KEY` / `cursor-agent login` |
| `mock` | 内置回声 | 永远可用的回退 |

### 2.1 DSH（DeepSeek Harness）说明

- **依赖**：`pip install -e ".[harness-dsh]"`（精确 pin `deepseek-harness-sdk==0.2.1rc2`，随包附带同版本运行时，约 95MB/平台，**不需要系统 Node**）。
- **隔离**：每个任务独立 `DSH_HOME`（`<data_dir>/dsh_home/<initiative_id>`），不读 `~/.dsh`；默认 patch 关闭会话日志上传（`session-log-deepseek`，否则每轮都会把会话事件发给 DeepSeek），并切断宿主机技能根（`skill-filesystem.includeDefaultRoots: false`），避免 `~/.agents/skills` 被注入任务提示。
- **生命周期**：执行器不调用 `provider.close()`，且 `Session.run()` 阻塞、无 cancel → provider 每轮新建子进程、跑完即关，多轮靠 `resume_session(native_id)` 续跑（实测跨进程恢复可用，冷启动约 0.7s）；单轮带看门狗，超时直接关进程逼它退出。
- **阶段能力**：`disallowed_tools` 转成 DSH 的「禁用整行」patch（Bash→`tool-bash`、Grep/Glob→`tool-fs-search`、Task→`tool-subagent`、WebFetch/WebSearch→`tool-web`、TodoWrite→`tool-todo`）；**`Read` 无法单独禁用**（`tool-fs` 同时注册 read/write/edit），会在日志记 warn。阶段 MCP 通过动态 insert `@deepseek-ai/dsh-mcp-client` 挂载，只支持 `stdio` 与 `streamable-http`，`sse` 会跳过并记 warn。
- **已知差距**：一个会话只有一个可写根，`extra_dirs` 拿不到写权限（读不受限，产物走既有归档兜底）；没有回合上限配置，只能靠请求/阶段超时；事件里没有 token 用量与成本，成本统计不纳入本期。
- **安全提示**：DSH 沙箱只限制**写**，读不受限（能读 `~/.ssh`、`.env` 等），云端必须靠容器隔离，不要把密钥放在 agent 可读路径。

### 2.2 权限模式与环境阻塞

- **解析口径**：`permissions.resolve_permission_mode()` 按「任务级 override（HITL 指令里的 `[sortie:coding_permission_mode=...]` 或 `StageContext.permissions`）> `TDT_CODING_PERMISSION_MODE` > `acceptEdits`」解析，只接受 SDK 白名单内的值。CLI 执行器与 SDK harness 共用同一函数，结果经 `context_pack["permission_mode"]` 传入 `ClaudeAgentOptions`。
- **为什么必须透传**：`acceptEdits` 只自动批准文件编辑与只读命令集，`node --test` / `npm` / `curl` 仍需审批；无人值守时这些调用直接被拒（官方 headless 文档）。隔离容器可显式配 `bypassPermissions`，并用 `TDT_PERMISSION_DENY_RULES` 配置作用域 deny 规则收紧危险命令（deny 在 bypass 下仍生效，默认留空）。
- **预检**：`TDT_PERMISSION_PREFLIGHT=true`（默认）时 worker 进程首个阶段跑一条解释器命令，失败只告警；`/api/health` 返回实际生效的 `permission_mode`，便于发现环境漏配。
- **失败归因**：阶段结束后从 `selftest_log.json`（`status=blocked`）、`test_report.json`（`verdict.not_a_code_defect` / `status=blocked`）与 provider 标记事后判定 `environment_blocked` / `auth_blocked`。这是**事后判定**，不做事前预检跳过任务。
- **止血**：`TDT_ENV_BLOCK_PAUSE_ENABLED=true`（默认）时环境类失败第 1 轮即置 `stopped`，不再按 `qa_passed=False` 打回 `eng_implement`；`TDT_MAX_CONSECUTIVE_SAME_FAILURE`（默认 3）对任意重复失败兜底。采用终态而非 `interrupt` 是因为并非每个阶段都有 HITL 门禁，修复执行环境后重新发起任务即可恢复。
- **沙箱落地（2026-11-10）**：`docker/claude-sandbox.json` 随 `runtime-harness` 镜像分发，`TDT_HARNESS_SETTINGS_FILE` 指向它即启用（文件不存在时自动跳过）。worker 容器需要 `cap_add: [SYS_ADMIN]` + `security_opt: [seccomp=unconfined, apparmor=unconfined]`；Ubuntu 24.04 的 `kernel.apparmor_restrict_unprivileged_userns=1` 下三者缺一都会失败。跨 harness 的公共档位放在 `context_pack["sandbox"]["mode"]`（`read_only|workspace_write|full_access`），dsh 映射到 `DSH_PERMISSION_MODE`，codex 映射到 `--sandbox` 并支持 `TDT_CODEX_ASK_FOR_APPROVAL` / `TDT_CODEX_NETWORK_ACCESS`。依赖只允许装进 workspace，全局安装由沙箱拒绝。

### 2.3 DSH 沙箱可用性自动回退（默认关闭）

背景：部分运行时（实测云端 linux wheel）缺 sandbox runner，`workspace-write` 下 bash 在命令启动前即被拒
（`SANDBOX_UNAVAILABLE` / `no usable backend`），`danger-full-access` 提权又因无审批通道失败。
`DSH_PERMISSION_MODE` 只在进程启动时解析、SDK 无热切换 RPC，因此回退实现为「探测 → 关运行时 →
以 `danger-full-access` 重拉该轮」，不是改运行中变量。

| 配置 | 默认 | 说明 |
|---|---|---|
| `TDT_DSH_SANDBOX_AUTO_FALLBACK` | `false` | 总开关；关闭时零探测、行为与之前完全一致 |
| `TDT_DSH_SANDBOX_PROBE_TTL_SECONDS` | `600` | 进程级缓存 TTL（键含主机/SDK 版本/初始档位/cwd）；0 = 每轮探测 |
| `TDT_DSH_SANDBOX_PROBE_TIMEOUT_SECONDS` | `30` | 探测轮超时；超时/异常一律**保守保持原档位**，不提权 |
| `TDT_DSH_SANDBOX_PROBE_PROMPT` | 固定 `echo __dsh_probe__` | 不注入任务内容、不挂 MCP/工具禁用、不写任务产物 |
| `TDT_DSH_SANDBOX_UNAVAILABLE_HINTS` | 内置集合 | 逗号分隔自定义特征词；识别 `tool/result` 与回复里的不可用文案 |

行为：任务已显式 `full_access` 时跳过探测；探测轮用独立 `dsh_home/_probe/<uuid>` 与独立会话，
跑完即删，绝不污染任务 `native_id`；命中不可用时该轮以 `danger-full-access` 执行，`reply.meta`
带 `permission_fallback` / `probe_reason` / `effective_mode` / `probe_cached`，并打 `WARNING` 记录
任务、阶段、会话与原因（不含密钥）。

**语义澄清**：`approval.policy='never'` 是「拒绝前不再询问」，不是自动放行；`workspace-write` 下
workspace 内的 bash 本来就无需审批，只有越界/提权才需审批且无人值守时 fail closed。本能力**不做
「沙箱内免审批」**，只在沙箱后端整体不可用时回退到无沙箱模式。

**安全提示**：外层统一 launcher（per-run mount namespace / env 白名单 / egress 代理）尚未落地时，
在公网多任务共享 `/data` 的容器里开启此开关等于放大权限面，属临时止血而非安全默认值。
按 `docs/plans/2026-11-10-dsh-sandbox-auto-fallback.md` §3，根治仍需外层隔离；关开关即回滚。

选择与锁定：

- 创建任务时选择（`CreateInitiativeRequest.harness`），默认
  `TDT_DEFAULT_HARNESS=dsh`；注册表校验失败抛 `UnknownHarnessError`。
- `/start` 时锁定（`Initiative.harness_locked=True`）；草稿期可经
  `PATCH /api/initiatives/{id}/harness` 更换。
- 运行中更换必须经 HITL 授权（`switch_harness` 动作）—— 图把新 harness 写入
  流水线状态并重跑/重入该阶段。

## 3. 阶段路由

任务状态携带非空 `harness` 时，`graph._executor_name()` 对**所有阶段**返回
`harness_stage`。`HarnessStageExecutor` 随后：

1. 解析并探测 provider（TTL 缓存）；
2. bootstrap 任务上下文包（滚动记忆 + 绑定文件 + RAG 简报）；
3. 在按阶段新建的会话上发送该轮对话（工作目录 = 任务 worktree）；
4. 会话记录写入任务记忆，超 token 预算自动总结；
5. provider 不可用且 `TDT_HARNESS_FALLBACK=true` 时降级到既有阶段执行器
   （mock / 编码 CLI）；
6. 始终产出各阶段契约要求的产物（`contracts.REQUIRED`）。

## 3.1 控制平面意图（跨 agent 能力）

真实 harness 的 agent loop 在 provider 进程里，跨阶段路由与升级必须由编排层执行。
执行器在提示词中下发协议，模型只在回复里声明意图，块从产物与记忆中剥离：

| 块 | 用途 | 兑现 |
|----|------|------|
| ` ```control {"handoff": {...}} ` | 交给某个角色 | 查 `ctx.next_targets`（plan 后继 ∩ 契约白名单 ∩ 非 HITL）→ `proposed_next` → 图条件边跳转；越界记 `rejected` 并回落默认边 |
| ` ```control {"escalate": {...}} ` | 需要拍板 | 先 `MissionManager.advise`；裁决不了进决策中心（钉钉/飞书）并挂起，回复后作为 `human_instruction` 重入，不提权 |
| ` ```subroom {...} ` | 拉小群讨论 | 经 `StageSessionHost.open_subroom` 立即开会，结论回注后二次调用 provider 收尾 |
| ` ```toolkit_intent {...} ` | 缺工具/Skill/记忆 | 编排层检索、上浮、执行脚本并落 `toolkit_meta.json` / `skill_runs.json` |
| ` ```qa {"passed": bool} ` | QA 结论 | `StageResult.qa_passed`；仅 `false` 打回，未声明按人工门禁批准继续 |

失败收尾与门禁语义：

- **降级自检**：阶段以 `status=fail` 收尾、且自审没有任何结论（lens 全 `skipped` / `failed`）时，
  图会带着自检指令重入本阶段，次数由 `TDT_STAGE_SELF_CHECK_ATTEMPTS` 控制（默认 1，0 关闭）。
  自检只要求补齐产物、对齐 `diff.patch`、跑通自测、如实勾选 checklist，不扩展需求；
  部署类阶段不重跑。这样残缺交付不会直接流到 QA 再被判一次 fail。
- **门禁推荐动作**：`qa_passed=false` 时 `pending_hitl.recommended_action` 给出
  `{action: "reroute", next_stage: on_fail, label: "返工"}`，工作台据此把主按钮换成「返工」并说明原因；
  仍保留「仍要放行」（approve，同样会被打回）与「停止」。避免出现「点了批准却回到开发」的误解。

`run_session(ctx, host)` 才会注入小群协议；`run(ctx)` 保持单发语义。无 host 时
升级仍返回 `needs_hitl`（落到工作台门禁），不静默继续。

验收：`scripts/verify_collab.py`（mock 剧本化回归 + 真机精简航线），报告落
`data/verify/<run_id>/`。

## 4. 需求绑定记忆

`data/missions/<initiative_id>/memory/` 目录约定：

```text
memory.md                 滚动总结（自动压缩，LLM 回写）
sessions/<session>.jsonl  每个 harness 会话一份转录
context/*.md              需求绑定文件（brief.md / plan.md / harness.md 等）
```

新会话经 `bootstrap_context()` 自动加载全部内容；上下文包受
`TDT_MISSION_CONTEXT_TOKEN_LIMIT` 预算约束，超限时滚动记忆经
`LlmClient.chat_text` 总结后回写。

## 5. 共享数据层

`TDT_MEMORY_BACKEND=mem0` 时本地存储被包装为 `CompositeMemory`：mission 范围的
写入同步镜像到 mem0（`TDT_MEM0_BASE_URL` / `TDT_MEM0_API_KEY`），检索命中并入
上下文块 —— 以 `user_id = "<prefix>:<initiative_id>"` 做任务级隔离。RAG 检索
（`knowledge_gateway.retrieve`）结果同样注入 harness 提示。所有远端调用失败均
静默降级到本地。
