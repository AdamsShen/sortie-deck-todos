# Harness 子 agent 并行：可行性调研与接入方案

- 日期：2026-11-05
- 状态：待确认（未改代码）
- 关联：[2026-10-26-collab-harness-e2e-verification.md](./2026-10-26-collab-harness-e2e-verification.md)、[2026-11-05-stage-boundary-and-upstream.md](./2026-11-05-stage-boundary-and-upstream.md)

## 1. 需求

三个 CLI harness（Claude Code / Codex / OpenCode）在执行过程中都能自行决定派生子 agent 并行干活。要回答两件事：

1. 这些能力能否接进本项目的 harness 层（可观测、可控、可预算）？
2. 业界云平台是怎么实现「agent 派生子 agent 并行」的，有哪些共性可以借鉴？

## 2. 现状（本项目，已核实）

### 2.1 执行模型

- 一个阶段 = 一个 provider 会话，唯一创建点在 `src/sortie_deck/harness_providers/executor.py:949`。
- 多 agent 只发生在阶段之间：`graph.py:2470` 的并行波次用 `asyncio.gather` 并发跑分支，每个分支仍是独立 stage + 独立会话。
- 阶段内的小群（`executor.py:1446` → `subroom.py:353`）由 `orchestrator.py:1929 _discussion_reply` 单次 LLM 调用回答，不是 harness 子 agent。
- 多镜头自审（`executor.py:1156`）在同一会话追加轮次。

### 2.2 三家 harness 的实际状态

| Harness | 上游是否支持子 agent | 本项目是否开启 | 本项目是否观测 |
|---|---|---|---|
| Claude Code | 支持。内置 `Agent`（v2.1.63 前叫 `Task`）工具；自定义 agent 放 `.claude/agents/*.md`；并发默认 20、嵌套默认 3 层 | 隐式开启：`claude_code.py:177-192` 未传 `agents`、未限制工具；`setting_sources` 默认加载本机 `~/.claude/agents/*` | **否**。`TaskStartedMessage` / `TaskProgressMessage` / `TaskNotificationMessage` / `TaskUpdatedMessage` 都是 `SystemMessage` 子类，被 `claude_code.py:236-248` 的 `SystemMessage` 分支丢弃（只处理 `compact_boundary`） |
| Codex CLI | 支持。本机 `codex features list` 显示 `multi_agent stable true`；自定义 agent 为 TOML（`~/.codex/agents/`、`.codex/agents/`）；`config.toml [agents]` 有并发上限 | 隐式开启（feature 默认 true）；本机无 `~/.codex/agents/`，也没设 `[agents]` | **否**。`codex.py:178-231 _parse_jsonl` 只认 `thread.started` / `item.completed(agent_message)` / `turn.completed`，其余事件被忽略 |
| OpenCode | 支持。`mode: subagent` + 子会话，`permission.task` 控制可派生的名单 | 取决于部署侧 `opencode.json`，本项目未下发任何 agent 配置 | **否**。`opencode.py:132-144` 只取 `parts` 中 `type=text`，`task` 工具调用与子会话全丢 |

### 2.3 实测：模型确实会自己用

扫描本项目 62 个 transcript 目录、857 份阶段转录、7884 次工具调用：

```
Bash 4495 | Read 1439 | Edit 1264 | Write 666 | Skill 9 | Agent 4
```

4 次 `Agent` 调用：跑贪吃蛇测试套件、跑后端单测、抽设计/清单片段（`Explore`）、审 Kotlin 模块（`kotlin-reviewer`）。对应 4 个 `~/.claude/projects/<cwd>/<session>/subagents/` 目录。这些子 agent 的 token、耗时、产物**全部不进 `stage.meta`**，属于影子执行。

结论：能力已经默认打开，缺的是**观测**与**控制**，不是「能不能接」。

## 3. 业内调研

### 3.1 三个 CLI harness 的机制

