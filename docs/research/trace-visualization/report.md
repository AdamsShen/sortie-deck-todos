# 全链路 Trace 如何可视化：业界实现调研

> 调研日期：2026-10-27
> 调研范围：A 通用分布式追踪 UI（6 项）+ B LLM/Agent 专用平台（6 项）
> 背景：Sortie Deck 已完成 OpenTelemetry 全链路接入，现在要决定这些 span 用什么界面看

---

## 一、TL;DR

1. **"能看"和"看得懂"是两件事。** 12 个候选里，能把 trace 画出来的有 12 个；能原生渲染 LLM 语义（token、prompt、成本）的只有 6 个（B 组）；能把**多 agent 协作**表达清楚的，**0 个做到位**。
2. **通用 UI 里 Jaeger 是零成本验证的最优解**，Grafana Tempo 是长期主力（生态最全、可嵌入），Uptrace 是资源最省的备选。Zipkin 已进入维护状态，新项目不要选。Helicone 同理（2026-03 被收购后维护模式）。
3. **本项目的落点不在"选哪个平台"，而在"怎么嵌进工作台"。** 标准工具都是"外部系统 + 跳转"，而 Sortie Deck 是控制平面，用户的心智是「打开任务页就该看到这次出击的完整执行」。
4. **建议路径：Jaeger 深链（0.5 天）→ 任务页内嵌轻量瀑布图（2–3 天）→ 控制平面专有视图（持续）。** 不建议一上来部署 Grafana + Tempo，那是运维成本换 UI 美观，当前阶段不划算。

---

## 二、关键结论

### 2.1 三类方案的本质差异

| 维度 | 通用追踪 UI（Jaeger/Tempo/SigNoz） | LLM 专用平台（LangSmith/Langfuse/Phoenix） | 网关型（Helicone） |
|---|---|---|---|
| 数据模型 | span 树 | trace + observation + session | 单条请求 |
| 对 `gen_ai.*` | 当普通属性罗列 | 一等公民，原生渲染 prompt/token/成本 | 能看到 prompt/completion，但无结构 |
| 强项 | 检索、时序分析、跨服务拓扑、成本低 | LLM 语义、eval 闭环、会话回放 | 一行接入，成本看板 |
| 弱项 | 要自己解读 agent 含义 | 需要改埋点/依赖 SDK、数据出厂 | 没有 span 层级，做不了排障 |
| 适合 | 基础设施排障 | 迭代 prompt / 评估质量 | 只想看花费 |

### 2.2 "拓扑图" 不等于 "因果链"

这是本次调研最重要的发现。

多 agent 系统的核心失败模式是**级联故障**：Agent C 因为 Agent B 传了脏数据而报错，脏数据来自 Agent A 失败的工具调用。此时主流工具给你的是**三份互不关联的错误**，而不是一条因果链。

各家能力边界：

| 能力 | 代表 | 边界 |
|---|---|---|
| 单 agent 内部追踪 | Langfuse、LangSmith、Phoenix、AgentOps | 已商品化，做得都很好 |
| 失败聚类 | Braintrust（Topic 聚类） | 能分组相似失败，聚不到因果链上 |
| 服务拓扑 | Datadog 决策路径图、Splunk 工作流映射、SkyWalking 3D 图 | 告诉你"谁连谁"，不告诉你"这次故障沿哪条边传播、每一跳被如何变形" |
| 单 agent 内递归模式 | Phoenix（trajectory mapping） | 出不了 agent 边界 |
| 委托树 + 回放 | AgentOps | 最接近，但"回放"是按记录重放视图，不是确定性重执行 |

OWASP 已在 2025-12 把级联故障列为 **ASI08**（单个故障在自主 agent 之间传播并放大为系统性危害），推荐缓解措施里明确写了 "comprehensive observability with automated cascade pattern detection"。**这是一块全行业都没填上的空白** —— 也意味着不要指望买个平台就解决。

### 2.3 可靠性数学解释了为什么这件事重要

10 个 agent 各自 95% 可靠，系统整体只有 `0.95^10 ≈ 60%`。出错的那 40% 才是工具真正要发挥价值的地方，而这部分恰恰是当前可视化最薄弱的环节。

---

## 三、横向对比矩阵

### A 组：通用分布式追踪 UI

