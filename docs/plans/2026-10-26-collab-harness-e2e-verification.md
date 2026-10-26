# 多人协作 + Agent Harness 真机验收方案

> 状态：**G1–G5 已实施，验收脚本已落地**（`scripts/verify_collab.py`；mock 全绿，见 §10）
> 日期：2026-10-26
> 范围：`src/sortie_deck/harness_providers/`、`graph.py`、`pipeline_llm.py`、`mission_briefing.py`、`orchestrator.py`、验收脚本与测试。

## 1. 需求与验收点

用户要求验证：任务在 **Claude Code / Codex 等 agent 环境**里正常完成多人协作，且具备：

| # | 能力 | 可观测验收标准 |
|---|------|----------------|
| C1 | 在真实 harness 中完成任务 | 任务锁定 `harness=claude_code|codex`，各阶段由该 harness 产出 `reply.md` 与契约产物，任务到达 `done` |
| C2 | Agent 自主决定和谁沟通 | 执行 agent 声明目标角色 → 图跳到该 plan 节点（`handoff accepted` 事件）；越界目标被拒并回落默认边；HITL 门禁不可绕过 |
| C3 | 需要时拉小群讨论 | 小群记录独立（不回流大群），结论以 `stage:<stage>` 记忆定向注入发起阶段；深度上限 1；预算生效 |
| C4 | Manager 推进流程 | 并行波汇合产生 `trigger=join` 决策并决定下一节点；分支失败时 reroute；无法决策时升级真人 |
| C5 | 合适的时机加载 skill / mcp / tool / 知识库 / memory | 阶段入口主动召回注入 brief；回复内 `toolkit_search → toolkit_pick → skill_run / mcp_calls` 真实兑现并落 `toolkit_meta.json` / `skill_runs.json`；KB 命中进入 prompt；`memory_search/read` 有预算与审计 |
| C6 | 需要真人时通过钉钉等确认 | Agent 声明升级 → 决策中心生成 `decision_id` → 钉钉机器人收到脱敏消息（签名 URL）→ 任务暂停 → 回复后恢复 |
| C7 | 角色不足时自主创建带审核角色 | 创建任务时 loadout 发现目录缺角色 → 生成 `pending-review` forge 草稿与未发布 agent → 审核通过后注册角色并允许启动 |

## 2. 现状核验（代码 + 实测）

### 2.1 已具备并能通过测试

| 能力 | 实现位置 | 现有测试 |
|------|----------|----------|
| 自主 handoff / 条件边路由 | `src/sortie_deck/graph.py:814`、`:829`、`:838` | `tests/test_handoff_join.py` |
| Manager 汇合/冲突/升级 | `src/sortie_deck/manager.py:152`、`:221`、`:240` | `tests/test_handoff_join.py` |
| 小群 | `src/sortie_deck/subroom.py`、`graph.py:1025` | `tests/test_subroom.py` |
| 按需加载 | `src/sortie_deck/loadout.py`、`graph.py:1324` | `tests/test_loadout*.py` |
| 真人决策中心 | `src/sortie_deck/decisions.py`、`orchestrator.py:1924` | `tests/test_decisions.py`、`tests/test_im_escalation.py` |
| 钉钉出站签名 | `src/sortie_deck/notify.py:51` | `tests/test_decisions.py` |
| Forge 草稿/审核/注册 | `src/sortie_deck/agent_forge.py:164`、`:293` | `tests/test_roles_forge.py` |

本地基线：协作相关 8 个测试文件 **152 passed**（2026-10-26 实跑）。

### 2.2 真机探测结果

| Provider | 探测 | 结论 |
|----------|------|------|
| `claude_code` | `probe=ok`；`send()` 实测返回 `可用` | 真实可用（本地 Claude 凭据） |
| `codex` | CLI 实测 `codex exec --json` 返回 `OK`；但 provider `probe=auth_required` | **误判 + 解析失效**（见 G3） |
| `opencode` | `Connection refused`（未部署服务） | 需部署 `opencode serve` 才能验收 |
| `cursor` | `auth_required` | 需 `cursor-agent login` 或 `CURSOR_API_KEY` |
| 钉钉 | 未配置 webhook | 验收用本地 HTTP sink，或用户提供真实机器人 |

### 2.3 阻塞验收的缺口