| 维度 | Claude Code | Codex CLI | OpenCode |
|---|---|---|---|
| 定义载体 | Markdown + YAML frontmatter，`.claude/agents/`、`~/.claude/agents/`、`--agents` JSON | 独立 TOML：`~/.codex/agents/`、`.codex/agents/`；必填 `name`/`description`/`developer_instructions` | `opencode.json` 的 `agent` map 或 `agents/*.md`；`mode: primary\|subagent\|all` |
| 派发方式 | 模型按 `description` 自主调用 `Agent` 工具；支持 `@agent-name` 强制 | 模型按 description 调用；内置 `default`/`worker`/`explorer`；Responses API 另有 `spawn_agent` 等 6 个原语 | 模型自动调用、`@` 提及；底层是 `task` 工具，`permission.task` 可 deny |
| 并行 | 默认后台并行；并发上限默认 20（`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`）；嵌套默认 3 层 | `[agents].max_concurrent_threads_per_session`；Responses API 侧默认并发 3 | General subagent 官方描述即「并行执行多个工作单元」；同时多子会话是支持形态 |
| 上下文 | 非 fork 子 agent 全新上下文，只回摘要；细节留在 `subagents/agent-*.jsonl` | 子线程独立，root 汇总 | 子会话独立，父会话可见返回 |
| 限制 | 主线程才支持 `Agent(worker,researcher)` 白名单；`permissions.deny: ["Agent(Explore)"]` 可禁用；`CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` | `--disable multi_agent` 可关；`--enable` 可开 | `permission.task: {"*":"deny", "x":"allow"}`；`hidden` 只影响菜单不影响调用 |
| 观测 | SDK 暴露 `Task*` 四类消息、`SubagentStart/Stop` hooks、每个子 agent 独立 transcript | `exec --json` 的 item 事件（子 agent 事件类型待实测）；文章称 CLI 无子 agent 级成本统计 | 子会话可导航；HTTP 侧有子会话列表（端点待核实） |

### 3.2 云平台的做法

| 平台 | 隔离单位 | 编排方式 | 关键机制 |
|---|---|---|---|
| AWS Bedrock AgentCore | **每个 session 一台 Firecracker microVM** | 应用层每 agent 调用一次 Runtime；harness 内置 `task`/`bash`/`cron` 在沙箱内跑子 agent | 物理隔离（独立内核/文件系统/端口）；`/mnt/workspace` 持久、session id 即恢复句柄（14 天）；最多 5 个挂载点；OTel→CloudWatch + CloudTrail；按实际 CPU 计费 |
| Azure AI Foundry（connected agents, classic） | 子 agent 是独立已发布 agent，独立 Agent Identity | **把子 agent 包成 tool**（id+name+description）放进主 agent tools，模型像调函数一样委派 | 单 thread、子 agent 过程对用户不可见、嵌套上限 2、不保证并行；确定性并行要改用 workflows；classic 2027-03-31 退役 |
| Google ADK / Vertex AI Agent Engine | 同一 session，共享 `session.state` | `sub_agents` 层级；`SequentialAgent`/`ParallelAgent`/`LoopAgent` 为确定性编排；Collaborative workflow 为 LLM 协调 | Parallel 分支各写不同 `output_key` 避免竞态；大对象走 `ArtifactService`，不自动传递、需用 filename 显式衔接 |
| 阿里云百炼 | 平台托管 | `multiagent` 字段配 coordinator 拓扑（1–20 个成员）；工作流里的 Agent Group 节点由决策模型挑选子 agent | 子 agent 各有 system prompt 与最小工具集；组内可并行处理子任务；TeamHarness 提供协作规则 |
| OpenAI Responses API | 子 agent 为同级 thread | `multi_agent.enabled`：`spawn_agent` / `send_message` / `followup_task` / `wait_agent` / `interrupt_agent` / `list_agents`，用 `/root/researcher` 层级寻址 | 默认并发 3；写入集中在 root；结构化输出后由 root 归并 |

### 3.3 共性归纳

1. **控制权两分**：模型自主委派（tool/Agent/task/spawn_agent）用于开放式任务；确定性并行（ADK ParallelAgent、百炼 Agent Group、Azure workflows）用于可预定义 fan-out。生产系统通常两者并存，而不是二选一。
2. **隔离单位是「子会话」而非「同一上下文里的角色」**：Claude/OpenCode/Codex/Azure 都是子会话或子线程，AgentCore 升级为独立 microVM。
3. **只回摘要**：子 agent 的完整过程留在自己的 transcript，父 agent 只拿结论——这是「省主上下文」的立论基础。
4. **并发与深度都有硬上限**，且默认值保守（Claude 20/3 层，Codex 3，Azure 2 层）。
5. **写操作收敛到父**：多方实践都建议子 agent 只读/只产出建议，避免同文件写竞争。
6. **观测是一等公民**：子 transcript 独立落盘 + 生命周期 hooks/事件 + 平台级 trace + token 按 agent 归属。
7. **路由质量由 `description` 决定**：Azure 的 name/description、OpenCode 的 description、Claude 的 description 都是唯一的路由信号。
8. **长任务靠 session id + 持久文件系统恢复**，不靠进程常驻。

## 4. 拟采用方案（分三档，建议先 A 后 B）

### A. 观测接入（不改行为，约 1–2 天）

