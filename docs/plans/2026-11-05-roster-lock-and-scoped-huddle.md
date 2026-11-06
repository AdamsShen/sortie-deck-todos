# 编制锁定与有界小群：航线 / 名册 / 子会话的收敛方案

- 日期：2026-11-05
- 状态：待确认（未改代码）
- 关联：[2026-11-05-pipeline-complexity-tiers.md](./2026-11-05-pipeline-complexity-tiers.md)、[2026-11-05-subagent-parallel-integration.md](./2026-11-05-subagent-parallel-integration.md)

## 1. 需求

用户提出的目标模型：

1. 规划官定完航线草稿后，阶段与其执行角色即确定，每个阶段由哪个角色执行明确落库；
2. 草稿编辑阶段，用户可以把任意全局角色 / 自定义角色加入航线；
3. 航线确认后，名册外角色不得再参与该任务的讨论与执行；
4. 执行中（如开发阶段），owner 角色可主动发起小群讨论，且该角色有明确注入的 prompt。

本质是把「编制」从「档位模板全量发人」改成「按航线确定的最小可用名册 + 用户显式增补」，并在确认时冻结。

## 2. 现状核实（本项目）

| 用户期望 | 现状 | 证据 |
|---|---|---|
| 阶段有明确执行角色 | **部分满足**。航线 spec 的 `role` 字段存在，规划官/阶段目录会填；前端只展示不可改 | `models.py:477-491`；`PipelineEditor.tsx` 角色列为纯文本 |
| 草稿期可加任意角色 | **不满足**。只能从阶段目录加阶段，角色随阶段固定；无角色增删接口 | `PipelineEditor.tsx` 的 `onAddStage(c.id)`；全仓库无 roster 写接口 |
| 确认后锁定名册 | **不满足**。确认只锁阶段，名册仍是模板全量 10 个 | `orchestrator.py:789-791`、`1136-1139`；`default_pipeline.yaml:6-46` |
| 只有名册内角色可讨论/执行 | **讨论：全量名册都能发言**；**执行：按 stage.role 查名册，查不到静默跳过** | `orchestrator.py:991-1007`、`664`；`graph.py:1441-1444` |
| 执行中主动拉小群 | **已实现**，但参与者取全量名册 | `subroom.py:1-18`、`graph.py:1412-1484`、`executor.py:115-131` |
| 角色拿到注入 prompt | **已实现**，但拼装分散、未留可审计快照 | `graph.py:1702-1720`、`1818+`、`2075` |

关键事实：

- 名册 = `Initiative.roles`，元组字段含 `title/persona/executor/toolkit_ids/repos/catalog_agent_id/avatar`（`orchestrator.py:816-831`）。
- 名册来源是档位模板的 `roles` 列表，`express/standard/default` 三个模板都是同一批 10 个角色；没有按 stages 反向裁剪的代码。
- 小群参与者来自 `ini.roles`（`graph.py:1439`），阶段咨询候选来自 `_available_roles(ini)`（`stage_consult.py:39-40`）。名册一收，这两个候选集合自动收窄。
- 档位是硬约束：手动编辑航线不能突破档位白名单（`orchestrator.py:1270-1295`），只能减不能加。
- 角色创建后无增删改接口，唯一可改是头像（`api/app.py:1323-1336` → `orchestrator.py:1849-1877`）。

## 3. 业内调研

调研工具说明：`agent-reach` 的本机命令入口缺失（`agent-reach` 不在 PATH，`mcporter`/`gh` 未安装），本轮改用 `web_search` + 官方文档 `web_fetch`，来源均为官方页面。

