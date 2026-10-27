# 多 Agent 协作的任务执行评测方案（v2）

> 目标：为 Sortie Deck 建立一套**可复算、可对比、可回归**的评测体系，回答四类问题：
>
> 1. **效率**：一次任务/一个阶段慢在哪、贵在哪、并行有没有收益、人工等待占多少；
> 2. **结果**：任务是否真的完成、产物是否合格、返工率多高、换 harness/模型后是变好还是变差；
> 3. **协作**：分工是否合理、交接是否有效、沟通开销多大、冲突如何解决、谁贡献了里程碑；
> 4. **可视化**：能否在 5 分钟内定位「哪次交接、哪个工具、哪个角色」导致失败，而不是翻 timeline。
>
> 状态：**待确认，未开始编码**。本方案与 `docs/plans/2026-10-27-otel-full-tracing.md`（追踪）是上下层关系：OTel 负责事实，本方案负责度量、数据集与可视化。

## 1. 需求背景

当前项目已经能回答「这一次任务发生了什么」：`TimelineEvent` 记录业务事件，OTel span 记录执行层级。但无法回答：

- 过去 30 天 default / standard / express 三条轨道的成功率分别是多少？
- 换成 `cursor_cli` 后，阶段耗时和 token 是升了还是降了？
- 一个任务从创建到完成，有多少时间在跑、多少时间在等人、多少时间在排队？
- 阶段 `ok` 了，但产物真的合格吗？人工打回了多少次？
- 这次改 prompt / 改模型，是变好了还是只是「感觉快了」？

这些都需要**跨任务的统一口径 + 基线对比 + 回归门禁**，不是单条 trace 能回答的。

## 2. 现状核查

### 2.1 已经具备的数据

| 数据 | 位置 | 粒度 | 可靠性 |
|---|---|---|---|
| 任务终态 / 当前阶段 | `src/sortie_deck/models.py:312-342` | 任务 | 高（`Initiative.status`） |
| 阶段结果 | `src/sortie_deck/models.py:380-386`（`StageResult`） | 阶段 | 高（`status` / `qa_passed` / `meta`） |
| 业务事件流 | `src/sortie_deck/models.py:228-252`（`TimelineEvent`） | 事件 | 高（`at` / `kind` / `data` / `trace_id`） |
| 产物契约 | `src/sortie_deck/contracts.py:15-47` | 阶段 | 只校验「文件是否存在」 |
| 产物文件 | `src/sortie_deck/artifacts.py:71-119`、`src/sortie_deck/layout.py:5-12` | 文件 | 高 |
| HITL 决策 | `src/sortie_deck/orchestrator.py:2003-2011` | 事件 | 高（`data` 为完整 `HitlDecision`） |
| 自主交接 / 拒绝 | `src/sortie_deck/graph.py:850-916` | 事件 | 高（accepted / denied + reason） |
| MCP 工具调用 | `src/sortie_deck/graph.py:1025-1054`；产物 `mcp_calls.json` | 调用 | 高（含 ok / error） |
| 工具使用汇总 | `src/sortie_deck/graph.py:1579-1639` | 阶段 | 高（绑定 / 调用 / 检索 / 上浮） |
| git 回写 | `src/sortie_deck/git_publish.py:34-41`、`:338-345` | 阶段 | 高（commit / push / mr_url / error） |
| 预算与因果链 | `src/sortie_deck/guardrails.py:46-180` | 任务 | 中（token 只统计了 subroom） |
| OTel span | `src/sortie_deck/observability.py:161-248`、`src/sortie_deck/graph.py:1200-1223` | 调用 | 高（但只导出 trace，无聚合） |
| 队列 job | `src/sortie_deck/run_queue.py:30-55` | job | 中（无 claim 时间戳） |
| 人工决策中心 | `src/sortie_deck/decisions.py:41-73` | 决策 | 高（created_at / answered_at / SLA） |

### 2.2 关键缺口

| # | 缺口 | 影响 | 补法 | 位置 |
|---|---|---|---|---|
| 1 | 阶段耗时未结构化 | 只能靠 timeline 相邻事件近似，无法区分执行与等待 | `StageResult.meta["duration_ms"]`，阶段事件自动带出 | `graph.py:1225` 起计时，`graph.py:1552` 后写值，事件在 `graph.py:1556-1573` |
| 2 | 任务总时长不可信 | `created_at → updated_at` 含 HITL 等待与空闲 | 以 `mission.run` span 分段累加；补 `sortie.segment` | `orchestrator.py:1835-1862` |
| 3 | token / 成本不进业务对象 | 无法按任务/阶段/角色归因；预算只统计 subroom | 从 span 聚合；或 contextvar 累计到 mission | `subroom.py:304` 是目前唯一 `consume(tokens=...)` |
| 4 | 契约结果只以异常体现 | 合同失败直接整个 run 失败，不知道缺哪个阶段哪个文件 | 记录 `contract_ok` / `missing` 后再抛 | `graph.py:1549`、`contracts.py:43-47` |
| 5 | 队列等待无时间戳 | 多 worker 下无法区分「排队慢」与「执行慢」 | `run_jobs` 加 `started_at` | `run_queue.py:30-55`、`claim()` `:77-135` |
| 6 | 并行分支无标识 | 同层多个 `stage.run` 无法归属分支，算不了并行收益 | span 加 `sortie.parallel_branch` | `graph.py:2033-2045` |
| 7 | HITL 等待无显式区间 | escalation 与普通门禁两条路径口径不一 | `submit_hitl` / 决策中心写 `wait_ms` | `orchestrator.py:1973-2011` |
| 8 | 结果质量无自动判定 | 契约只查存在性；真实 harness 的 `qa_passed` 不可靠 | 离线 evaluator：测试命令 / rubric / 人工评分 | 新增 `evaluation/evaluators.py` |
| 9 | coding CLI 内部不可见 | `claude` / `cursor` 子进程里的 LLM token 拿不到 | 明确声明盲区，只测 `process.exec` 边界 | `plugins/coding.py:125-161` |

### 2.3 现在就能算的（无需改代码）

- 任务状态分布：`api/app.py:464-484` 的 dashboard 已有。
- 阶段状态序列、HITL 动作分布、交接成败、工具调用次数：遍历 `Initiative.timeline` 即可。
- 产物数量 / 大小 / 是否存在：`ArtifactRef` + 文件系统。
- MR 是否创建：`StageResult.meta.git_publish`。
- 但**没有统一口径、没有聚合、没有基线、没有回归**，只能算零散统计。

### 2.4 协作评测特有的数据缺口

现有数据能说明「谁在什么时候做了什么」，但不足以说明「协作是否有效」：