| | Jaeger | Tempo + Grafana | Uptrace | SigNoz | SkyWalking | Zipkin |
|---|---|---|---|---|---|---|
| License | Apache-2.0 | AGPL-3.0 | AGPL-3.0 | MIT/Apache | Apache-2.0 | Apache-2.0 |
| 原生 OTLP | 是 | 是 | 是 | 是 | 部分 | 是 |
| 最小部署 | 单容器 | Grafana+Tempo+对象存储 | 单二进制+CH | 多组件+CH | OAP+存储 | 单 JAR |
| 瀑布图 | 是 | 是 | 是 | 是（+火焰图） | 是 | 是 |
| 拓扑图 | 依赖图 | Service structure | Service graph | Service map | 3D 基础设施图 | 依赖图 |
| 查询语言 | 无（字段过滤） | TraceQL | 构造器 | 构造器 | OAL | 无 |
| RED 指标入口 | SPM Monitor tab | Traces Drilldown（GA） | 是 | 是 | 是 | 无 |
| 日志联动 | 无 | 强（Loki + exemplar） | 是 | 强 | 是 | 无 |
| 可嵌入 | 无 | 强（iframe） | 无 | 无 | 无 | 无 |
| 资源占用 | 极低 | 中 | 低 | 中高 | 中高 | 低 |
| 现状 | 活跃 | 活跃（3.0 新架构） | 活跃 | 活跃 | 活跃（新 Horizon UI） | 维护模式 |

### B 组：LLM / Agent 专用平台

| | LangSmith | Langfuse | Phoenix | AgentOps | Braintrust | Helicone |
|---|---|---|---|---|---|---|
| License | 闭源 | MIT | Apache-2.0 | SDK 开源 | 闭源 | Apache（部分） |
| 自托管 | 仅企业 | 全功能免费 | 是 | 否 | 仅数据面 | 是 |
| OTLP 摄入 | 需 LangChain 回调 | 原生 | 最彻底 | 部分 | 部分 | 不走 OTLP |
| Trace 树 | 是（run tree） | 是 | 是 | 时间线 | 是 | 无 |
| 会话/Thread | 是 | 是（session） | 部分 | 回放 | 部分 | 分组 |
| Token/成本 | 是 | 是 | 是 | 是（三级聚合） | 是 | 强 |
| Eval | 强 | 强 | 强 | 基础 | 最强 | 无 |
| 多 agent 委托树 | 无 | 无 | 无 | 相对最强 | 无 | 无 |
| 图可视化 | Studio graph mode | 无 | Agent 图 + embedding | 无 | 无 | 无 |
| 数据可带走 | 弱 | 强（MIT + S3 导出） | 是 | 弱 | 弱 | 中 |
| 现状 | 活跃 | 被 ClickHouse 收购 | 活跃 | 活跃 | 活跃（$800M 估值） | 维护模式 |

---

## 四、逐项速查

详细字段见 `results/` 下 4 个分组文件。

**Jaeger** — CNCF 毕业项目。瀑布图 + 搜索 + 两条 trace 对比 + 依赖图 + SPM（RED 指标）。单容器可跑，本地验证的事实标准。没有 agent 语义，`gen_ai.*` 只是普通属性。**适合：立刻能用的排障后端。**

**Grafana Tempo + Traces Drilldown** — Tempo 3.0（2026-05-28）换新架构，TraceQL metrics GA 且不再依赖 metrics-generator，对象存储不加索引。Traces Drilldown（2025-04-25 GA）提供 queryless 的 RED 入口，标签页含 Breakdown / Comparison / Service structure / Root cause errors / Root cause latency / Exceptions / Trace list。**适合：团队已有 Grafana，且需要 trace-metrics-logs 三向联动。**

**Uptrace** — Go 单二进制 + ClickHouse（唯一依赖）。traces/metrics/logs 三合一，service graph + 告警。资源占用最低，官方定位小团队/边缘。**适合：想用一个容器搞定全部三类信号。**

**SigNoz** — 开源 Datadog 替代。APM（延迟分位、错误率、Apdex、DB 分解）+ 瀑布图 + 火焰图 + trace funnels + service map + 告警。ClickHouse 统一存储。**适合：需要告警与异常追踪，愿意承担多组件运维。**

**Apache SkyWalking** — 2026-06 发布全新 Horizon UI：3D 基础设施图、Trace explorer（在 duration 散点图上套索选慢 trace，再用三种方式阅读）、Loki 风格 Log explorer。数据模型是 Segment 而非纯 span，与 OTLP 有映射损耗。**适合：已有 SkyWalking 体系、JVM 栈为主的组织。**

**Zipkin** — 最老牌，B3 协议，按 traceID 查询最快，UI 朴素。无 metrics/告警/深度拓扑。**结论：维护状态，新项目不要选。**

**LangSmith** — LangGraph 集成最深（环境变量即接入）。run tree + thread 视图 + Studio（graph mode 画图结构 / chat mode 对话调试）+ Engine（自动聚类未发现的问题）+ 完整 eval 闭环。**适合：本项目技术栈最贴近，但闭源、按 trace 计价、数据出厂。**

**Langfuse** — MIT 全功能开源（2026-01 被 ClickHouse 收购）。trace 树 + session 视图 + prompt 版本管理 + 数据集 + eval + 人工标注，OTLP 与 SDK 双摄入，S3 可导出。**适合：要数据主权 + LLM 语义的团队。**