**G1（P0）真实 harness 无法自主 handoff。**
`harness_providers/executor.py:331` 只对编码角色硬编码 `proposed_next="qa_verify"`，其余角色恒为 `None`；提示词里也没有下发 `handoff_targets`。因此 C2 在真实 harness 下不可达。

**G2（P0）真实 harness 无法升级真人。**
`StageSessionHost.escalate` 已存在（`subroom.py:542` → `graph.py:1025` → `orchestrator.py:1924`），但没有任何执行器调用它，harness 提示词也没有升级协议。因此 C6 只能靠预先配置的 `hitl_after` 门禁，agent 自己判断不了。

**G3（P0）Codex provider 误判与解析失效。**
- `codex.py:37` 只认环境变量，忽略已存在的 `~/.codex/auth.json` → `auth_required`。
- `codex.py:122` 只解析旧版 `msg.type=agent_message`；实际输出是 `{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}` → 文本为空 → 阶段 `fail`。
- 未指定沙箱（编码阶段默认只读）、未关闭 stdin 继承（服务端可能读到悬空 stdin）。

**G4（P1）"角色不足自动 forge" 实际不可达。**
`pipeline_llm.py:159` 把 loadout 结果过滤为「注册表已知角色」，`_LOADOUT_SYSTEM` 还明确要求 "Never invent a role id"；而 `mission_briefing.py:683` 的 forge 分支只在 `not registry.is_known(slot)` 时触发。两者互斥 → 创建任务时永远不会产生 forge 草稿，只能手工 `POST /api/forge/drafts`。

**G5（P1）升级回复会走错权限分支。**
`graph.py:1632` 把 `needs_hitl` 统一当权限阻断：`EDIT_INSTRUCTION` 会注入 `bypassPermissions` 并置 `publish_pending`。真人回答升级问题时不应提权。

## 3. 业内调研