| # | 缺口 | 影响 | 补法 | 位置 |
|---|---|---|---|---|
| 1 | 没有消息级协作图 | 无法回答「A 给 B 的信息，B 是否用了」 | 从 `RoomMessage` + handoff 事件抽取 `from/to/kind/ref` 边 | `rooms.py:49-53`、`graph.py:850-916` |
| 2 | 交接载荷无 schema | 只能知道交接发生，不能判断信息是否完整 | 在 `AgentContract` 增加 `handoff_required_fields`，交接时校验 | `agent_contract.py:40-55` |
| 3 | 没有里程碑定义 | 只有阶段 `ok/fail`，无法算协作推进度 | 每条 track 定义 milestone（阶段 + 契约 + 关键产物） | 新增 `evaluation/milestones.py` |
| 4 | 没有贡献归因 | 不知道哪个角色推进了哪些里程碑 | 按阶段 / 交接 / 工具调用归因，记录 `n_j/M` | 新增 `evaluation/collaboration.py` |
| 5 | 沟通成本未拆分 | 无法区分「干活」与「协调」的 token | 消息按 `msg_type` 分类；LLM 调用按上下文归因 | `models.py:458-480`、`llm_chain.py:179-202` |
| 6 | 没有失败模式标签 | 失败原因只能人工看 trace | 引入 MAST 14 类，LLM judge + 人工校准 | 新增 `evaluation/failure_taxonomy.py` |
| 7 | 角色越界无检测 | 无法判断 agent 是否做了角色外的事 | 记录工具/阶段访问，与 `AgentContract` 比对 | `agent_contract.py:40-55` |
| 8 | 并行协作不可见 | 不知道并行波次里谁和谁真正交互 | span 加 `graph.node.parent_id` + `sortie.parallel_branch` | `graph.py:1209-1218`、`:2033-2045` |

## 3. 业内调研结论

