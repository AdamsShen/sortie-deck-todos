# 技术方案：记忆治理改造（8 差距项分两阶段）+ 记忆体系整体策略文档

> 日期：2026-10-21
> 状态：阶段一 + 阶段二均已实施完成（阶段二：#6 memory 元工具、#7 Plan-time 并 mem0、#8 质量评估脚本；461 测试通过）
> 依据：`docs/多Agent协作记忆-调研与实现对照.md` 3.2 节差距项清单
> 实施差异备注：冲突检测对 mission 作用域豁免（并行分支结论相似是常态，
> 交给分支仲裁处理）；`get()` 可读取治理态条目以支持演变追溯。

---

## 一、需求背景

上一轮调研（《多Agent协作记忆-调研与实现对照.md》）确认本项目记忆体系已采用业界最主流的 Hybrid 拓扑（本地 + 共享记忆池），但存在 8 个差距项。用户确认：

1. **全部 8 项，分两阶段**：阶段一 #1-#5（高性价比核心治理），阶段二 #6-#8（阶段一完成并验证后评估实施）；
2. **治理能力基于 mem0 后端落地**：衰减、冲突检测交给 mem0（其写入侧原生 LLM 决策 ADD/UPDATE/DELETE/NOOP 即 Azure Foundry Consolidation 的等价物），本地 MemoryStore 做轻量兜底（衰减加权 + 状态标记），mem0 不可用时整体静默降级；
3. **新建独立策略文档** `docs/记忆体系整体策略.md`，专写写入/管理/读取时机全链路，与对照文档各司其职。

附带修复：`harness_providers/opencode.py:37` 复用 `settings.mem0_timeout` 的配置耦合。

## 二、业内做法摘要（本轮会话已联网核实）

| 做法 | 来源 |
|---|---|
| 写入时 LLM 三选一（新增/更新覆盖/丢弃冗余）+ 时间戳保留演变轨迹 | Azure Foundry Memory Consolidation、Mem0 ADD/UPDATE/DELETE 决策（mem0.ai/blog/state-of-ai-agent-memory-2026） |
| bi-temporal：旧事实失效标记不删除，可追溯历史 | Zep/Graphiti（cognee.ai/ai-memory-tools-vs-databases） |
| 语义记忆"更新路径比写入路径更重要"；记忆需新近度衰减与重要性过滤 | Mem0 State of Memory 2026、arXiv:2603.07670 |
| 程序性记忆（技能/工作流经验）单列一类，AgentCore "procedural" 策略、LangMem 显式支持 | studytrails.com/agentic-ai-book/chapter-3-agent-memory |
| 并行分支冲突：禁止后写覆盖，用角色权重/置信度/协调器仲裁 + Provenance | 多智能体记忆设计综述（ShanglinWu/LLM-MAS_Memory_Survey） |
| 角色分层写入：协调器写高层结论，执行者写过程增量，评估者写高权重观察 | 同上 |

## 三、阶段一（#1-#5）技术设计

### 3.1 差距 #2：角色分层写入摘要（先行，#1 依赖它的基础设施）

**现状**：`harness_providers/executor.py:194-197` 每轮无条件 `append_memory(f"Stage ...: {reply.text[:600]}")`，流水账。

**设计**：新增 `mission_memory.py` 内模块级函数 `build_stage_summary(stage, role, reply_text, harness) -> str`：
- 按角色分粒度：协调/规划类角色（planner 等，可配置映射）→ 用 LLM 提炼「决策/结论/风险」要点（temperature=0.1，超时 15s）；执行类角色 → 只保留回复首段 + 产物路径增量；
- LLM 失败/未启用时降级为现状的截断流水；
- 在 `_run_harness` 中替换原 `append_memory` 调用；`reply.text` 为空或状态非 ok 时仅记录状态行。
- 新增配置 `TDT_MEMORY_STAGE_SUMMARY_LLM: bool = True`（LLM 关闭时零成本回退）。

**改动文件**：`src/sortie_deck/mission_memory.py`、`src/sortie_deck/harness_providers/executor.py`、`src/sortie_deck/settings.py`

### 3.2 差距 #1：程序性记忆反思沉淀

**设计**：stage 执行成功后异步生成"程序性教训"，写入条目记忆 `scope="agent"`：
- `executor.py` 在 `record_turn`/`append_memory` 之后新增 `maybe_reflect(ctx, reply)`（fire-and-forget `asyncio.create_task`，不阻塞 stage 返回）；
- 仅对 `status=="ok"` 且回复长度 > 阈值的 stage 触发；LLM 用固定 system prompt 从 (stage, role, reply) 中提炼一条 `kind="lesson"` 的经验（「下次同角色做同阶段时应该…」），失败静默；
- 写入走 `_memory_store.create(CreateMemoryRequest(scope="agent", scope_id=ctx.role, kind="lesson", tags=["stage:"+ctx.stage, "procedural"], source="reflection"), author_id="system", author_name="反思沉淀")`；
- 去重护栏：同 (role, stage) 的 lesson 数量上限（默认 20 条，超限按 updated_at 淘汰最旧），防止无限堆积；
- 现有 agent 作用域注入链路（`context_candidates` 的 agent bucket，`memory.py:336-337`）自动让后续任务的同类 stage 受益——无需改注入侧。