目标：模型自发的子 agent 在任务页可见、进 meta、能统计，但**不改变是否派生**。

「不改行为」的确切含义：

- **不改变派生决策**：模型今天会不会用子 agent、用几个、用什么类型，改完完全一样，仍由 harness 自己决定。
- **不改变权限**：子 agent 今天能写文件（`bypassPermissions` / `acceptEdits` 继承），改完照样能写。A 只加「仪表盘」，不加「刹车」。
- **不等于只读**：只读是 B 里的 `readonly` 档位，和 A 无关。A 阶段子 agent 仍然可能写文件，风险与今天一致（今天已经在发生，只是没人看得见）。

一句话：A 是「先把影子执行照亮」，B 才是「决定要不要限权、限多少」。

1. `src/sortie_deck/harness_providers/base.py`
   - `HarnessReply.meta` 增加统一结构 `subagents: list[dict]`，字段：`id` / `name` / `type` / `status` / `started_at` / `ended_at` / `turns` / `tokens` / `summary` / `transcript`。
2. `claude_code.py`
   - 在 `SystemMessage` 分支之前识别 `TaskStartedMessage` / `TaskProgressMessage` / `TaskNotificationMessage` / `TaskUpdatedMessage`（`claude_agent_sdk/types.py:1194/1211/1229/1254`），累积进 `meta["subagents"]`，同时通过 `on_activity("subagent", "<type>:<desc>")` 推进度。
3. `codex.py`
   - 先跑一次带子 agent 的 `codex exec --json` 抓真实事件类型（`item.type` 可能新增 `agent`/`collab` 类），在 `_parse_jsonl` 里做 passthrough：未知 item 收进 `meta["subagent_events_raw"]`，已识别的结构化进 `subagents`。
4. `opencode.py`
   - 解析消息 `parts` 中 `type=tool` 且 `tool=task` 的调用/结果；若服务端支持子会话列表，按 `parentID` 拉取（端点待核实，缺失则只保留 task 调用记录）。
5. `executor.py`
   - `_activity_callback`（`executor.py:599`）支持 `kind="subagent"`；阶段收尾把 `reply.meta["subagents"]` 写入 `stage.meta["subagents"]` 并汇总数量/耗时。
   - 可选：把 `~/.claude/projects/<cwd>/<session>/subagents/*.jsonl` 复制到 `logs/<stage>/subagents/`，随任务归档。
6. 前端 `apps/web/src/types.ts` + 节点详情：显示「子 agent ×N」与每个的名称/状态/耗时。

验收：跑一次 `design_ui`（历史上有自发 Agent 调用），`stage.meta.subagents` 至少 1 条；未派生时为 `[]` 而不是缺字段。

### B. 控制面（约 2–3 天）

目标：按阶段决定「能不能派生、派生多少、用哪个模型」。

1. `settings.py` 新增：
   - `subagent_mode: str = "readonly"`（`off` / `readonly` / `on`）
   - `subagent_max: int = 3`
   - `subagent_model: str = ""`（空表示继承主会话）
   - `subagent_stage_policy: str = ""`（`stage=mode` 逗号分隔，覆盖全局）
2. `claude_code.py`
   - `off`：`disallowed_tools=["Agent"]`（硬禁用）；
   - 注入 `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`、`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`、`CLAUDE_CODE_SUBAGENT_MODEL`（需要强制统一时再加 `_FORCE=1`）；
   - `readonly`：**注意这是软约束**。Claude 官方规则是「主会话为 `bypassPermissions` / `acceptEdits` / `auto` 时，子 agent 强制同模式、忽略其 `permissionMode`」，所以我们没法用权限模式把子 agent 锁成只读。可用的硬手段只有两个：
     a) `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` 关掉内置 `general-purpose` 等类型，再用 `agents=` 只注册 `tools: [Read, Grep, Glob]` 的只读子 agent；
     b) 用 `PreToolUse` hook 拦子 agent 的 `Write/Edit`（settings 级 hook 对子 agent 内部工具调用同样生效）。
     实现上建议 a + b 一起用，否则模型仍可能派生可写子 agent。
3. `codex.py`
   - `off`：`--disable multi_agent`（硬禁用）；
   - 并发：`-c agents.max_concurrent_threads_per_session=<n>`；
   - `readonly`：TOML 里 `sandbox_mode="read-only"` 是**子 agent 级沙箱，属硬约束**（部署镜像预置 `.codex/agents/*.toml`）。
