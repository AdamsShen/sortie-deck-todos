# 航线复杂度分档：极速 / 标准 / 完整

- 日期：2026-11-05
- 状态：待确认（未改代码）
- 关联：[2026-11-05-stage-boundary-and-upstream.md](./2026-11-05-stage-boundary-and-upstream.md)
- 背景任务：`ini_1f447c262543`（贪吃蛇小游戏，15 阶段 / 105 分钟）

## 1. 需求

1. 建任务时让用户选「极速 / 标准 / 完整」三档，并看到每档包含哪些阶段。
2. 选定档位后，planner（航线规划官）只能在该档位的阶段集合内**减少**阶段，不能新增。
3. 兼容现有「角色不够时由大模型自建角色」的机制，档位裁剪只作用于阶段，不砍角色扩展。
4. 非开发任务（日常办公、研究、写作）也要有三档定义，并给出阶段简述。

## 2. 现状（已核实）

### 2.1 planner 目前没有任何阶段预算

- LLM planner 拿到 `catalog` 后自由输出阶段，代码只校验 id 合法、`intake` 开头、`done` 结尾：

```137:191:/Library/git-root/sortie-deck-todos/src/sortie_deck/pipeline_llm.py
data = await llm.chat_json(system=_SYSTEM, user=user)
track = data.get("track_hint")
...
stages = normalize_specs(data.get("stages") or [])
```

- system prompt 反而鼓励拆细：多工种各开一个 tech_design、各开一条 build→selftest 分支、再加 eng_integrate：

```24:28:/Library/git-root/sortie-deck-todos/src/sortie_deck/pipeline_llm.py
When multiple eng_* specialists (eng_web, eng_backend, eng_ios, …) should run together,
give EACH its own tech design stage (`<role>_tech_design`, owned by that role) in an
`eng_planning` parallel wave, then independent build→selftest branch chains in an
`eng_delivery` parallel wave, then eng_integrate.
```

- `plan_pipeline_smart` 只要 LLM 返回成功就直接用，规则版 `resolve_track`（trivial → express）根本不执行：

```319:334:/Library/git-root/sortie-deck-todos/src/sortie_deck/pipeline_llm.py
if use_llm:
    llm_proposal = await plan_pipeline_llm(...)
    if llm_proposal:
        return llm_proposal
proposal = plan_pipeline(...)
```

### 2.2 现有 trivial 是「改动小」，不是「需求小」

- 关键词全是修补类（一行、typo、文案、小改、hotfix、配置变更）：

```37:60:/Library/git-root/sortie-deck-todos/src/sortie_deck/pipeline.py
_TRIVIAL_PATTERNS = [
    r"一行", r"一字", r"改个?字", r"小改", r"微调",
    r"hotfix", r"typo", r"错别字", r"文案", ...
]
```

- `auto` 判定还有「短简报」兜底，但用的是 `title + brief` 拼接长度：

```355:355:/Library/git-root/sortie-deck-todos/src/sortie_deck/pipeline.py
suggested, reason = suggest_track(f"{title}\n{brief}")
```

实测贪吃蛇：brief 单独 23 字 → express；title 与 brief 相同、拼接后 47 字 → standard。且「贪吃蛇 / 小游戏 / 登录注册」不含任何 trivial 关键词。

结论：**不能直接用现有 trivial 判贪吃蛇**，需要单独的任务类型 + 规模判定。

### 2.3 非开发任务现状

- 已有 `research` 轨（采集 → 结论 → 建议），但：
  - `suggest_track` 只会返回 `express | standard`，永远不会选 research；
  - `stage_catalog()` 只合并 standard/default/express + `_EXTRA_STAGES`，research 阶段不在 catalog 里，LLM planner 看不到也提不了；
  - `contracts.REQUIRED` 没有 research 阶段的产物契约，产物质量无校验。
- 前端建任务页（`apps/web/src/components/NewTaskPage.tsx`）没有任务类型 / 档位 / 复杂度选项，全部走 `auto`。

### 2.4 角色自建机制（需保留）