| 框架 | 成员/拓扑确定时机 | 运行时可否加人 | 子协作形态 | 来源 |
|---|---|---|---|---|
| LangGraph | `compile()` 固化拓扑；子图作为节点或节点内调用，schema 隔离、checkpoint 命名空间隔离；中断向上冒泡 | 页面未提供运行时改图 API，编译产物按不可变构件使用 | 子图 = 作用域收敛的协作单元，父图只看到显式映射回来的字段 | https://langchain-ai.github.io/langgraph/concepts/subgraphs/ |
| CrewAI | `Crew(agents=[...])` 构造时确定；`Process.hierarchical` 由 manager 分派 | 委派范围限于同一 crew 成员（`coworker` 参数），无跨 crew 委派 | `allow_delegation=True` 自动注入 Delegate / Ask 两个工具；官方明确提示委派环，建议专家角色关闭委派 | https://docs.crewai.com/edge/en/concepts/collaboration |
| AutoGen | 团队构造时传入 agent 列表，顺序即轮转顺序；Swarm 的 `HandoffMessage` 只能转到预先注册的成员 | 本页无运行时增删成员 API，换成员需重建团队 | GroupChat 共享上下文广播；子会话（nested chat）用于临时专项小组 | https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html |
| OpenAI Agents SDK | handoff 目标必须静态注册（`handoffs=[...]`），模型只能选已注册目标 | 目标 agent 不可由模型改写；仅自定义 `Handoff` 子类可在调用时刻决定目标 | `input_filter` 裁剪下一位 agent 看到的历史；`Agent.as_tool` 用于不转移会话的结构化调用 | https://openai.github.io/openai-agents-python/handoffs/ |

归纳出的四条共性（与本项目对齐）：

1. **拓扑与成员在“编译/确认”时固定**，运行期只做路由与状态流转，不做结构变更；
2. **子协作是作用域收敛的**：参与者是成员的子集，上下文私有，只回传结论；
3. **上游产物直接传递优于来回问答**（CrewAI `context=[task]`），讨论应当只用于真正阻塞的点；
4. **讨论/委派必须有硬上限**（CrewAI 明确提示委派环），否则成本和时延失控。

与本项目冲突点：本项目目前把「档位模板全量角色」当成成员，等于把「可上场名单」误当成「已上场名单」，导致成员与拓扑脱钩；而框架们的成员集合都直接来自拓扑。

## 4. 技术设计

### 4.1 概念拆分

- **航线（pipeline）**：阶段序列 + 每阶段 `role` + 门禁/依赖，确认后即执行计划。
- **编制（roster）**：本次任务可上场角色集合。**新规则**：`roster = stages[].role 的并集 ∪ 用户草稿期显式加入的角色`。
- **绑定（binding）**：每个 slot 对应的发布 agent / 人设 / 工具包 / 仓库 / 头像，建队时一次性解析，随 roster 落库。

### 4.2 草稿期（planner → 可编辑）

1. 规划官提案阶段与 `role`；后端 `_apply_tier_to_proposal` 仍按档位裁剪阶段（保留性能护栏）。
2. 裁剪后重算 roster：只保留被阶段引用的角色，避免「编制 10、上场 4」。
3. `PATCH /api/initiatives/{id}/pipeline` 扩展：
   - 允许修改 `stages[].role`（必须是角色目录内的 key）；
   - 允许增删 `roster` 角色（全局角色 + registry 自定义角色），但**阶段集合仍受档位白名单约束**（只允许换人，不允许越档加阶段）；
   - 每次写入重算绑定并回显「参与 x / 编制 y」。
4. 前端 `PipelineEditor` 角色列改为下拉；新增「编制」区显示已加入但暂无阶段的角色，并标记「待命」。

### 4.3 确认期（冻结）

`confirm_pipeline` 时：

1. 校验每个 `stages[].role` 都能在 roster 中解析到 `RoleAgent`，缺失直接 400（不再运行期静默跳过）；
2. 写入 `meta["execution_plan"] = {stages, roster_slots, bindings, frozen_at, hash}`；
3. 此后 `PATCH pipeline` / roster 写接口一律拒绝（`RuntimeError("plan is frozen")`）；
4. 冻结事件进 timeline + room，便于审计。

### 4.4 运行期（收敛）

- 阶段执行：只按 `stages[].role` 在 roster 找人；缺失记 `warn` 并标记阶段失败，不静默继续（`orchestrator.py:664`、`graph.py:1441-1444`）。
- 房间讨论：`participants = roster`（现状已是，收窄后自动生效）；`@` 名册外角色返回明确提示（现状 `orchestrator.py:2008-2014` 已有该文案）。
- 阶段咨询：候选 = roster（`stage_consult.py:39-40`，自动生效）。
- 小群：参与者 ⊆ roster（`graph.py:1439`、`subroom.py:251`，自动生效）；保留深度 1、token/轮次/超时上限。
- 人（owner / 共享用户）不受 roster 限制。