**改动文件**：`src/sortie_deck/harness_providers/executor.py`、`src/sortie_deck/memory.py`（新增淘汰辅助）、`src/sortie_deck/settings.py`（`TDT_MEMORY_REFLECT_ENABLED: bool = True`、`TDT_MEMORY_REFLECT_AGENT_LESSON_CAP: int = 20`）

### 3.3 差距 #3：记忆衰减与失效（mem0 主力 + 本地兜底）

**设计**：
- **本地兜底（无 mem0 也生效）**：
  - `MemoryEntry` 增加可选字段 `status: Literal["active","superseded","expired"] = "active"`、`superseded_by: str = ""`（bi-temporal 思路：失效不删除）；
  - `MemoryStore.list()/context_candidates` 默认过滤 `status=="active"`；
  - `_score_entry`（`memory.py:107-112`）加入新近度因子：`score = keyword_hits * weight + recency_bonus`，`recency_bonus = max(0, 3 - days_since_update)`（可配置上限），使旧记忆自然沉底但不消失；
- **mem0 主力**：`Mem0Memory` 写入时携带 `metadata: {"tags", "created_at", "kind"}`（`memory.py:398-414` 现仅传 tags，扩展）；检索侧利用 mem0 平台的新近度/重要性排序，命中结果带分数并入 `CompositeMemory.context_block`；
- 索引文件 `MEMORY_INDEX.md` 行尾标注失效条目（`[已失效]`），前端 `GET /api/memory` 支持 `include_inactive` 参数查看历史。

**改动文件**：`src/sortie_deck/memory.py`、`src/sortie_deck/api/app.py`、`apps/` 前端记忆卡片（失效态展示，小改）、`src/sortie_deck/settings.py`（`TDT_MEMORY_RECENCY_BONUS_DAYS: float = 3.0`）

### 3.4 差距 #4：冲突检测更新（mem0 原生 + 本地兜底）

**设计**：
- **mem0 路径**：mem0 的 `add()` 原生执行 LLM 冲突决策（新事实 vs 已有记忆 → ADD/UPDATE/DELETE/NOOP）。`CompositeMemory.create` 中 mission 作用域镜像时改为**回读决策结果**（mem0 /v2/memories 响应中的 event/事件类型），若为 UPDATE/DELETE，则在本地对应条目上同步标记 `status="superseded"` + `superseded_by=新条目id`，并新增/更新本地条目——实现"mem0 判冲突、本地跟随"，两端一致；
- **本地兜底（mem0 不可用时）**：`MemoryStore.create` 增加轻量冲突检查：同 scope+scope_id+kind 且 `_terms` 重合度 ≥ 阈值（默认 0.8，可配）的 active 条目 → 新条目写入、旧条目标记 `superseded`（不调 LLM，纯规则，成本为零）；
- 全部决策记录进 `updated_at` 演变链（superseded_by 指向），满足"演变而非覆盖"。

**改动文件**：`src/sortie_deck/memory.py`（Mem0Memory.add 返回决策信息、MemoryStore.create 冲突检查）、`src/sortie_deck/settings.py`（`TDT_MEMORY_CONFLICT_THRESHOLD: float = 0.8`）

### 3.5 差距 #5：并行分支结论合并仲裁

**现状**：子房间并行 stage 各自打 `stage:<名称>` 标签隔离，无合并；汇入同一 stage 时靠 `list_for_stage` 顺序取前 N。

**设计**：
- `memory.py` 新增 `merge_stage_conclusions(entries) -> list[MemoryEntry]`：当同一 `query/stage` 命中多条带 `stage:` 标签的 mission 条目时，
  - 冲突检测：两两 `_terms` 重合度 ≥ 阈值视为同主题；
  - 同主题多分支：按 Provenance 打权（`author` 是协调角色 > 执行角色；`updated_at` 新 > 旧；置信度字段预留），取权重最高者为"采纳"，其余在 `render_block` 中以缩进行标注「另一分支结论：…」；
  - 记录仲裁结果到 logger.info（含条目 id 列表），可审计；
- `context_candidates` 中 mission bucket 渲染前调用此合并；
- **不做** LLM 仲裁（复杂度/成本不匹配收益，规则版本先验证）。

**改动文件**：`src/sortie_deck/memory.py`

### 3.6 附带修复：opencode 超时配置解耦

`harness_providers/opencode.py:37` 的 `settings.mem0_timeout` → 新增 `settings.opencode_http_timeout: float = 5.0`。

**改动文件**：`src/sortie_deck/settings.py`、`src/sortie_deck/harness_providers/opencode.py`

## 四、阶段二（#6-#8，阶段一验收后评估实施）