- `propose_loadout_llm` 允许 LLM 提出 catalog 外角色（`missing_slots`，最多 2 个）；
- `mission_briefing.py` 把 `missing_slots` 并入编制位，随后由 `_tool_ensure_roles` 生成 forge 草稿，进入 `ForgeReview.tsx` 人工审批；
- 这是非开发任务（如「育儿顾问」「行业分析师」）能跑起来的关键，档位裁剪不能影响这条链路。

## 3. 业内调研

| 来源 | 结论 | 对本案的启示 |
|---|---|---|
| [Anthropic — Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) | 最成功的实现从最简单的可组合模式开始，只在确实需要时才增加自主性；workflow 里包含 routing 模式，把简单任务路由到更轻的流程/模型以降低延迟和成本 | 档位本质是 routing：简单任务走静态轻流程，复杂任务才上规划、评审、多工种 |
| [CrewAI — Processes](https://docs.crewai.com/en/concepts/processes) | Sequential 是静态线性管道、无 router；Hierarchical 由 manager agent 逐步 LLM 路由 | 静态流程可预测、成本低；每多一层 LLM 编排就多一份延迟，和本次 15 阶段实测吻合 |
| [CrewAI process 成本/延迟对比](https://n4n.ai/blog/comparing-crewai-process-types-by-cost-and-latency) | 两种 process 的成本与延迟差异主要来自路由决策次数 | 减少阶段数 = 减少 LLM 路由次数，是延迟的主控项 |

三家共性：**流程复杂度应作为显式输入，而不是让规划模型自由发挥**。

## 4. 设计

### 4.1 两个正交维度

- `task_kind`：`delivery`（开发交付） | `knowledge`（办公/研究/写作）
- `tier`：`express`（极速） | `standard`（标准） | `full`（完整）

建任务时先定 `task_kind`（规则推荐 + 用户可改），再定 `tier`（规则推荐 + 用户可改）。

`task_kind` 判定规则（后端，纯规则，不额外调模型）：

| 信号 | delivery | knowledge |
|---|---|---|
| 关键词 | 游戏、页面、接口、App、脚本、部署、数据库、前后端 | 周报、月报、会议纪要、调研、方案、文案、表格、PPT、邮件、总结 |
| 交付物 | 代码 / 可运行程序 | 文档 / 表格 / 幻灯片 |
| 仓库绑定 | 绑定 git 仓库 | 未绑定仓库 |
| 兜底 | 命中工程阶段关键词 → delivery | 其余 → knowledge |

### 4.2 三档阶段定义

**delivery**

| 档位 | 阶段链路 | 适用 | 预估 |
|---|---|---|---|
| 极速 | 集结 → 开发 → 测试 → 发布 → 完成 | 单文件工具、小游戏、页面微调、Demo | 5-10 min |
| 标准 | 集结 → PRD → 设计 → 用例 → 开发 → 自测 → 测试 → 发布 → 完成 | 单端 + 有验收口径的中型需求 | 20-35 min |
| 完整 | 标准 + 分工种技术方案（`<role>_tech_design`）+ 分支自测 + 联调 + 提测 | 多端、跨模块、外部依赖 | 45-90 min |

极速档对应 `packages/templates/express_pipeline.yaml` 的 5 个节点；标准档对应 `standard_pipeline.yaml` 去掉联调/提测；完整档是现有 planner 的自由输出（收紧为 catalog 允许集合）。

**knowledge**

| 档位 | 阶段链路 | 适用 | 预估 |
|---|---|---|---|
| 极速 | 集结 → 产出 → 完成 | 周报、邮件、单篇文案、简单整理 | 3-6 min |
| 标准 | 集结 → 采集 → 产出 → 完成 | 会议纪要、资料汇总、带引用的短文 | 8-15 min |
| 完整 | 集结 → 采集 → 结论 → 建议 → 完成 | 调研报告、方案对比、需要结论与行动建议 | 15-30 min |

第一版直接复用 research 轨的 `research_scan`（采集）/ `research_synthesize`（结论/产出）/ `research_recommend`（建议），不新增阶段；二期再抽象成中性 `office_*` 阶段。

同时补齐两处缺口：

- `stage_catalog()` 合并 research 阶段，并打 `kind: knowledge` 标记，避免工程 planner 误选；
- `contracts.REQUIRED` 为 research 阶段补最小契约：`research_scan → notes.md`、`research_synthesize → report.md`、`research_recommend → recommendation.md`。

### 4.3 裁剪算法（只减不加）

新增 `src/sortie_deck/tiers.py`：

```python
TIER_STAGES: dict[tuple[str, str], list[str]]  # 4.2 的允许集合，有序
TIER_LABELS: dict[str, str]                     # 极速/标准/完整
TIER_ETA: dict[tuple[str, str], tuple[int, int]]
```

`enforce_tier(proposal, task_kind, tier) -> proposal`：

1. 按 `depends_on` 拓扑序展开提案；
2. 逐阶段映射到档位允许集合：
   - `eng_web | eng_backend | eng_ios | eng_android | eng_agent | eng_implement` → `eng_implement`（极速/标准）；
   - `*_tech_design` → 丢弃（极速/标准）；完整档保留；
   - `*_selftest` → 极速档丢弃、标准档合并为 `eng_selftest`、完整档保留；
   - `qa_cases | qa_handoff | eng_integrate` → 极速/标准档丢弃；
   - `product_prd | design_ui` → 极速档丢弃，标准/完整档保留；
3. 去重并按允许集合顺序重排（折叠后的 `eng_implement` 合并 `depends_on`）；
4. 必保阶段：`intake` 首、`done` 尾、核心产出（delivery 必须有 `eng_implement`；knowledge 必须有 `research_synthesize`）；
5. 若结果缺核心产出，回落到规则版 `plan_pipeline(tier=...)`；
6. 裁剪明细写入 `meta.tier_pruned = [{id, mapped_to, reason}]`，供前端展示「规划官提案被收敛了哪些节点」。

**关键约束**：`kept_ids ⊆ TIER_STAGES[(task_kind, tier)]`，且不新增提案里没有的阶段（除补齐 intake/done/核心产出）。

### 4.4 与角色自建机制的兼容

- `enforce_tier` 只处理 `stages`，不触碰 `roles` / `needed_slots` / `missing_slots`；
- 档位只限制**阶段数**，角色仍由 loadout planner 决定，`missing_slots` 仍生成 forge 草稿走人工审批；
- 增加档位角色上限，避免「极速档拉 5 个角色」把时间又拖回去：

| 档位 | 角色上限（含自建） |
|---|---|
| 极速 | 2 |
| 标准 | 4 |
| 完整 | 6 |

超上限的 `missing_slots` 不生成草稿，记入 `meta.deferred_slots`，用户升档后可继续。

### 4.5 API 与前端

- `GET /api/pipeline/tiers?task_kind=delivery|knowledge` 返回三档定义：
  `{tier, label, description, stages:[{id,label}], stage_count, eta_minutes:[min,max]}`，前端不硬编码阶段清单；
- `CreateInitiativeRequest` 增加 `task_kind: auto|delivery|knowledge`、`tier: auto|express|standard|full`；
- `NewTaskPage` 增加「任务类型 + 档位」两块卡片，每张卡展示：档位名、一句话适用场景、阶段链路（label 串联）、阶段数、预估时长；
- `PipelineEditor` 手动编辑时，若新增阶段超出当前档位允许集合，提示「超出当前档位，需升档或移除该节点」；
- 旧任务 `meta` 无 `tier` 时视为 `full`，行为与现在一致，不做数据迁移。

### 4.6 推荐值

`tier=auto` 时按规则推荐：

- `task_kind=delivery`：brief 短且单端、无持久化/第三方依赖 → `express`；提到多端/联调/支付/权限 → `full`；其余 `standard`。
- `task_kind=knowledge`：无「引用/来源/对比/调研」→ `express`；有「调研/对比/分析」但无「建议/方案」→ `standard`；两者都有 → `full`。
- 推荐结果在 UI 上标注「推荐」，用户可改。

## 5. 文件清单

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/tiers.py` | 新增：档位定义、阶段映射、`enforce_tier` |
| `src/sortie_deck/pipeline.py` | `stage_catalog()` 合并 research 阶段并打 kind 标记；`plan_pipeline` 支持 `tier` |
| `src/sortie_deck/pipeline_llm.py` | planner 输出后调用 `enforce_tier`；system prompt 增加档位预算说明 |
| `src/sortie_deck/models.py` | `CreateInitiativeRequest` 增加 `task_kind` / `tier` |
| `src/sortie_deck/contracts.py` | 补 research 三个阶段契约 |
| `src/sortie_deck/api/app.py` | 新增 `GET /api/pipeline/tiers` |
| `src/sortie_deck/orchestrator.py` | 建任务时判定 task_kind/tier、调用裁剪、写 `meta.tier_pruned` |
| `apps/web/src/components/NewTaskPage.tsx` | 三档选择卡片 + 阶段链路 |
| `apps/web/src/components/PipelineEditor.tsx` | 越界节点提示 |
| `apps/web/src/i18n.ts` / `types.ts` | 档位文案与类型 |
| `tests/test_tiers.py` | 折叠、去重、必保阶段、越界拒绝、角色上限 |

## 6. 备选方案

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 只做后端裁剪，不做 UI | 改动最小 | 用户不知道被砍了什么，不可控 | 否 |
| B. 用 `pipeline_track` 直接强制覆盖，不用 planner | 最简单、最可预测 | 丢失 planner 的排序/并行能力 | 作为兜底分支保留 |
| C. 让 planner 自己遵守档位预算 | 不改后端 | LLM 会越界，无法保证 | 否，只作为提示 |
| **D. 后端强制裁剪 + 前端选择 + planner 在预算内排序** | 可控、可解释、保留编排能力 | 需要新增 tiers 模块与 UI | **推荐** |

## 7. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| P1 | `tiers.py` + catalog/契约补齐 + 裁剪 + API | 单测：贪吃蛇 brief（delivery/express）由 15 阶段裁到 5 节点；角色扩展不被裁；旧任务行为不变 |
| P2 | 前端三档卡片 + 阶段链路 + 越界提示 | 建任务页可见两套三档；选择后提案与链路一致 |
| P3 | 办公阶段抽象（`office_*`）+ 二期文案 | research 复用退场，契约独立 |

**预期收益**：同类贪吃蛇任务 15 阶段 → 5 阶段，按 2026-11-05 实测速率（有效编码约 45 min + 固定开销）估算，总时长从 105 min 降到 25-35 min。

## 8. 风险

1. 折叠会损失并行收益：标准档把 `eng_web ∥ eng_backend` 折成单 `eng_implement`，串行可能更慢。这是刻意的取舍——标准档定位「单端、单工种」，多工种任务应升到完整档（full 档按简报命中的平台展开 `<role>_tech_design` / build / selftest 三条并行链）。
2. 办公任务复用 research 语义不精准（「写周报」≠「研究结论」）。缓解：第一版只在中性 label 上展示，P3 抽象。
3. 用户手动编辑航线与档位冲突。缓解：越界提示 + 升档，不静默丢弃。
4. `suggest_track` 旧的 trivial 语义仍然存在，需明确两者关系：`tier` 是显式选择，`complexity` 仅作为推荐输入，不再直接决定航线。

## 9. 状态

- [x] 方案确认（2026-11-05）
- [x] P1 实现（2026-11-05）：`tiers.py`、catalog 合并 research + 产物契约、`enforce_tier` 裁剪、`GET /api/pipelines/tiers`、`CreateInitiativeRequest.task_kind/tier`；全量测试 870 passed
- [x] P2 实现（2026-11-05）：建任务页任务类型 + 三档 chip + 阶段链路预览；`PATCH /api/briefing/sessions/{id}/prefs`；规划会话 draft 提案同样走档位收敛
- [x] P3 实现（2026-11-05）：`office_pipeline.yaml`（采集/产出/建议）+ 三处产物契约；`research_*` 保留兼容并折叠到 `office_*`