**Arize Phoenix** — 最彻底的 OTel 原生（OpenInference 语义）。除 trace 树外，有 **agent 图可视化**（节点=LLM 调用，边=数据流）与 **embedding 降维散点图**，还有漂移检测。本地 `px.launch_app()` 一条命令起 UI。**适合：想在同一界面看执行结构与语义分布。**

**AgentOps** — 会话回放（time-travel debugging）+ delegation tree + handoff，自称唯一 agent-shaped 数据模型，400+ LLM 内置价格表。**适合：多 agent 编排本身是产品核心时。**

**Braintrust** — eval 优先（2026-02 $80M B 轮）。Topic 聚类把相似失败分组，CI 回归门禁最干净。**适合：质量迭代是主要诉求，trace 是配角。**

**Helicone** — 网关型，改 `base_url` 即接入，成本看板强。无 span 层级，代理是全局单点。**结论：2026-03 被 Mintlify 收购后进入维护模式，不要选型。**

---

## 五、落到 Sortie Deck

### 5.1 先明确一个判断

现有 trace 结构是：

```
mission.run                    ← 一次 start/resume
└─ stage.run                   ← 一个阶段
   ├─ gen_ai.chat              ← 一次 LLM 调用
   ├─ execute_tool             ← 一次 MCP 工具
   ├─ retrieval.recall         ← 一次召回
   └─ process.exec             ← 一个 CLI 子进程
```

这棵树的信息量已经足够。**缺的不是"更多维度"，而是"让用户在任务页里看到它"。**

外部平台（Jaeger / Grafana / Langfuse）都要求用户**离开工作台、记住 task id、去另一个系统搜**。对控制平面产品来说，这是体验断裂。

### 5.2 三个阶段

#### 阶段 0：Jaeger 深链（0.5 天，立刻可用）

不需要任何前端开发，只需三处小改：

1. `settings.py` 加 `otel_ui_base: str = ""`（如 `http://127.0.0.1:16686`）
2. API 暴露该配置（`GET /api/config` 或直接塞进 task 详情响应）
3. 前端在 timeline 事件上渲染"查看 trace"按钮：`{otel_ui_base}/trace/{event.trace_id}`

`TimelineEvent.trace_id` 已经能自动填充（`orchestrator.py` 的 `_append_timeline`），零后端逻辑。

**收益**：马上能点开看瀑布图。**局限**：跳出产品，且要求用户能访问 Jaeger。

#### 阶段 1：任务页内嵌轻量瀑布图（2–3 天）

```
GET /api/initiatives/{id}/trace?segment=1
  → 后端代理 Jaeger/Tempo 查询 API，裁剪成前端友好的结构
  → [{ span_id, parent_id, name, start, duration, status, attrs }]
  → 前端用 SVG 画横向条形 + 缩进层级
```

关键设计：

- **不要前端直连 Jaeger API**（跨域、暴露后端地址、字段冗余）
- 后端做字段裁剪，只返回 `sortie.*` 和 `gen_ai.*` 白名单属性
- 并发分支用不同颜色区分（`sortie.parallel_branch` 属性已经在 `_run_branch` 里加了）
- 失败 span 用红色左边框，不靠用户去读 `result_status`

**为什么自己做而不是 iframe 嵌入 Grafana**：iframe 需要匿名访问或 service token，等于把观测后端暴露给浏览器；且 Grafana 的样式无法与工作台统一。

#### 阶段 2：控制平面专有视图（持续）

这些是标准工具**给不了**的，因为它们是业务语义而非 trace 语义：

| 视图 | 数据来源 | 价值 |
|---|---|---|
| 任务列表加"耗时 / token"列 | 从 `mission.run` 上卷 | 一眼看出哪个任务贵、哪个慢 |
| 阶段卡片显示耗时与成本 | 从 `stage.run` 聚合 | 定位瓶颈阶段，无需打开 trace |
| HITL 等待时长统计 | 需要新增：审批等待单独开 span 或用时间差算 | 回答"到底 agent 慢还是人等得久" |
| 航线模板对比 | 按 `sortie.track` 聚合 | 哪个模板更容易失败，反哺航线设计 |
| 降级率看板 | `sortie.result_status != ok` 或 `sortie.degraded` | 现在降级是静默的，看不到 |

其中"HITL 等待时长"需要额外埋点：当前 `interrupt()` 时 span 就关闭了，等待时长只能靠 `pending_hitl` 出现时间与 `submit_hitl` 时间差计算。这是一条独立的聚合逻辑，不依赖 trace。

### 5.3 不推荐的做法