| 结论 | 来源 |
|---|---|
| Agent 评测必须同时评「结果」和「过程」：只看最终输出会漏掉错误工具、越权、重试放大成本 | [AI Agent Evaluation: How to Test Agents Before Production](https://exploreagentic.ai/insights/ai-agent-evaluation-testing)、[MLflow: AI Agent Evaluations](https://mlflow.org/articles/ai-agent-evaluations-a-developers-practical-guide/) |
| 指标通常分三组：outcome（成功率/正确性）、process（轨迹/工具选择）、operational（延迟/成本/护栏触发） | [AI Agent Evaluation: How to Measure Whether Your Agents Actually Work](https://iternal.ai/ai-agent-evaluation) |
| 成熟团队按「人工看 trace → 建 golden set → 自动 evaluator → 回归门禁 → 线上监控」演进，先自动化一切容易失控 | [Langfuse: AI Agent Observability](https://langfuse.com/blog/2024-07-ai-agent-observability-with-langfuse)、[Evaluating AI Agents: Golden Sets, Judges, and Shadow Mode](https://www.internalsdecoded.com/articles/evaluating-ai-agents) |
| LLM-as-judge 适合语义质量，但应作为补充，不应替代确定性信号；需要 rubric 与人工校准 | [Langfuse LLM-as-a-Judge](https://js-sdk-v4-docs-snapshot.langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge) |
| OTel GenAI 指标只需两个核心直方图：`gen_ai.client.operation.duration`（秒）与 `gen_ai.client.token.usage`（按 input/output 分维） | [OpenTelemetry: Inside the LLM Call](https://opentelemetry.io/blog/2026/genai-observability/)、[Greptime: OTel GenAI Semantic Conventions](https://www.greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions) |
| Agent 层建议 `invoke_agent` / `invoke_workflow` / `execute_tool` 三类 span；本项目的 `mission.run` / `stage.run` / `execute_tool` 已基本对齐 | 同上 |
| token 拿不到时应**省略而非估算**，否则成本看板会混入不可信数据 | OTel 规范与 Greptime 文中的两条计数规则 |

**结论**：trace 负责「为什么」，metrics 负责「多少」，eval 负责「好不好、比之前好不好」。三者不能互相替代。

### 3.1 协作评测的四类范式

| 范式 | 代表工作 | 核心做法 | 对本项目的启示 |
|---|---|---|---|
| 结果导向 | AgentArch（ServiceNow, 2025） | `Acceptable Score` 同时要求工具选对、参数正确、最终决策正确；报告 `pass@1` / `pass^k` | 阶段 `ok` 不等于协作成功；要把「工具选择 / 参数 / 决策」拆开 |
| 过程导向 | MultiAgentBench / MARBLE（ACL 2025） | 里程碑 KPI + Communication Score + Planning Score，`CS=(C+P)/2` | 用里程碑推进度衡量协作，而不是只看最终任务分 |
| 失败模式导向 | MAST / MASFT（UC Berkeley, 2025） | 151 条专家标注轨迹，归纳 14 类失败模式（3 大类） | 失败要可分类、可统计，才能定位是规范、交接还是验证的问题 |
| 协作行为导向 | CollabBench（2026） | 效率指标 + 情感指标（helpfulness / trust / empathy）；只优化效率会退化成「沉默执行者」 | 不能只奖励「快」；要单独度量沟通质量与主动性 |

关键数据点：

- MultiAgentBench：6 个场景 × 100 用例；里程碑 KPI 按 `n_j/M` 计算再对 agent 取平均；graph 拓扑在研究场景最佳；认知规划让里程碑达成率 +3%；迭代 10 轮时 CS 反而下降，说明「交互越多越好」不成立。
- MAST：14 类失败模式，人类标注一致性 κ=0.88；few-shot o1 自动标注 F1=0.80，可作为分诊工具但不能替代人工金标。
- AgentArch：最高成功率仅 70.8%（简单任务）/ 35.3%（复杂任务）；`pass^8` 峰值 6.34%，说明单次成功率会严重高估可靠性。
- CollabBench：只用效率奖励训练，沟通频率会崩塌；必须同时优化「任务工作」与「团队合作」。

协作评测的实验设计（从上述工作归纳）：

1. **单 agent 基线**：同工具、同上下文，比较多 agent 的净增益；没有基线就无法判断「协作是否值得」。
2. **消融**：去掉一个角色 / 换协调拓扑 / 关闭规划，测任务分与协作分的变化（MultiAgentBench 的 star/chain/tree/graph 对比即此思路）。
3. **反事实归因**：把某个 agent 的输出替换为金标，观察下游是否变好；用于判断「这个角色是否真的贡献了价值」。
4. **重复运行**：N=3-8，报 `pass@1` / `pass^k`；AgentArch 的 `pass^8` 仅 6.34%，单次成功率会严重高估可靠性。
5. **成本核算**：token 乘数 = 轮数 × 参与 agent 数；协调开销占比 = 协调 token / 总 token；并行节省的墙钟时间要减去通信开销。
6. **人工校准**：小样本人工评分，与 judge 计算 kappa / 相关性；MASFT 的 κ=0.88 是可接受的下限参考。

### 3.2 数据集采集的三条来源

| 来源 | 做法 | 优点 | 代价 |
|---|---|---|---|
| 生产 trace | 每次运行自动采集 span + 消息 + 产物，按周采样 | 分布真实、覆盖长尾 | 噪声大、需要脱敏与标注 |
| 基准 suite | 固定任务集 + 固定环境（如 MultiAgentBench 每场景 100 用例） | 可复现、可对比 | 与真实业务有分布差 |
| 专家标注失败集 | 从生产/基准中抽样，按失败分类法人工标注（如 MASFT 151 条） | 诊断价值最高 | 标注成本高、一致性难保证 |

通用流水线（Overmind 2026 的实践，与 Langfuse/LangSmith 的 dataset 模式一致）：

1. **Capture**：一次运行 = 一条 trace，包含目标、每次模型/工具调用、环境观察、最终结果。
2. **Sample**：随机（基线）+ 分层（关键切片）+ 失败加权（信息量最大）；不要标注全量。
3. **Label**：标注准则从代码/契约推导，写成加权 yes/no 清单；LLM judge 做第一遍，人工抽检校准。
4. **Build**：每行保留 `source_trace_id` 与目标/结果；按 **run** 而不是按 step 切分，防止评测集泄漏到训练集。
5. **Version**：数据集带版本、里程碑定义版本、judge 版本；每周重建。

### 3.3 可视化实现的三条路线

| 路线 | 代表产品 | 数据模型 | 擅长 | 局限 |
|---|---|---|---|---|
| 时间线 / 瀑布 | Jaeger、Tempo、LangSmith、Langfuse、Phoenix | span 树 + 时间戳 | 延迟、token、单次回放 | 因果关系埋在 JSON 里，跨 trace 查询弱 |
| 图 / 状态机 | LangGraph Studio、AutoGen Studio、OpenInference `graph.node.*` | 节点 + 边 + 状态快照 | 循环、死路、分支、交接拓扑 | 生产规模下需要额外存储 |
| 图数据库 / 因果图 | Neo4j + 自建（如 GraphEvals） | 显式 `INFLUENCED_BY` / `PROVIDED_CONTEXT` 边 | 多跳因果查询、跨运行聚合 | 自建与运维成本高 |

关键判断：

- **时间线回答「何时」，图回答「为何」**。两者都需要，不能用一个替代另一个。
- LangGraph Studio 的单条 trace 调试最快（文章实测 15 分钟），但跨 trace 批量查询需要导出脚本；Neo4j 方案批量查询 <5 秒，但单条排查更慢（40 分钟）。
- 对本项目：**先用 OTel span + `graph.node.parent_id` 生成图，不引入图数据库**；当每天 >1000 条任务且需要多跳因果查询时再考虑 Neo4j。

### 3.4 调研来源

| 主题 | 来源 | 用到的结论 |
|---|---|---|
| 协作 benchmark | [MultiAgentBench, ACL 2025](https://aclanthology.org/2025.acl-long.421)、[MARBLE](https://github.com/ulab-uiuc/MARBLE) | 里程碑 KPI、Communication / Planning Score、6 场景 × 100 用例 |
| 失败分类 | [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657)、[MASFT 数据集](https://github.com/multi-agent-systems-failure-taxonomy/MASFT) | 14 类失败模式、151 条标注轨迹、κ=0.88、o1 few-shot F1=0.80 |
| 架构评测 | [AgentArch, 2025](https://arxiv.org/abs/2509.10769) | Acceptable Score、pass@1 / pass^k、工具选择/参数/决策拆解 |
| 协作行为 | [CollabBench, 2026](https://arxiv.org/abs/2606.05793) | 效率 + 情感双轴、只优化效率会退化为「沉默执行者」 |
| 数据模型 | [Langfuse Data Model](https://www.langfuse.com/docs/observability/data-model)、[Langfuse Agent Observability](https://langfuse.com/blog/2024-07-ai-agent-observability-with-langfuse) | Session → Trace → Observation、OTel 底座、属性下推 |
| 数据集构建 | [Overmind: Traces to Dataset](https://www.overmindlab.ai/research/traces-to-training-dataset) | Capture → Sample → Label → Build、按 run 切分、provenance |
| 语义约定 | [OpenInference Semantic Conventions](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md)、[OTel GenAI](https://opentelemetry.io/blog/2026/genai-observability/)、[Greptime 解读](https://www.greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions) | `AGENT` / `TOOL` / `LLM` span、`graph.node.id/parent_id`、`gen_ai.client.*` 指标 |
| 可视化 | [LangGraph Studio](https://docs.langchain.com/oss/python/langgraph/studio)、[AutoGen Studio](https://microsoft.github.io/autogen/stable/user-guide/autogenstudio-user-guide/index.html)、[Grafana Agent 观测](https://grafana.com/blog/observing-agentic-ai-workflows-with-grafana-cloud-opentelemetry-and-the-openai-agents-sdk) | 图 + 状态检查 + 时间旅行；控制转移图；TraceQL / 指标看板 |
| 因果可视化 | [5 款 Agent 可观测工具实测](https://www.binarybox.org/p/i-tested-5-agent-observability-tools)、[可视化 Agent 轨迹](https://blog.redlinesoft.net/posts/visualizing-agent-trajectories) | 时间线回答 when、图回答 why；SCC 循环 / 死路 / 重复调用 / 上下文膨胀 |

## 4. 评测模型

### 4.1 四层指标

**L1 效率（Efficiency）**：时间、token、成本、工具调用、并行收益。
**L2 结果（Outcome）**：任务成功率、阶段成功率、首次通过率、契约完整率、测试/MR/人工返工。
**L3 协作（Collaboration）**：里程碑推进、沟通质量、规划质量、交接完整性、角色遵守、负载均衡、冲突解决。
**L4 过程（Process）**：HITL 介入、重试、降级、工具失败、召回命中、失败模式分布。

协作层是 v2 新增的核心：它解释「为什么结果好/坏」，但不能单独当成功率使用。

判定顺序固定为：**L2 结果 → L3 协作 → L4 过程**。先回答「任务是否完成」，再回答「协作是否有效」，最后回答「哪个环节出了问题」；不允许用高协作分掩盖任务失败。

| 任务结果 | 协作分 | 解读 | 优先动作 |
|---|---|---|---|
| 成功 | 高 | 健康 | 记录为基线，扩大样本 |
| 成功 | 低 | 靠个别 agent 硬扛，协作是瓶颈 | 查 handoff、负载均衡、沟通开销 |
| 失败 | 高 | 协作过程没问题，方向或规范错 | 查 brief、里程碑、验收标准 |
| 失败 | 低 | 协作机制失效 | 查失败模式分布，先修交接与验证 |

### 4.2 结果与效率指标

| 指标 | 定义 | 数据源 | 现在能否算 |
|---|---|---|---|
| `task_success_rate` | `status=done` / 所有终态（done+failed+stopped） | `Initiative.status` | 能 |
| `end_to_end_success` | done 且无契约失败且无 `status=fail` 阶段 | timeline + contract | 部分（契约未结构化） |
| `stage_success_rate` | `StageResult.status=ok` / 阶段执行次数 | timeline 阶段事件 | 能 |
| `first_pass_yield` | 第一次尝试即 ok 的阶段 / 阶段总数 | `qa_attempt` + 阶段重复次数 | 能近似 |
| `contract_pass_rate` | 契约通过阶段 / 有契约阶段 | 需补 `contract_ok` | 否 |
| `human_rework_rate` | (`reject`+`edit_instruction`+`reroute`) / HITL 决策数 | timeline `kind=hitl` | 能 |
| `hitl_rate` | 有 HITL 的任务 / 任务数 | timeline / status | 能 |
| `mr_merge_rate` | 已合并 MR / 已创建 MR | `git_publish` + GitLab API | 否（未同步合并状态） |
| `active_duration_ms` | `mission.run` 各段时长之和（不含人工等待） | OTel span | 否（需启用 OTel） |
| `wall_duration_ms` | 首次执行到末次事件，含等待 | timeline | 能近似 |
| `hitl_wait_ms` | 等待事件到人工决策的时间 | timeline / decisions | 能近似 |
| `queue_wait_ms` | job 入队到 claim 的时间 | `run_jobs` | 否（缺 `started_at`） |
| `stage_duration_p50/p95` | 按 stage / executor / role 分组 | OTel span / 补 `duration_ms` | 否（需补） |
| `tokens_in/out_per_task` | `gen_ai.usage.input_tokens/output_tokens` 求和 | OTel span | 否（需聚合） |
| `cost_per_task` | tokens × 价格表 | 配置 + 聚合 | 否 |
| `tokens_per_success` | 总 token / 成功任务数 | 同上 | 否 |
| `tool_calls_per_stage` | `execute_tool` 次数 | span / `mcp_calls.json` | 能 |
| `tool_failure_rate` | `ok=false` 的工具调用 / 总调用 | `mcp_calls.json` | 能 |
| `duplicate_tool_call_rate` | 同 stage 内同工具同参数重复次数 / 总调用 | `mcp_calls.json` | 能 |
| `retrieval_hit_rate` | `sortie.hit_count` / `sortie.query_count` | span | 能（需 OTel） |
| `parallel_speedup` | 各分支耗时之和 / 波次墙钟 | span 时间戳 | 能近似（缺 branch 标识） |
| `handoff_denied_rate` | denied / (accepted+denied) | timeline `kind=transition` | 能 |
| `degraded_rate` | 走 fallback 的调用 / 总调用 | 需补 `sortie.degraded` | 否 |
| `judge_score` | rubric 0-1 | 离线 LLM judge | 否 |
| `human_score` | 人工 1-5 分 | UI 标注 | 否 |

### 4.3 协作质量指标

| 指标 | 定义 | 数据源 | 现在能否算 |
|---|---|---|---|
| `milestone_achievement_rate` | 已达成里程碑 / 里程碑总数 | 新增 milestones + 事件 | 否 |
| `agent_kpi_j` | `n_j / M`，第 j 个 agent 贡献的里程碑数 / 总数 | 阶段 / 交接 / 产物归因 | 否 |
| `overall_kpi` | `(1/N) Σ agent_kpi_j` | 同上 | 否 |
| `communication_score` | 1-5 分；清晰度、及时性、信息完整度 | 消息 + LLM judge | 否 |
| `planning_score` | 1-5 分；任务分解、角色维持、策略自适应 | 阶段计划 + LLM judge | 否 |
| `coordination_score` | `(communication_score + planning_score) / 2` | 同上 | 否 |
| `handoff_accept_rate` | accepted / (accepted + denied) | `graph.py:850-916` | 能 |
| `handoff_completeness` | 交接必填字段齐全度 | 新增 handoff schema | 否 |
| `role_adherence_rate` | 角色内动作 / 总动作 | 工具 + 阶段 + contract | 否 |
| `information_reuse_rate` | 下游引用上游产物/消息的比例 | 产物 + 消息 + judge | 否 |
| `redundancy_rate` | 重复工具调用 / 总调用 | `mcp_calls.json` | 能 |
| `conflict_resolution_rate` | Manager 自行解决的冲突 / 总冲突 | `manager.py` 决策 + 事件 | 能 |
| `escalation_rate` | 升级真人 / 总冲突 | `decisions.py` | 能 |
| `load_balance_gini` | 各角色 token / 工具调用的 Gini 系数 | token 归因 | 否 |
| `communication_overhead_ratio` | 协调类 token / 总 token | 消息分类 + LLM usage | 否 |
| `proactive_engagement_rate` | 主动发起澄清/同步的次数 / 阶段数 | 消息 `msg_type` + judge | 否 |

本项目的 milestone 可直接从 `contracts.REQUIRED`（`contracts.py:15-40`）推导：

`intake`（brief + 角色编制）→ `product_prd`（prd.md + acceptance.json）→ `eng_*`（implementation.md + tasks.json + diff.patch）→ `qa_verify`（test_report.json + test_cases.md）→ `deploy_preview`（deploy_plan.md + deploy_result.json）。

每条 track 只取实际存在的阶段，避免 express 轨道缺阶段导致分母虚高。

### 4.4 失败模式指标（MAST 映射）

| 类别 | 失败模式 | 本项目可观测信号 |
|---|---|---|
| FC1 规范与设计 | 不遵守任务规范、不遵守角色规范、步骤重复、丢失对话历史、未意识到终止条件 | 契约违规、角色越界、重复工具调用、上下文截断、超轮次 |
| FC2 智能体间失配 | 对话重置、未请求澄清、任务偏离、信息隐瞒、忽略他方输入、推理行动不一致 | handoff 缺失字段、下游未引用上游、manager 冲突、问答偏离 |
| FC3 验证与终止 | 过早终止、未验证/验证不完整、错误验证 | QA 未跑、契约缺失、reviewer 通过率异常、测试未执行 |

输出两个指标：

- `failure_mode_distribution`：14 类各自占比（人工标注或 few-shot judge）。
- `failure_severity`：按「是否导致任务失败 / 是否导致返工 / 是否仅增加成本」分三档。

### 4.5 口径原则

1. **成功率分母只算终态**：`running` / `waiting_hitl` / `draft` 单列，不能混入成功率。
2. **时长必须拆三段**：`active`（真正执行）、`hitl_wait`（等人）、`queue_wait`（排队）。否则「任务跑了 3 小时」没有诊断价值。
3. **trace 的 ERROR 不等于任务失败**：`observability.py:193-206` 把 `needs_hitl` 也标成 ERROR，这是「需要关注」的语义。评测的成功率只用 `StageResult.status`，不能直接用 span status。
4. **token 缺失不估算**：与 `llm_chain.py:204-209` 的既有约束一致；报告里标注 `usage_unavailable`。
5. **成本需要价格表**：默认只报 token；配置了 `model -> input/output 单价` 才折算 USD。
6. **所有指标必须分组**：至少按 `track` / `harness` / `coding_executor` / `model` / `role` 分组，否则绝对值没有意义。
7. **非确定性任务要看分布**：同一 case 跑 N 次，报 mean / p50 / p95 / pass@k，不能拿单次结果下结论。
8. **协作指标是过程指标，不是成功率**：沟通分高不代表任务成功；必须与 L2 结果指标联合看。
9. **里程碑定义必须冻结并版本化**：同一 track 的 milestone 集合变更后，历史数据不能直接比较。
10. **LLM judge 只做分诊，不做金标**：MAST 的 few-shot o1 F1 仅 0.80；关键结论要人工抽检校准，报告里附 kappa / 一致率。

## 5. 技术设计

### 5.1 架构

```
采集层（线上，只记事实）
  ├─ OTel spans（mission.run / stage.run / gen_ai.chat / execute_tool / process.exec）
  ├─ TimelineEvent（业务事件、HITL、交接、工具使用）
  ├─ RoomMessage（群聊 / 子房间 / 上下游咨询）
  └─ eval.json + trace.jsonl（每任务一份，可离线复算）
        │
        ▼
数据集层（离线，可版本化）
  ├─ dataset.py     统一 schema、去重、脱敏
  ├─ sampler.py     随机 / 分层 / 失败加权
  ├─ labels.py      人工标签 / judge 标签 / 一致性
  └─ 构建 dataset item + provenance + run 级切分
        │
        ▼
复算层（纯函数，不依赖 trace 后端）
  ├─ collector.py      Initiative + artifacts + eval.json → RunRecord
  ├─ collaboration.py  里程碑 / 交接 / 沟通 / 负载 / 失败模式
  └─ metrics.py        RunRecord → MetricValue（成功率、分位、成本、返工率…）
        │
        ▼
聚合层
  ├─ report.py     JSON / Markdown / CSV
  ├─ CLI           sortie eval report / dataset
  └─ API           /api/evaluations/summary、/{id}、/{id}/graph
        │
        ▼
实验层（离线，可回归）
  ├─ evals/suites/*.yaml   固定 case 集
  ├─ runner.py             批量跑 + 收集 + 对比 baseline
  └─ evaluators.py         Contract / Test / LLMJudge / HumanScore
        │
        ▼
可视化层（前端）
  ├─ MissionWaterfall   任务/阶段/工具/HITL 瀑布
  ├─ CollaborationGraph 角色/阶段/交接/消息拓扑
  ├─ MetricsDashboard   成功率、p50/p95、token、成本、返工
  └─ FailureGraph       循环、死路、重复调用、失败模式
```

设计原则：

1. **评测不阻塞主链路**：采集只记录原始事实，指标计算全部离线。
2. **可离线复算**：`eval.json` 落 `data/missions/<id>/evaluation/run.json`（与 `layout.py:5-12` 的任务目录一致），不依赖 Jaeger/Tempo；`eval.json` 仅作为逻辑名，实际文件统一放 `evaluation/` 子目录。
3. **单一指标实现**：CLI、API、报告共用 `evaluation/metrics.py`，避免三套口径。
4. **先确定性、后 LLM judge**：契约、测试退出码、MR 状态优先；语义质量才用 judge。
5. **不改领域模型**：评测明细放 `evaluation/run.json`，`ini.meta["evaluation"]` 只存摘要与文件指针（避免把大 JSON 塞进任务元数据），不破坏 `StageResult` / `TimelineEvent` 的既有契约。

### 5.2 新增模块

```
src/sortie_deck/evaluation/
├── __init__.py
├── schema.py             # RunRecord / StageRecord / MessageRecord / EdgeRecord / MetricValue
├── collector.py          # Initiative + timeline + room + artifacts → RunRecord
├── milestones.py         # track → milestone 定义与达成判定
├── collaboration.py      # 里程碑 KPI、交接、沟通、负载、冲突指标
├── failure_taxonomy.py   # MAST 14 类映射与 judge prompt
├── metrics.py            # 效率 / 结果 / 过程指标纯函数
├── dataset.py            # dataset item 构建、采样、切分、版本
├── report.py             # 聚合、分位、分组、Markdown/JSON
├── graph.py              # RunRecord → 可视化 nodes/edges
├── evaluators.py         # Contract / Test / LLMJudge / HumanScore
└── runner.py             # 离线 suite 执行与 baseline 对比

apps/web/src/components/evaluation/
├── EvaluationPage.tsx
├── MissionWaterfall.tsx
├── CollaborationGraph.tsx
├── MetricsDashboard.tsx
└── FailureGraph.tsx

evals/
├── suites/smoke.yaml      # mock，确定性，CI 门禁
├── suites/coding.yaml     # 真实仓库 + 测试命令
└── cases/*.yaml           # brief / track / harness / repos / expected
```

### 5.3 采集点改动清单

| 文件:行 | 改动 | 目的 |
|---|---|---|
| `graph.py:1225` | `_run_stage` 入口记 `started = time.monotonic()` | 阶段耗时 |
| `graph.py:1552` | publish 后写 `result.meta["duration_ms"]` | 随阶段事件带出，OTel 关闭也可算 |
| `graph.py:1209-1218` | stage span 加 `sortie.parallel_branch` | 并行分支归属 |
| `graph.py:1549` | 契约校验包 `try`，先写 `contract_ok` / `missing` 再抛 | 契约失败可归因 |
| `graph.py:1556-1573` | 阶段事件 `data` 补 `duration_ms` / `contract_ok` / `degraded` | 离线复算 |
| `graph.py:1945-2016` | HITL gate 事件写 `requested_at` | 等待时长 |
| `orchestrator.py:2003-2011` | `submit_hitl` 计算 `wait_ms` 写入决策事件 | HITL 等待 |
| `orchestrator.py:1854` | `mission.run` span 加 `sortie.segment`（`ini.meta["run_segment"]` 递增） | 区分多次 start/resume |
| `orchestrator.py:1800-1833` | 收尾调用 `evaluation.collector.write_eval_json(ini)` | 每任务留档 |
| `run_queue.py:30-55` | `run_jobs` 加 `started_at REAL`；`claim()` 写值 | 队列等待 |
| `run_queue.py:77-135` | claim 时记录 `started_at` | 同上 |
| `observability.py` | P1 增加 MeterProvider + OTLP metric exporter，注册 4 个指标 | 趋势看板 |
| `settings.py:210-222` | 加 `eval_*` 配置（见 5.6） | 开关与价格表 |
| `api/app.py:464` | 新增 `/api/evaluations/*` 路由 | 查询与看板 |
| `cli.py:78` | 新增 `sortie eval report` / `sortie eval run` / `sortie eval dataset` | 本地与 CI |
| `rooms.py:49-53` | 在 `RoomMessage.meta` 写 `reply_to` / `refs` / `kind` | 消息级协作边 |
| `agent_contract.py:40-55` | 增加 `handoff_required_fields`，交接时校验 | 交接完整性 |
| `graph.py:862-916` | handoff 事件 `data` 补 payload 字段齐全度 | 协作评测 |
| `manager.py:85-98` | 决策事件补 `confidence` / `trigger` / `resolution`（已有，落 eval） | 冲突解决 |
| `observability.py:161-248` | span 加 `graph.node.id` / `graph.node.parent_id` / `session.id` / `agent.name` | 直接生成协作图 |
| `api/app.py` | `/api/evaluations/{id}/graph` / `timeline` | 可视化数据 |
| `apps/web/src/api.ts` | 新增 evaluation API 客户端 | 前端 |
| `apps/web/src/components/evaluation/` | 瀑布 / 拓扑 / 看板 / 失败归因四个视图 | 可视化 |

注意：`run_jobs` 是既有 SQLite 表，新增 `started_at` 不能只改 `CREATE TABLE IF NOT EXISTS`——老库不会自动补列。需要先 `PRAGMA table_info(run_jobs)` 检查，缺失时幂等执行 `ALTER TABLE run_jobs ADD COLUMN started_at REAL`。

### 5.4 `eval.json` schema

> 说明：`eval.json` 是逻辑名；实际落盘为 `data/missions/<id>/evaluation/run.json`（见 5.8），避免与任务根目录下的其他文件混放。

```json
{
  "initiative_id": "ini_xxx",
  "track": "default",
  "harness": "claude_code",
  "coding_executor": "claude_code",
  "model": "gpt-4o-mini",
  "status": "done",
  "created_at": "2026-10-27T08:00:00Z",
  "finished_at": "2026-10-27T08:42:11Z",
  "segments": 3,
  "duration": {
    "active_ms": 1520000,
    "hitl_wait_ms": 900000,
    "queue_wait_ms": 12000
  },
  "stages": [
    {
      "stage": "product_prd",
      "role": "product",
      "executor": "mock_product",
      "status": "ok",
      "attempt": 1,
      "duration_ms": 42000,
      "tokens_in": 1200,
      "tokens_out": 800,
      "contract_ok": true,
      "missing": [],
      "tool_calls": 0,
      "tool_errors": 0,
      "degraded": false
    }
  ],
  "hitl": {
    "count": 3,
    "actions": {"approve": 2, "edit_instruction": 1},
    "rework": 1
  },
  "artifacts": {
    "count": 14,
    "bytes": 382000,
    "diff_lines": 512
  },
  "git": {
    "pushed": 1,
    "mr_created": 1,
    "mr_merged": null
  },
  "collaboration": {
    "milestones_total": 6,
    "milestones_done": 5,
    "coordination_score": 4.1,
    "handoff": {"accepted": 7, "denied": 1, "payload_complete": 6},
    "messages": {"total": 42, "coordination_tokens": 1800},
    "load_balance_gini": 0.28,
    "failure_modes": {"step_repetition": 2}
  },
  "usage_unavailable": false
}
```

说明：

- `mr_merged` 为 `null` 表示未同步，不能当成 `false`。
- `usage_unavailable=true` 表示该任务有 CLI 调用拿不到 token，成本指标应剔除。
- `degraded` 来自新增埋点；当前只有 LLM 降级可间接从日志看出。
- `collaboration` 段由 5.8 的协作模块填充；未启用 judge 时 `coordination_score` 为 `null`，不能按 0 处理。

### 5.5 离线评测 suite

**smoke（CI 每次跑）**

- 3-5 个 mock brief，固定 `harness=mock`、`coding_executor=mock`。
- 断言：任务 `done`、阶段顺序符合模板、契约全通过、无意外 HITL、`duration_ms < 阈值`。
- 目标：防止编排逻辑回归，不评模型质量。

**coding（夜间 / 手动）**

- 固定仓库 + 固定测试命令（如 `pytest -q` / `npm test`）。
- 断言：测试退出码 0、`diff.patch` 非空、`tasks.json.files_written >= N`、MR 创建。
- 目标：评真实编码能力，数据来源是 `plugins/coding.py:125-161` 的 `process.exec` 与 `tasks.json`。

**regression（PR 门禁）**

- 同一 suite 跑 baseline 与 candidate，比较成功率、p50/p95、token、成本。
- 阈值：成功率不得下降超过 X%，p95 不得上升超过 Y%。
- 输出 Markdown 对比表，失败阻断合并。

**experiment（A/B）**

- 同一批 case，切换 harness / model / prompt / 并行策略。
- 每 case 重复 N 次（建议 3-5），报 mean / p50 / p95 / pass@k。
- 这是回答「换模型值不值」的唯一可靠方式。

### 5.6 配置项

在 `settings.py:210` 附近新增：

```python
# --- 评测（见 docs/plans/2026-10-27-task-execution-evaluation.md）---
eval_enabled: bool = True              # 是否在任务收尾写 eval.json
eval_judge_enabled: bool = False       # 离线 LLM judge 总开关
eval_judge_model: str = ""             # 空 = 复用 TDT_LLM_MODEL
eval_price_table: str = ""             # JSON: {"gpt-4o-mini": {"input": 0.15, "output": 0.6}}
eval_baseline_path: str = ""           # 回归对比 baseline JSON
```

价格单位统一为 **USD / 1M tokens**，避免小数精度问题。

### 5.7 API 与 CLI

```
GET  /api/evaluations/summary?since=30d&group_by=track,harness
GET  /api/evaluations/{initiative_id}
POST /api/evaluations/compare        # body: baseline_id, candidate_id
```

```
sortie eval report --since 30d --group-by track,harness --out report.md
sortie eval run --suite smoke --repeat 1 --fail-under 0.9
sortie eval compare --baseline baseline.json --candidate current.json
```

读接口复用现有 `require_user`，不新增鉴权模型。

### 5.8 数据集采集设计

**四层数据模型**（对齐 Langfuse 的 Session → Trace → Observation，以及 OpenInference 的 `graph.node.*`）：

| 层 | 本项目实体 | 作用 |
|---|---|---|
| Session | `Initiative` | 一次任务，跨多次 start/resume |
| Trace | `mission.run` segment | 一次连续执行（不含 HITL 等待） |
| Observation | `StageResult` / `stage.run` / `gen_ai.chat` / `execute_tool` / `process.exec` | 阶段、LLM、工具、子进程 |
| Event | `TimelineEvent` / `RoomMessage` / `HitlDecision` | 消息、交接、HITL、决策 |

**采集点映射**：

| 数据 | 现有位置 | 采集方式 |
|---|---|---|
| 任务元数据 | `models.py:312-342` | 收尾写 `run.json` |
| 阶段结果 / 耗时 | `graph.py:1556-1573` | 阶段事件 + `duration_ms` |
| 交接 | `graph.py:850-916` | `kind=transition` 事件 |
| HITL | `orchestrator.py:1973-2011` | `kind=hitl` 事件 + `wait_ms` |
| 消息 | `rooms.py:49-53`、`subroom.py` | `RoomMessage` → `message.jsonl` |
| 工具调用 | `graph.py:1025-1054` | `mcp_calls.json` + `execute_tool` span |
| LLM 调用 | `llm_chain.py:179-202` | `gen_ai.chat` span |
| 子进程 | `plugins/coding.py:125-161` | `process.exec` span |
| 产物 | `artifacts.py:71-119` | `ArtifactRef` + 文件 hash / 大小 |
| 经理决策 | `manager.py:85-98` | `kind=transition` 事件 `data` |
| 真人决策 | `decisions.py:41-73` | `HumanDecision` 记录 |

**数据集 item schema**：

```json
{
  "item_id": "ds_xxx",
  "dataset": "golden/coding",
  "version": "2026-10-27",
  "run_id": "ini_xxx",
  "input": {"brief": "...", "track": "standard", "harness": "claude_code", "repos": ["..."]},
  "expected": {"status": "done", "required_artifacts": ["prd.md", "diff.patch"], "tests": "pytest -q"},
  "trace_ref": "data/missions/ini_xxx/evaluation/trace.jsonl",
  "labels": {"task_success": true, "collaboration_score": 4.2, "failure_modes": []},
  "metrics": {"active_ms": 1520000, "tokens_in": 12000, "cost_usd": 0.08},
  "provenance": {"source": "production", "captured_at": "...", "git_sha": "..."}
}
```

**存储布局**：

```
data/missions/<initiative_id>/evaluation/
├── run.json          # 5.4 的 eval.json + collaboration 段
├── trace.jsonl       # 规范化后的 span / event / message
├── labels.json       # 自动 + 人工标签
└── artifacts.json    # 产物清单、hash、大小

data/eval/datasets/<name>/<version>/
├── manifest.json     # 数据集版本、milestone 版本、judge 版本
├── items.jsonl
└── labels.jsonl
```

**采样与切分**：

- 随机 10% 作为基线；分层覆盖 track / harness / role；失败与 HITL 全量保留。
- 按 `run_id` 切分 train / eval / test，同一条 trace 的所有 step 只落一边，防止泄漏。
- 数据集版本与 milestone 版本、judge 版本绑定；变更后旧基线不可直接比较。

**质量控制**：

- 标注准则优先从 `contracts.REQUIRED`、`AgentContract`、执行器判定逻辑推导，减少主观。
- 人工一致性目标 Cohen's κ ≥ 0.8（MASFT 报告 0.88）；低于阈值先修准则，不扩样本。
- LLM judge 只做分诊；每批次抽 5-10% 人工复核，记录一致率。

### 5.9 可视化实现

**四个视图，不做更多**：

| 视图 | 回答的问题 | 图形 | 数据 |
|---|---|---|---|
| 任务瀑布 | 时间花在哪 | 甘特/瀑布：x=时间，y=阶段/角色；条=stage；点=HITL/工具/LLM | span + `duration_ms` |
| 协作拓扑 | 谁和谁协作、是否有效 | 节点=角色/阶段；边=handoff/message；粗细=次数；颜色=成功/拒绝 | handoff + RoomMessage + graph.node |
| 指标看板 | 整体是否变好 | 折线/柱状/热力图：成功率、p50/p95、token、成本、返工率 | metrics 聚合 |
| 失败归因 | 为什么失败 | 图：环=SCC、死路=出度 0、重复=同参调用、上下文膨胀=节点大小 | trace graph + MAST 标签 |

**后端接口**：

```
GET /api/evaluations/{id}/timeline   # 瀑布/甘特
GET /api/evaluations/{id}/graph      # nodes / edges
GET /api/evaluations/summary         # 聚合指标
GET /api/evaluations/compare         # baseline vs candidate
```

**图数据模型**：

```json
{
  "nodes": [
    {"id": "stage:product_prd", "kind": "stage", "label": "PRD", "role": "product",
     "start_ms": 0, "duration_ms": 42000, "status": "ok", "tokens": 2000}
  ],
  "edges": [
    {"from": "stage:product_prd", "to": "stage:eng_implement", "kind": "handoff",
     "accepted": true, "payload_complete": true, "count": 1}
  ]
}
```

**前端技术选型**：

当前 `apps/web/package.json` 只有 React / react-markdown / three，没有图表或图库。P3 需要新增依赖，建议：

| 用途 | 选型 | 体积 / 说明 |
|---|---|---|
| 协作拓扑 | `reactflow` | ~50KB gzip；节点/边、缩放、minimap、自定义节点；适合 50-500 节点 |
| 甘特 / 热力图 / 看板 | `echarts` + `echarts-for-react` | ~100KB gzip（按需引入）；不要用 DOM 逐条渲染 |
| 轻量折线 | 现有 SVG 或 `echarts` | 若只看 3-5 条指标，可手绘 SVG，避免引入整个图表库 |

- 两个库都用 `React.lazy` + 路由级 code split，避免拖慢首屏。
- 页面入口：`apps/web/src/App.tsx:144` 的 `view` 状态新增 `"eval"`，在 `:2621` 的 `main-nav` 加入口；组件放 `apps/web/src/components/evaluation/`。
- 复用 `apps/web/src/api.ts`、`types.ts`、`i18n.ts`；新增文案中英双语。
- 实时：复用现有 SSE（`orchestrator._emit`）；历史：读 `evaluation/run.json` / trace。
- 若不允许新增依赖：退化为 SVG 手绘，瀑布与拓扑可做，但缩放/拖拽/筛选需要自己实现，工作量约翻倍。

**实现顺序**：

1. 先做静态瀑布 + 指标看板（P3，数据已够）。
2. 再做协作拓扑（需要 handoff + message 边）。
3. 最后做失败归因图（需要 SCC / 重复调用检测 + MAST 标签）。

**规模策略**：

- 单任务：全量图。
- 跨任务：只画聚合边热力图（角色→角色、阶段→阶段），不画单条消息。
- 因果查询：v1 不做图数据库；当每天 >1000 任务且需要多跳查询时，再把 `eval.json` 的边双写到 Neo4j。

## 6. 与现有 OTel 方案的关系

| 层 | 现有 OTel 方案 | 本方案 |
|---|---|---|
| 事实采集 | span 树 + traceparent + 日志 trace_id | 复用，不重复埋点 |
| 指标 | P3「可选」提到 metrics | 提升为 P1 必做，按 GenAI 语义约定命名 |
| 聚合 | 无 | `evaluation/metrics.py` + report |
| 实验 | 无 | `evals/` + runner + evaluator |
| 业务关联 | `TimelineEvent.trace_id`（`models.py:252`、`orchestrator.py:480-486`） | 直接复用 |
| 图关系 | 无 | span 加 `graph.node.id` / `graph.node.parent_id`，与 OpenInference 对齐 |
| 会话归组 | `sortie.initiative_id` | 同时写 `session.id`，对齐 Langfuse / OpenInference |

多 agent 层级不靠新字段：OpenInference 用 `AGENT` / `CHAIN` span + `graph.node.*` + 父子关系表达；本项目 `stage.run` 相当于 `AGENT`，`execute_tool` 相当于 `TOOL`，`mission.run` 相当于 `CHAIN`。

一句话：**OTel 是「事实层」，本方案是「度量层 + 数据集层 + 实验层 + 可视化层」**。`eval.json` 同时是 trace 的降级备份——OTel 关闭时仍能评测。

## 7. 备选方案

| 方案 | 优点 | 不采用的原因 / 适用场景 |
|---|---|---|
| 直接接 Langfuse / LangSmith | 开箱即用的 dataset / experiment / judge | 需要外发数据；本项目 LLM 不走 LangChain 模型对象，coding CLI 更不可见；可作为导出后端，不作为唯一方案 |
| 只做 OTel + Grafana | 趋势和告警成熟 | 能看「多少」，不能回答「结果对不对」；缺 golden set 与回归 |
| 只做 LLM-as-judge | 覆盖语义质量 | 成本高、不稳定、不可解释；不能替代契约与测试 |
| 只做 pytest / CI | 确定性最强 | 只覆盖代码正确性，不覆盖编排效率、人工返工、成本 |
| 自建轻量 evaluation 模块 | 口径可控、可离线、无外部依赖 | 需要维护指标定义；这是本方案的选择 |

## 8. 分阶段实施

| 阶段 | 内容 | 验收 | 工作量 |
|---|---|---|---|
| **P0** | `evaluation/` 模块 + `eval.json` + `sortie eval report` + 现有数据能出的指标（含协作基线：handoff 接受率、工具重复率、HITL 返工率） | 对历史任务生成报告，能按 track / harness 分组 | 2-3 天 |
| **P1** | 补 duration / segment / parallel_branch / contract_ok / hitl_wait / queue_wait；里程碑定义、handoff schema、消息边；OTel metrics + `graph.node.*` | 报告含 active / wait / queue 三段；能算里程碑 KPI 与交接完整性 | 3-4 天 |
| **P2** | `evaluation/dataset.py` + 采样/切分/版本 + `evals/` suite + 确定性 evaluator + baseline 对比 | 能构建 golden set，PR 因成功率或 p95 回退被阻断 | 3-5 天 |
| **P3** | 可视化四视图（瀑布/拓扑/看板/失败归因）+ API + 前端 | 5 分钟内定位一次失败任务的根因 | 4-6 天 |
| **P4（可选）** | MAST few-shot judge + 人工评分 + MR 合并率同步 + Neo4j 因果图 | 失败模式分布可统计，跨运行因果可查 | 3-5 天 |

P0 不依赖 OTel，先解决「有没有」；P1 解决「准不准」；P2 解决「能不能防回归」；P3 解决「能不能快速定位」。

## 9. 风险与盲区

| 风险 | 缓解 |
|---|---|
| coding CLI 内部 LLM token 不可见 | 报告标 `usage_unavailable=true`；成本只覆盖 API 模式；需要精确成本时改用 API harness |
| Agent 非确定性，单次结果不可比 | 每 case 重复 N 次，报分布与 pass@k；不拿单次结果下结论 |
| 生产 trace 采样导致数据缺失 | 评测数据不走采样；trace 用 ParentBased + Collector tail sampling，错误/HITL 全留 |
| 指标被「刷」：成功率高但质量差 | outcome + process + human 三层同时看；judge 与人工评分只做补充 |
| HITL 等待污染时长 | 强制拆 active / hitl_wait / queue_wait，报告默认展示三段 |
| 评测本身成本高 | mock 优先；真实模型夜间跑；judge 只采样争议样本 |
| 数据量小，均值无意义 | 报 p50/p95 与 bootstrap 置信区间；样本不足时标注 |
| 隐私 | 不记录 prompt 全文；judge 输入脱敏；沿用 `otel_capture_content=false` 默认 |
| 评测与业务耦合过深 | 采集只写事实；所有指标计算离线；`eval_enabled=false` 时零行为变化 |
| 协作指标被「刷」：消息多不等于协作好 | 同时看 outcome、handoff 完整性、信息复用率；不以消息数量单独评分 |
| LLM judge 一致性不足 | 先用人工标注校准；报告附 kappa / 一致率；judge 只做分诊 |
| 里程碑定义漂移 | milestone 版本化；变更后不跨版本比较 |
| 数据集泄漏 | 按 run 切分；每行保留 `source_trace_id`；训练/评测分仓 |
| 可视化变成「漂亮的图但不可查」 | 图必须由结构化边生成；先保证 `graph.node.*` 与 handoff 边可查，再做 UI |
| 图数据库成本 | v1 不引入 Neo4j；用 JSON + 前端图库；超过 1000 任务/天再评估 |
| 标注隐私 | 默认不采集 prompt 全文；消息脱敏；数据集访问受控 |

## 10. 状态

- [x] 现状核查（效率 / 结果 / 协作数据缺口）
- [x] 业内调研（MultiAgentBench / MAST / AgentArch / CollabBench）
- [x] 数据集调研（Langfuse / Overmind / MASFT）
- [x] 可视化调研（LangGraph Studio / AutoGen Studio / OpenInference / Grafana）
- [x] 方案设计 v2（协作指标 + 数据集 + 可视化）
- [ ] 用户确认
- [ ] P0 实施（metrics + eval.json + report）
- [ ] P1 实施（协作埋点 + 数据集采集）
- [ ] P2 实施（suite + baseline）
- [ ] P3 实施（四视图可视化）