4. `opencode.py`
   - `off`：`permission.task: {"*":"deny"}`（硬禁用）；
   - `readonly`：**硬约束**——子 agent 配置里 `permission.edit=deny`、`permission.bash` 按需收敛，再配合 `permission.task` 白名单。
5. 预算联动：`subagent_max` 与 `stage_max_turns` 一起进 meta，超限时只记警告不改行为（避免误杀）。

**`readonly` 三家可执行性对照**：

| Harness | 只读可硬保证？ | 依赖手段 |
|---|---|---|
| Claude Code | 需组合手段（默认软） | 禁内置 agent + 只注册只读 `agents=` + PreToolUse hook；否则 `bypassPermissions` 会继承给子 agent |
| Codex CLI | 是 | 子 agent TOML 的 `sandbox_mode="read-only"` |
| OpenCode | 是 | agent 级 `permission.edit=deny` + `permission.task` 白名单 |

默认建议：文档型阶段（intake / product_prd / design_ui / qa_cases）`off`；编码与自测阶段 `readonly`。

### C. 显式编排（暂不做）

用现有并行波次跑**独立 harness 会话**（每子任务独立 cwd/artifacts、独立重试、独立计费），而不是靠 harness 内部子 agent。优点是跨 harness 一致、产物可收、失败可重跑；代价是要新增子任务契约与结果合并规则。等 A/B 跑出数据后再评估。

## 5. 备选方案

| 方案 | 取舍 |
|---|---|
| 只在提示词里禁用子 agent | 一行改动，但只是「请求」不是「约束」，模型仍可派生；且失去并行收益 |
| 直接上 C（全部用编排层 fan-out） | 一致性最好，但改造面大，且丢掉了 harness 内建的上下文隔离与工具循环 |
| 只做 A 不做 B | 成本与写冲突不可控（Claude 并发 20），不建议长期 |

## 6. 风险

1. 子 agent 的产物落在 provider 自己的 transcript，`_artifacts` 收不到——需要在 A 里显式归档，否则「审校入口」缺失。
2. 成本不可见：Claude/Codex 的子 agent token 与主会话合并计费，B 的 `subagent_max` 只能限并发，不能限总量。
3. 写冲突：多个子 agent 同改一个文件，父 agent 只看到摘要，冲突难以事后发现——因此默认只读。
4. Codex/OpenCode 的子 agent 事件格式未实测，A 的字段映射可能需要二次调整。

## 7. 验证

- 单测：fake provider 注入 `TaskStarted/Progress/Notification` → 断言 `meta["subagents"]`；`subagent_mode=off` 时 provider 收到禁用参数。
- 实跑：`design_ui`（Claude）、`eng_web_selftest`（Codex）、OpenCode 各一次，确认页面能看到子 agent。
- 回归：`pytest tests/ -q --ignore=tests/test_llm_chain.py`。

## 8. 待确认

1. 先做 **A**（只观测）还是 **A + B**（观测 + 开关）？
2. 默认策略选哪个：全阶段 `off`（最省）、文档阶段 `off` + 编码阶段 `readonly`（推荐）、还是全阶段 `readonly`？
3. 是否需要把子 agent 的 transcript 复制进 `logs/<stage>/subagents/`（会增加归档体积，但排障与审校需要）？
4. Codex 侧是否允许在部署镜像里预置 `.codex/agents/*.toml`（需要在 Dockerfile 改一行）？

## 9. 来源

- Claude Code 子 agent 官方文档：https://code.claude.com/docs/en/sub-agents
- Claude Code 子 agent 使用指南：https://claude.com/blog/subagents-in-claude-code
- Claude Agent SDK 类型（本机）：`.venv/lib/python3.12/site-packages/claude_agent_sdk/types.py:1194-1254`
- OpenCode Agents 官方文档：https://opencode.ai/docs/agents/
- OpenAI Codex 多 agent 概念页：https://developers.openai.com/codex/concepts/multi-agents （直连 403，内容以搜索摘要与第三方整理为准）
- Codex 子 agent 实战整理：https://agentscodex.com/posts/2026-10-09-openai-codex-subagents-parallel-multi-agent-workflows
- AWS AgentCore 托管编码 agent：https://aws.amazon.com/blogs/machine-learning/its-safe-to-close-your-laptop-now-hosting-coding-agents-on-amazon-bedrock-agentcore
- Azure AI Foundry connected agents：https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/connected-agents
- Google ADK 工作流：https://adk.dev/workflows/
- 阿里云百炼多智能体：https://help.aliyun.com/zh/model-studio/managed-agents-multiagent-proposal
- 阿里云百炼 Agent Group 节点：https://help.aliyun.com/zh/model-studio/workflow/agent-group-node