| 做法 | 为什么不 |
|---|---|
| 一上来部署 Grafana + Tempo | 两套服务 + 对象存储，换来的只是更好看的瀑布图；当前量级（每天数千 span）Jaeger 完全够 |
| 选 LangSmith 作为唯一后端 | 闭源 + 按 trace 计价 + 数据出厂，且本项目 LLM 出口已抽象，没必要绑死 |
| 选 Langfuse 但不用其 eval | 部署 ClickHouse 的运维成本换不到对应价值，不如先做深链 |
| 自建从零写完整追踪后端 | 存储、采样、查询、保留策略全要自己做，典型的高投入低回报 |
| 把 trace 数据倒进业务数据库再用业务 API 查 | span 量级和查询模式与业务数据完全不同，会拖慢主库 |

### 5.4 推荐组合

```
短期（本周）    Jaeger 单容器  +  深链按钮
中期（2-3 天）  后端 trace 代理接口  +  任务页内嵌瀑布图
长期（按需）    需要看趋势时再加 Grafana + Tempo；需要 eval 时再接 Langfuse（OTLP 直发，不改埋点）
```

**关键前提**：因为埋点已经按 OTel 标准做了，**换后端不改一行业务代码**。这是本次集成最大的结构性收益 —— 上面任何一步都可以独立回退。

---

## 六、来源

### 通用追踪 UI
- [Jaeger SPM 文档](https://www.jaegertracing.io/docs/2.22/architecture/spm/)
- [Jaeger Frontend/UI 配置](https://www.jaegertracing.io/docs/2.20/deployment/frontend-ui/)
- [Grafana Traces Drilldown GA 公告（2025-04-25）](https://grafana.com/whats-new/2025-04-25-ga-release-of-grafana-traces-drilldown/)
- [Grafana Tempo 3.0 发布说明](https://grafana.com/blog/tempo-3-0-release-all-the-latest-features)
- [Grafana: Metrics from traces](https://www.grafana.com/docs/tempo/latest/metrics-from-traces/)
- [Grafana 链路下钻 UI 参考](http://grafana.org.cn/docs/grafana/latest/explore/simplified-exploration/traces/ui-reference)
- [Uptrace 官网](https://uptrace.dev/) / [Uptrace OTel APM](https://uptrace.dev/get/opentelemetry-apm.html)
- [SigNoz 架构文档](https://signoz.io/docs/architecture)
- [SkyWalking Horizon UI 发布（2026-06-21）](https://skywalking.apache.org/blog/2026-06-21-skywalking-horizon-ui-introduction/)
- [SkyWalking vs Zipkin 选型对比](https://blog.csdn.net/weixin_28740299/article/details/165497338)
- [自托管 APM 对比：SigNoz / Jaeger / Uptrace](https://www.pistack.xyz/posts/signoz-jaeger-uptrace-self-hosted-apm-distributed-tracing-guide)

### LLM / Agent 平台
- [LangSmith Studio 文档](https://docs.langchain.com/langsmith/use-studio)
- [Langfuse 自托管定价](https://langfuse.com/pricing-self-host)
- [Langfuse vs Braintrust](https://www.langfuse.com/faq/all/best-braintrustdata-alternatives)
- [Arize Phoenix 官网](https://arize.com/phoenix)
- [Phoenix 技术定位（OTel 原生 + embedding 可视化）](https://www.aiwisdom.dev/learn/ai-observability/phoenix/see)
- [AgentOps 官网](https://www.agentops.ai/) / [Dashboard 文档](https://docs.agentops.ai/v1/usage)
- [AgentOps Review 2026](https://aitoolsatlas.ai/tools/agentops/review)
- [Braintrust 官网](https://www.braintrust.dev/)
- [Braintrust vs Helicone 能力对比](https://www.braintrust.dev/articles/helicone-vs-braintrust)
- [Helicone 进入维护模式（Mintlify 收购）](https://www.confident-ai.com/knowledge-base/compare/top-7-llm-observability-tools)

### 多 Agent 可视化缺口
- [The Agent Observability Landscape in 2026: Who Traces, Who Misses](https://dev.to/nicolas_fainstein/the-agent-observability-landscape-in-2026-who-traces-who-misses-8j0) — 注意：作者在推广自家产品 AgentWatch，竞品评级应视为单一利益相关方判断

---

## 七、调研局限

1. **各产品的 UI 形态只能通过文档与截图判断**，没有实际部署逐个试用。真正选型前建议对 Top 3 做一次各 30 分钟的实际导入验证（喂本项目的一条真实 trace）。
2. **深链 URL 格式随版本变化**，报告中标注"需实测"的部分必须在落地前用实际版本核对。
3. **企业级商业 APM（Datadog / Splunk / New Relic / Dynatrace）未纳入对比**：它们的多 agent 能力（决策路径图、工作流映射）确实领先，但部署门槛与成本与当前阶段不匹配；若未来进入企业客户场景需重新评估。
4. 部分 2026 年新特性的信息来自二手报道，官方文档可能滞后于发布说明。