### 4.5 Prompt 注入显式化

把当前分散的拼装收敛为一个 `StagePromptPack`：

```
persona           角色人设（含 toolkit brief）
memory            分层记忆（mission/stage/role 作用域）
upstream          上游产物（按 output_contract 过滤）
consult_log       阶段前咨询结论
subroom          小群结论（若有）
contract_hints    交付契约提示
```

落 `stage.meta["prompt_pack"]`（只存来源与摘要，不存全量 prompt），供节点详情页 review 与问题复现。执行入口仍是 `graph.py:1702-1720` 与 `1818+`。

### 4.6 数据兼容

- 老任务无 `execution_plan`：按 `ini.roles` 全量处理，前端徽章显示「参与 x / 编制 y」，不做回收；
- 新任务走新规则；
- 迁移脚本不强制，避免动线上正在跑的任务。

## 5. 文件改动清单

后端：

- `src/sortie_deck/models.py`：`UpdatePipelineRequest` 增加 `roster`（可选），新增 `RoleRosterSpec`；`Initiative.meta` 增加 `execution_plan`。
- `src/sortie_deck/orchestrator.py`：
  - `_roles_from_template` → `_build_roles(slots=...)`，支持 registry 自定义 slot；
  - `create_initiative` 调整顺序：先定航线再建 roster；
  - `update_pipeline` 支持换 role / 增删 roster，`confirm_pipeline` 冻结与校验；
  - `_seed_participants` 以 roster 为准。
- `src/sortie_deck/pipeline.py`：`normalize_specs` 校验 `role` 合法性。
- `src/sortie_deck/graph.py`：缺角色 fail fast；`_build_session_host` 与小群 roster 走新名册。
- `src/sortie_deck/stage_consult.py`：候选集合沿用 roster（无需改逻辑，补测试）。
- `src/sortie_deck/api/app.py`：`PATCH pipeline` 透传 roster；新增 roster 读写接口（草稿态）。

前端：

- `apps/web/src/components/PipelineEditor.tsx`：角色下拉、编制区、待命标记。
- `apps/web/src/App.tsx`：徽章改「参与 x / 编制 y」；确认页展示未上场角色提示。
- `apps/web/src/types.ts`：`Initiative.meta.execution_plan`、`PipelineStageInfo.role` 类型。

测试：

- `tests/test_pipeline_roster.py`（新增）：裁剪、换 role、自定义角色、越档拒绝、confirm 冻结；
- `tests/test_subroom*.py`：名册外角色不可入群；
- `tests/test_stage_consult*.py`：候选集合收敛。

## 6. 备选方案

| 方案 | 内容 | 取舍 |
|---|---|---|
| A | 只改前端徽章「参与 / 编制」 | 改动最小，但名册仍虚胖，讨论与小群仍拉进未上场角色 |
| B | 只裁剪名册，不改编辑器 | 语义先正确；但用户无法补人，草稿期改航线受限 |
| C（推荐） | 裁剪 + 编辑器可换 role/加人 + confirm 冻结 | 与主流框架一致，一次到位 |

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 草稿期漏加角色，确认后发现缺人 | 确认页列出「未上场角色」并给出一键加入；冻结前强制二次确认 |
| 老任务名册不回收，展示不一致 | 老任务显示「参与 x / 编制 y」，文案说明；不强制迁移 |
| 档位升档不可逆 | 冻结页提示「需要更高档位请新建任务」 |
| 小群候选变少，讨论质量下降 | 草稿期显式加人；小群上限保持（1 个/阶段、≤4 参与、轮次/超时） |
| 讨论成本失控 | 沿用现有硬上限；consult ≤2 问，subroom 深度 1 |

## 8. 实施阶段

- **P0**：名册按航线裁剪 + 徽章「参与 / 编制」+ 缺角色 fail fast（不动 UI 交互）
- **P1**：编辑器换 role、增删 roster（全局 + 自定义）
- **P2**：confirm 冻结 `execution_plan` + 讨论/咨询/小群统一收敛校验
- **P3**：`StagePromptPack` 显式化与可审计

P0 可独立上线，P1/P2 有依赖关系，P3 无阻塞。