| # | 内容 | 预估要点 |
|---|---|---|
| #6 | `memory_search` / `memory_read` 内置工具 | 参照 `toolkit_brief` 现有注册机制（`orchestrator.py:973-980`）挂载；护栏：每 stage read 次数上限 + 单条全文长度上限；读取记录写 `ini.meta` |
| #7 | Plan-time 绑定并入 mem0 | `create_initiative` 的 `context_candidates` 换为含 mem0 的检索路径（需处理 mem0 异步延迟——任务刚创建时 mem0 尚无该 initiative 数据，需以 user/workspace 域为主） |
| #8 | 记忆质量评估 | 一次性脚本 `scripts/memory_eval.py`：构造 N 组「任务 brief → 应命中的记忆」用例，对比 context_candidates 与加衰减/冲突后的命中质量，输出报告；不做自动化 CI |

> 阶段二不纳入本次编码，阶段一验收（见下）后单独确认。

## 五、交付物：策略文档

新建 **`docs/记忆体系整体策略.md`**（中文），与《多Agent协作记忆-调研与实现对照.md》分工（对照文档=调研与差距，策略文档=本项目全链路规范）。内容大纲：

1. 记忆分层与作用域模型（三层体系 + 4 作用域 + 类型）
2. **写入时机**：人类写入（记忆库页 + 简介异步生成）、任务启动（绑定 + 记忆包落盘）、stage 执行后（分层摘要 + 反思沉淀 + 冲突决策）、mem0 镜像时机
3. **管理时机**：超限压缩（maybe_summarize）、衰减与失效（superseded/expired + recency 加权）、淘汰上限（agent lesson cap）、冲突合并链
4. **读取时机**：Plan-time 绑定、stage Run-time 注入、房间讨论注入、Agent 主动取用（INDEX→entries→usage.log）
5. 治理规则速查表 + 配置项总表（全部 TDT_ 前缀）
6. 与业界方案（AgentCore/Mem0/Zep）的对应关系一览

同时更新 `docs/zh/modules/memory.md` 的实施计划表（把本次落地项从"待实施"改为"已完成"）。

## 六、文件改动清单（阶段一）

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/mission_memory.py` | 新增 `build_stage_summary`（角色分层摘要 + LLM 降级） |
| `src/sortie_deck/harness_providers/executor.py` | 替换 `append_memory` 为分层摘要调用；新增 `maybe_reflect`（异步反思） |
| `src/sortie_deck/memory.py` | MemoryEntry 增 status/superseded_by；`_score_entry` 衰减加权；`create` 冲突检查；`Mem0Memory.add` 决策回读；`merge_stage_conclusions`；agent lesson 淘汰 |
| `src/sortie_deck/api/app.py` | `/api/memory` 支持 `include_inactive`；失效条目状态流转接口（可选） |
| `src/sortie_deck/settings.py` | 新增 6 个 TDT_ 配置；opencode_http_timeout |
| `src/sortie_deck/harness_providers/opencode.py` | 超时配置解耦 |
| `apps/`（记忆卡片） | 失效条目展示（小改） |
| `tests/` | `test_memory_governance.py`：衰减打分、冲突标记、合并仲裁、分层摘要降级、反思写入与淘汰上限 |
| `docs/记忆体系整体策略.md` | 新建（交付物） |
| `docs/zh/modules/memory.md` | 实施计划表状态更新 |

## 七、备选方案

- **冲突检测纯本地 LLM**（每条写入调 LLM 三选一）：效果同 Azure Foundry，但写路径延迟 +15s 且成本高——已否，用户选定 mem0 主力。
- **#5 仲裁用 LLM 投票**：质量更高但 stage 注入路径上多一次 LLM 调用——先规则版，效果不足再升级。
- **衰减用 TTL 硬删除**：简单但丢失演变历史，违背 bi-temporal 可追溯——采用失效标记。

## 八、验收标准

1. `pytest tests/test_memory_governance.py` 全绿；现有 `test_memory_agents.py` 等不回归；
2. mem0 关闭（默认 local）时：分层摘要降级流水、反思可关、冲突走规则兜底、衰减加权生效——零外部依赖可运行；
3. mem0 开启时：mission 写入镜像带决策回读，本地条目状态与 mem0 一致；
4. stage 注入的 memory brief 中旧记忆排名随新近度下降；冲突条目呈现"采纳 + 参照"结构；
5. `docs/记忆体系整体策略.md` 覆盖写入/管理/读取全部时机，与代码行为一致。

## 九、风险

- mem0 服务联调依赖外部可用性——所有 mem0 路径保留静默降级（沿用现有 BLE001 模式），mem0 不可用不影响验收 2；
- 反思沉淀引入每次 stage 后一次 LLM 调用（异步、可关）——成本可控，默认开但配置可关；
- 前端失效展示涉及 `apps/` 结构——保持最小改动，若前端结构复杂则先只做 API 层，UI 后续跟进。
