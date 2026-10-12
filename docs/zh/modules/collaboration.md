# 协作模块（契约 · 角色 · Forge · Manager · 小群 · Loadout · 决策）

本页是[多 Agent 协作模型](../design/multi-agent-collaboration.md)的实现索引：新增了哪些模块、扩展了哪些字段、暴露了哪些接口。

## 新增模块

| 模块 | 职责 |
|------|------|
| `sortie_deck/agent_contract.py` | `AgentContract`（inputs / outputs / handoff_targets / timeout / on_fail / tools_allowed）、persona front-matter 解析、`validate_handoff`。纯数据层，不依赖编排 |
| `sortie_deck/roles.py` | `RoleRegistry`：内置角色 + persona 角色 + 自定义角色（`data/roles.json`），提供规范化、语义去重与查询 |
| `sortie_deck/agent_forge.py` | 大模型现场生成角色（persona + 契约），自检后落为 `pending-review`，人工改后发布 |
| `sortie_deck/manager.py` | 收窄职责的 Manager：汇合仲裁、冲突/升级、plan 摘要（`plan_digest`） |
| `sortie_deck/subroom.py` | 小群会话：独立 manager 实例、预算、深度上限 1、结论回写为定向记忆 |
| `sortie_deck/loadout.py` | 按需能力加载：召回 → 选择 → 探测 → 加载或降级，产出可审计清单 |
| `sortie_deck/skill_runtime.py` | Skill 脚本执行：语言白名单、超时、沙箱目录、写操作二次确认 |
| `sortie_deck/decisions.py` | 真人决策中心：`decision_id` 幂等、决策人鉴权、SLA 超时降级 |
| `sortie_deck/notify.py` | 多通道出站（钉钉 / 飞书 / 通用 webhook）与内容脱敏分级 |
| `sortie_deck/guardrails.py` | 任务级预算、等待图死锁检测、`trace_id` 因果链 |

## 契约的来源

契约写在 persona 的 front-matter 里，`load_persona` 只返回正文，YAML 不会污染提示词：

```markdown
---
role_key: qa
title: QA Engineer
outputs: [test_plan.md, test_cases.md, test_report.json]
handoff_targets: [eng_web, eng_ios, eng_android, eng_backend, eng, deploy]
timeout_seconds: 900
on_fail: escalate
---
You are a QA engineer …
```

`handoff_targets` 为空 = **不允许自主 handoff**，一律走编译期默认边。

## 扩展的领域字段

| 模型 | 新增 |
|------|------|
| `RoleId` | `_missing_` 让未注册的角色键成为一等成员（`parenting`、`stocks` …），`.value` 与 `== RoleId.X` 语义不变 |
| `StageContext` | `memory_brief`（分层记忆 + 小群结论）、`handoff_targets` |
| `RoomMessage` | `room_key`（`main` 为大群）、`msg_type` 增加 `subroom` |
| `TimelineEvent` | `kind` 增加 `handoff`、`subroom` |
| `PipelineState` | `plan_digest`、`handoff_target`、`handoff_counts`、`handoff_total`、`wave_attempts`、`halt` |
| `ToolkitItem` | `capabilities`、`executable`、`read_only`（供 loadout 召回与降级判断） |
| `Initiative.meta` | `plan_digest`、`loadout_plans`、`guardrails`、`pending_decision_id`、`forged_agents` |

## 路由实现要点

- **只有一种路由机制**：节点之间**没有静态边**。每个节点把下一跳写进 `handoff_target`，条件边读取它；冻结的 plan 就是这条条件边的默认分支。agent handoff、Manager 汇合决策、HITL 裁决全部走这一条路。
- **条件边而非 `Command(goto=)`**：本仓库使用的 LangGraph 版本在节点返回 `Command(goto=…)` 时**仍会走该节点的静态出边**，两者同时触发会让并行分支撞在同一个 state channel 上。统一成条件边后这个问题从根上消失，图里也不再需要 `Command`。
- **HITL 门禁同样是节点跳转**：门禁裁决写入 `handoff_target`（approve → plan 下一节点、reject → `on_reject`、reroute → 指定节点、stop → `stopped`），默认分支是 plan 的 approve 目标。
- **HITL 优先**：`hitl_after` 节点不参与自主 handoff，默认分支指向 `hitl__<stage>`。
- **落点校验**：目标必须是 plan 节点；同一目标 handoff 上限 3 次，任务级上限 40 次；`done` 不允许被直接跳到。
- **不可关**：路由没有开关（`TDT_AGENT_HANDOFF` / `TDT_MANAGER_JOIN_DECISION` 已移除）——这就是协作模型本身，不是可选项。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/roles` | 角色注册表（内置 + persona + 自定义） |
| POST | `/api/roles` | 注册自定义角色 |
| DELETE | `/api/roles/{role_key}` | 删除自定义角色 |
| GET | `/api/forge/drafts` | Forge 草稿（默认全部状态，可 `?status=pending-review`） |
| POST | `/api/forge/drafts` | 按能力描述生成草稿 |
| PATCH | `/api/forge/drafts/{id}` | 人工编辑草稿（persona / 契约 / 标签） |
| POST | `/api/forge/drafts/{id}/approve` | 审核通过，注册角色并可选发布 agent |
| POST | `/api/forge/drafts/{id}/reject` | 驳回 |
| GET | `/api/subrooms` | 小群记录（可按 `initiative_id` 过滤） |
| GET | `/api/subrooms/{id}/transcript` | 小群讨论记录（不回流大群） |
| GET | `/api/decisions` | 真人决策列表（可按 `initiative_id` / `status` 过滤） |
| POST | `/api/decisions/{decision_id}/answer` | 回复决策（幂等 + 鉴权） |
| POST | `/api/decisions/sweep` | 结算超时决策并应用 fallback |
| POST | `/api/bridges/dingtalk` | 钉钉机器人入站回调 |

## 工作台

三处增量视图，不改动既有页面结构：

- **角色锻造**：Forge 草稿列表，展示自检问题与疑似重复角色，可编辑 persona/契约后发布。
- **小群**：只读时间线 + 结论卡片 + 预算消耗。
- **决策中心**：待真人决策项、超时倒计时、fallback 与回复入口。

## 测试

| 文件 | 覆盖 |
|------|------|
| `tests/test_agent_contract.py` | 契约解析、非法 handoff 拦截、全部内置 persona 契约自检 |
| `tests/test_roles_forge.py` | 角色注册/去重/forge 审核发布、loadout 白名单与协作判定 |
| `tests/test_handoff_join.py` | 条件边 handoff、非法目标回落、HITL 不可绕过、ping-pong 预算、汇合决策、plan 摘要 |
| `tests/test_subroom.py` | 小群发起、独立 manager、深度上限、预算、结论作用域、记录不回流 |
| `tests/test_executor_session.py` | `run_session` 委托、小群挂起-续跑、图内端到端 |
| `tests/test_loadout.py` | 召回-选择-探测-降级、Skill 执行与超时、MCP 白名单与写确认 |
| `tests/test_decisions.py` | 脱敏、钉钉签名、超时降级、重复回复幂等、非授权人拒答 |
| `tests/test_im_escalation.py` | 钉钉入站解析、升级-暂停-回复闭环、超时结算 |
| `tests/test_guardrails.py` | 预算各维度、死锁检测、因果链、小群预算拦截 |
| `tests/test_research_track.py` | 非工程航线样例（研究 → 结论 → 建议） |