| 方案 | 做法 | 对本项目的启示 |
|------|------|----------------|
| OpenAI Agents SDK | handoff 被建模为「工具调用」，由运行时接管目标 agent 与会话（[说明](https://engineering.zooz.com/@dikshant.nagar.mec19/understanding-handoffs-in-openai-agents-sdk-b80cbf889450)、[概念](https://buildwithaitoday.com/wiki/openai-agents-sdk)） | 模型只产出**结构化控制意图**，切换由编排层执行并校验 |
| LangGraph | agent 节点返回 `Command(goto=...)` 做 handoff；`interrupt()` + `Command(resume=...)` 做人在环（[多代理](http://langgraph.com.cn/concepts/multi_agent.1.html)、[HITL](https://langgraph.com.cn/tutorials/get-started/4-human-in-the-loop/index.html)） | 与本仓一致：路由是图跳转；控制流与状态更新同源 |
| Claude Agent SDK | 权限由 `canUseTool` / 权限模式 / hooks 拦截；子 agent 继承顶层权限（[permissions](https://platform.claude.com/docs/en/agent-sdk/permissions)、[HITL 实践](https://nerdleveltech.com/en/human-in-the-loop-claude-agent-sdk-typescript-tutorial)） | harness 内部工具权限由 SDK 管；**跨阶段路由/升级**必须由外层编排层接管 |
| MCP | `elicitation/create` 让工具在运行中向用户要输入（[说明](https://www.mcp-framework.com/docs/tools/elicitation)） | 「结构化请求 + 客户端收集答案」是跨厂商共识 |

**共性结论**：agent harness 负责单次会话内的工具循环；**跨 agent 的交接、子群讨论、升级真人必须是编排层的控制平面能力**，模型只输出类型化的控制请求。本仓已有的 `toolkit_intent` / `subroom` 块正是这个范式，缺口只是没覆盖 handoff 与 escalate。

## 4. 拟采用设计

### 4.1 统一的「控制意图块」（补充现有声明-兑现范式）

在 harness 提示词中新增一段协议，模型可在回复里写一个 ```` ```control ```` 围栏块（与 `toolkit_intent` / `subroom` 并列）：

```json
{"handoff": {"role": "qa", "reason": "实现已完成，需要测试"}}
{"escalate": {"question": "两个验收口径冲突，选哪个？", "options": ["A", "B"], "assignee": "qa_owner"}}
```

执行侧（`harness_providers/executor.py`）：

1. `_parse_control_intent()` 解析；解析失败/无块 → 维持现状（零回归）。
2. `handoff`：用新增的 `StageContext.next_targets`（`[{stage, role, label}]`，由图按「plan 节点 ∩ 契约白名单 ∩ 非 HITL 节点」生成）把角色解析成阶段名，写入 `proposed_next`；解析不到 → 不写，落 `meta.control_intent.rejected` 审计。
3. `escalate`：调用 `host.escalate(question)`（复用现有决策中心），并把结果转成 `StageResult(status="needs_hitl", meta={"kind": "escalation", ...})`，让图真正挂起等待；无 host / IM 未配置时仍返回 `needs_hitl`，降级为工作台门禁而不是静默继续。
4. 产物与记忆里剥离控制块，避免 JSON 污染。

图侧（`graph.py`）：

- `run_stage` 组装 `ctx.next_targets`；`_apply_handoff` 保持现有二次校验（契约白名单 + 预算 + 不得跳 `done` + 不得绕 HITL）。
- `needs_hitl` 分支按 `meta.kind == "escalation"` 走独立路径：真人回答作为 `human_instruction` 重入本阶段，**不注入 bypassPermissions、不置 publish_pending**（修 G5）。

设计取舍：
- 模型输出**角色**而不是阶段名（契约本来就是角色白名单，模型也更懂角色）。
- 若同一角色对应多个节点，取「当前节点之后最近的一个」，并在提示词里列出可选目标，减少歧义。
- 控制块与 `toolkit_intent` 分离，避免把路由意图混进能力申请。

### 4.2 任务创建时的自主 forge（修 G4）

- `pipeline_llm.propose_loadout_llm` 的提示词与返回增加 `missing_slots`（最多 2 个，附 `capability` / `why`）；`needed_slots` 仍严格限定目录内角色（安全边界不变）。
- 校验：snake_case 规范化；与已知角色同名/高相似（复用 `RoleRegistry.find_similar`）→ 折叠为已知角色；否则保留。
- `mission_briefing._tool_loadout` 把 `missing_slots` 一并送回；`ensure_missing_roles` 对未知 slot 走 `AgentForge.propose` → `pending-review` 草稿 + 未发布 agent（现有逻辑）。
- 新增 `TDT_FORGE_REQUIRE_REVIEW=true`（默认开）：存在待审 forge 草稿时 `/start` 返回 409 `forge_review_required`；审核通过（现有 `/api/forge/drafts/{id}/approve`）后注册角色并放行。这样「自主创建」与「必须审核」同时成立。
- 确认页文案展示草稿 `issues` / 相似角色，审核人即任务确认人。

### 4.3 Codex provider 修复（修 G3）

- `probe`：`OPENAI_API_KEY` / `CODEX_API_KEY` 或 `~/.codex/auth.json`（支持 `CODEX_HOME`）。
- `send`：`stdin=DEVNULL`；`--sandbox` 取 `TDT_CODEX_SANDBOX`（默认 `workspace-write`）；保留 `--skip-git-repo-check`。
- 解析：兼容 `{"type":"item.completed","item":{"type":"agent_message","text":...}}`、`thread.started.thread_id`、`turn.completed.usage`；旧格式继续支持；`item.type=error` 记录告警但不清空正文。

### 4.4 验收脚本（新增 `scripts/verify_collab.py`）

一键跑真机验收，产物写入 `data/verify/<run_id>/`：

```
report.json / report.md      # 每个 C1–C7 的 pass/fail、关键事件、产物路径
dingtalk_sink.jsonl          # 本地 sink 收到的原始请求（含 timestamp/sign）
initiative.json              # 任务快照（状态、timeline、pending_hitl）
```

模式：
- `--harness claude_code`（默认）/ `codex` / `mock`
- `--scenario all|handoff|subroom|manager|loadout|escalate|forge`
- 使用临时 `TDT_DATA_DIR`，不污染现有 `data/`
- 场景内自建：本地钉钉 sink、toolkit 样例（tool/mcp/skill）、知识条目、记忆条目
- 真实 harness 场景给出「按提示词触发意图」的固定 prompt，断言事件与产物；`--harness mock` 用于 CI 快速回归

## 5. 文件与改动范围

| 文件 | 改动 |
|------|------|
| `src/sortie_deck/models.py` | `StageContext.next_targets`（可选字段） |
| `src/sortie_deck/graph.py` | 组装 `next_targets`；`needs_hitl` 增加 escalation 分支 |
| `src/sortie_deck/harness_providers/executor.py` | 控制意图协议、解析、handoff 映射、escalate 调用、提示词与产物剥离 |
| `src/sortie_deck/harness_providers/codex.py` | probe / 沙箱 / stdin / JSONL 解析 |
| `src/sortie_deck/pipeline_llm.py` | loadout `missing_slots` |
| `src/sortie_deck/mission_briefing.py` | 合并 `missing_slots`；确认页展示待审草稿 |
| `src/sortie_deck/orchestrator.py` | `/start` 前 forge 审核门禁（可配置） |
| `src/sortie_deck/settings.py` | `forge_require_review`、`codex_sandbox` |
| `scripts/verify_collab.py` | 新增验收脚本 |
| `tests/test_harness_control.py` | 新增：控制块解析、handoff 映射、escalation 挂起、块剥离 |
| `tests/test_codex_provider.py` | 新增：真实 JSONL 样例解析、凭据探测 |
| `tests/test_roles_forge.py` / `tests/test_loadout.py` | 补 `missing_slots` 触发与去重、审核门禁 |
| `docs/zh/modules/harness.md`、`docs/zh/modules/collaboration.md`、`CHANGELOG.md` | 行为与开关同步 |
| `docs/plans/2026-10-26-collab-harness-e2e-verification.md` | 本文档 |

## 6. 验证矩阵

| 场景 | 输入 | 断言 |
|------|------|------|
| S1 harness 基础 | 3 阶段任务，`harness=claude_code` | 每阶段 `reply.md` + 契约产物；`meta.harness=claude_code`；任务 `done` |
| S2 handoff | 提示词要求「把下一棒交给 qa」 | `handoff=accepted` 且 `to=qa`；被跳过的节点未执行；越界目标 `denied` 且回落 |
| S3 小群 | 提示词要求「先和 qa、design 拉小群确认边界」 | `subrooms` 1 条、transcript 非空、大群无记录、结论进入 `stage` 记忆、深度=1 |
| S4 manager | 两分支并行波 | `trigger=join` 决策存在；全成功 → 下一节点；一分支失败 → reroute |
| S5 loadout | 提示词要求检索并运行 skill、调 MCP、查 memory | `toolkit_meta.json` 含 search/pick/skill_run/mcp_calls；`skill_runs.json`；KB brief 命中；memory 读取计入预算 |
| S6 escalate | 提示词要求「需要真人拍板」 | 决策中心 1 条 pending；本地 sink 收到钉钉 payload 且带 `timestamp`/`sign`；任务 `waiting_hitl`；回答后恢复到下一节点 |
| S7 forge | brief 提出目录外能力（如「宠物殡葬策划」） | 生成 `pending-review` 草稿；未审核时 `/start` 409；approve 后角色注册、任务可启动 |
| S8 降级 | `harness=codex` 未登录 | probe 明确 `auth_required`；`TDT_HARNESS_FALLBACK=true` 时回落旧执行器并记录 `harness_fallback` |

## 7. 备选方案

1. **不改协议，用 SDK 原生能力**：Claude Agent SDK 的 subagent / hooks 只能在单会话内路由，无法驱动 LangGraph 的跨阶段节点与 HITL；且 Codex/OpenCode/Cursor 没有统一等价物 → 否决。
2. **让模型直接输出阶段名**：省掉 `next_targets` 映射，但模型看不到 plan 节点，容易编造 → 仅在提示词里给全量节点列表时可行，维护成本更高。
3. **forge 不阻断启动**（沿用当前未发布 agent 直接跑）：实现最省事，但与「带审核」冲突 → 作为 `TDT_FORGE_REQUIRE_REVIEW=false` 的可选降级。

## 8. 风险与限制

- 真实 harness 的 token/时延成本高：验收脚本默认只跑 3–4 阶段的精简航线，`mock` 模式用于回归。
- 钉钉真实群需要用户提供 webhook + secret；未提供时本地 sink 只能证明「出站格式/签名/幂等」，不能证明「真人收到」。
- Codex 的沙箱与登录态依赖宿主环境；CI 中若无凭据应显式 skip 而非 fail。
- 真实模型是否稳定遵守控制块协议有不确定性：协议里给最小示例，解析失败一律回落默认边（绝不阻塞任务）。

## 9. 实施顺序

1. P0：G3（codex provider）→ G1/G2/G5（控制意图 + escalation 分支）→ 单测。
2. P0：G4（loadout `missing_slots` + forge 审核门禁）→ 单测。
3. P1：`scripts/verify_collab.py` + S1–S8；`--harness mock` 全绿后跑 `claude_code` 真机，最后跑 `codex`。
4. P1：文档与 CHANGELOG；产出 `data/verify/<run_id>/report.md` 作为验收证据。

## 10. 实施记录

| 项 | 结果 | 位置 |
|----|------|------|
| G1/G2/G5 控制意图 + escalation 分支 | 完成 | `harness_providers/executor.py`、`graph.py`、`models.py` |
| G3 Codex provider | 完成：登录态探测、`--sandbox`、`stdin=DEVNULL`、新旧 JSONL 解析、`needs_auth` | `harness_providers/codex.py`、`tests/test_codex_provider.py` |
| G4 自主 forge | 完成：`missing_slots` 校验/去重、编制合并、`forge_drafts` 写入 meta、`/start` 409 门禁 | `pipeline_llm.py`、`mission_briefing.py`、`orchestrator.py`、`api/app.py`、`tests/test_forge_gate.py` |
| 验收脚本 | 完成：`basic/handoff/subroom/loadout/escalate/forge/manager` + probe | `scripts/verify_collab.py` |

实施中新发现并修复的两个问题（原方案未覆盖）：

1. **QA 门禁回环**：`graph.hitl_gate` 把「未声明 qa_passed」当失败，真实 harness 批准后
   会无限回到 build。现改为只有 `qa_passed is False` 才打回，并给 harness 注入
   ` ```qa ` 结论协议（`StageResult.qa_passed`）。
2. **活动编排器未绑定**：图内 `get_orchestrator()` 会懒创建一个默认实例，显式构造的
   `Orchestrator`（CLI / 验收脚本 / 嵌入式）里小群与升级 host 因此静默失效。现在构造即
   绑定（`set_orchestrator`）。
3. **运行中旧快照覆盖并发写入**：阶段循环在每一步用运行开始时的 `Initiative` 快照
   `upsert`，会把升级真人刚写入的 `pending_decision_id` 抹掉，导致「钉钉/决策中心答复
   成功但任务不再恢复」。现在每步先取最新快照（`_fresh_initiative`），`submit_hitl`
   也会等运行锁释放后再投递 resume（避免 resume 打到尚未挂起的图）。
4. **Codex item error 误判**：实测 Codex 把配置告警也发成 `item.type=error`，原实现据此
   把整轮判失败。现在 item 级错误只告警，只有 stream 级 error 才判失败。

验证命令与结果：

```bash
python scripts/verify_collab.py --harness mock            # 8/8 pass
python scripts/verify_collab.py --harness claude_code     # Express 真机
python scripts/verify_collab.py --harness codex --scenario basic
```

实测结果（2026-10-26）：

| 模式 | 结果 | 证据 |
|------|------|------|
| `mock` 剧本化 | C1/C1b/C2/C3/C4/C5/C6/C7 **8/8 pass** | `data/verify/20261026-185509-mock/report.md` |
| `claude_code` 真机 | C1/C1b **2/2 pass**：`intake → eng_implement → qa_verify → deploy_preview` 4 阶段完成，2 次 HITL 放行，`final=done`；任务内出现 **2 次真实自主 handoff**（`product→eng_implement`、`eng→qa_verify`，均为模型自己声明，非脚本注入） | `data/verify/20261026-183535-claude_code/report.md` |
| `codex` 真机 | C1/C1b **2/2 pass**：同样 4 阶段 + 2 次 HITL，`final=done`；修复了 item 级 error 误判后稳定 | `data/verify/20261026-184319-codex/report.md` |
| `opencode` / `cursor` | 探测 `error` / `auth_required`，脚本显式 skip，不误报 | 同上 probes |

真机 `handoff` 专项场景（要求开发阶段主动交接并跳过自测节点）在 900s 窗口内未跑完
（真实 agent 把 build 当成了真实功能开发，耗时超出验收窗口）；C2 的真实证据来自
`basic` 场景：Claude 自行发出两次 handoff（`product→eng_implement`、`eng→qa_verify`）。
专项场景的确定性回归由 `mock` 剧本化模式覆盖。
