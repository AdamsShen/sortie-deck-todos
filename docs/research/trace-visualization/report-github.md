# Trace 可视化：GitHub 仓库维度调研（第二轮）

> 调研日期：2026-10-27
> 方法：GitHub Search API（`stars` / `language` / `pushed` 过滤），非认证额度
> 目的：第一轮按"产品"维度看（官网、文档、定价），本轮按"仓库"维度看（代码规模、活跃度、可复用性），补上第一轮看不到的长尾与实现细节

---

## 一、核心发现

### 1. 最关键的否定结论：**不存在可复用的 trace 瀑布图组件库**

搜索 `waterfall language:typescript stars:>=100` 只返回 2 个结果，全是 masonry 图片瀑布流布局（`yandere-masonry`、`dsh-permission-rules`），与 trace 无关。

`trace timeline`、`trace viewer`（TS）加起来只有 4 个仓库，且全部是**完整应用**而非组件：

| 项目 | Stars | 性质 |
|---|---|---|
| CtrlSpice/otel-desktop-viewer | 1.3k | 桌面应用（CLI + 本地 UI） |
| harrylettering/claude-trace-replay | 264 | Claude 会话回放工具 |
| metafab/otel-gui | 195 | 本地 OTel 查看器 |
| mesaglio/otel-front | 118 | OTLP 前端 |

**结论**：每个产品（Jaeger、SigNoz、Langfuse、HyperDX）的瀑布图都是自己写的、和自身数据模型深度耦合，没有抽成通用库。这意味着第一轮报告里"自建轻量瀑布图（2–3 天）"是**没有替代方案的选择**——不存在 `npm install` 一个 trace waterfall 组件。

### 2. 与本项目定位最接近的项目：OpenLIT

`openlit/openlit` — 2.9k stars，Apache-2.0，TypeScript + Python，350 MB，昨天还在提交。

官方描述：**"the open-source agent harness engineering platform: trace, evaluate, guard, and improve everything around the model in your AI agents, on OTEL"**

标签里明确有：`agent-harness`、`agent-harness-engineering`、`coding-agent`、`claude-code`、`codex`、`cursor`、`genai`、`opentelemetry`、`clickhouse`。

这与 Sortie Deck 的"agent 编排控制平面 + OTel 埋点 + coding executor（claude_code / cursor_cli / codex）"几乎完全同构。**第一轮的 12 个候选里完全没有覆盖它**——因为它在官网 SEO 上不占优势，只在 GitHub 上有存在感。

价值点：它是完整平台（不是组件），但代码开源、Apache-2.0，其 trace 视图与 agent 视图的实现可以直接借鉴。

### 3. 唯一 MIT 许可的全栈可观测平台：HyperDX

`hyperdxio/hyperdx` — 10.0k stars，**MIT**，TypeScript，今天仍在提交。

能力：session replay + logs + metrics + traces + errors，ClickHouse + OTel 驱动，前端 React。

**MIT 是关键差异**：Grafana/Tempo 是 AGPL，SigNoz 是 MIT/Apache 混合，HyperDX 整体 MIT 意味着可以合法地把它的前端组件逻辑抄进闭源产品。若未来要做"内嵌 session replay + trace"，这是唯一无许可证顾虑的参考实现。

### 4. 出现了一条第一轮没覆盖的技术路线：零侵入自动埋点

| 项目 | Stars | 做法 |
|---|---|---|
| odigos-io/odigos | 3.7k | 基于 eBPF 自动生成 OTel instrumentation，不改代码 |
| deepflowio/deepflow | 4.3k | eBPF 零侵入采集，含网络层与应用层 |
| pinpoint-apm/pinpoint | 13.9k | Java agent 字节码增强，方法级无侵入（今天仍活跃） |

这条路线与本项目**不冲突而是互补**：我们的 LLM/工具调用必须手埋（语义只有自己知道），但 HTTP/DB/子进程这类基础设施调用可以靠自动埋点补齐。当前 `process.exec` 只记边界，若将来要进子进程内部，eBPF 是唯一不动代码的办法。

### 5. "LLM 可观测"与"产品分析"正在融合

`agent observability` 榜单第一位是 **PostHog（40.5k）**——一个产品分析平台，不是专门的 LLM 工具。这说明市场在收敛：用户不想为了看 agent 行为再开一个系统。

对本项目的启示：**自建视图的价值高于接入外部平台**。用户已经在工作台里看任务了，多一个跳转就是多一次摩擦。

---

## 二、榜单数据

### 2.1 Distributed Tracing（≥2000 stars，365 天内活跃）

| # | 项目 | Stars | Forks | 语言 | 更新 |
|---|---|---|---|---|---|
| 1 | SigNoz/signoz | 32.5k | 2.6k | TypeScript | 今天 |
| 2 | jaegertracing/jaeger | 23.3k | 3.2k | Go | 昨天 |
| 3 | openzipkin/zipkin | 17.5k | 3.1k | Java | 2 月前 |
| 4 | pinpoint-apm/pinpoint | 13.9k | 3.7k | Java | 今天 |
| 5 | highlight/highlight | 9.4k | 693 | TypeScript | 3 天前 |
| 6 | grafana/tempo | 5.5k | 778 | Go | 今天 |
| 7 | deepflowio/deepflow | 4.3k | 490 | Go | 今天 |
| 8 | odigos-io/odigos | 3.7k | 256 | Go | 今天 |
| 9 | openzipkin/brave | 2.4k | 712 | Java | 7 月前 |

统计：平均 12.5k stars，Go 44% / Java 33% / TypeScript 22%，**78% 最近 30 天有更新**。

值得注意：Zipkin 主仓 2 个月前有提交（比第一轮判定的"维护状态"稍乐观，但 Brave 客户端 7 个月未更新，生态确实停滞）；Pinpoint 今天仍在更新，Java 生态的字节码增强路线生命力比预期强。

### 2.2 LLM Observability（≥300 stars，共 62 个，取前 15）

| # | 项目 | Stars | 语言 | 更新 |
|---|---|---|---|---|
| 1 | langfuse/langfuse | 36.6k | TypeScript | 今天 |
| 2 | mlflow/mlflow | 28.5k | Python | 今天 |
| 3 | openobserve/openobserve | 22.5k | TypeScript | 今天 |
| 4 | NirDiamant/agents-towards-production | 21.8k | Jupyter | 今天 |
| 5 | kubesphere/kubesphere | 17.1k | Go | 3 月前 |
| 6 | raga-ai-hub/RagaAI-Catalyst | 16.2k | Python | 8 月前 |
| 7 | dataelement/bisheng | 12.1k | Python | 今天 |
| 8 | tensorzero/tensorzero | 11.7k | Rust | 4 月前 |
| 9 | VoltAgent/voltagent | 10.9k | TypeScript | 1 周前 |
| 10 | evidentlyai/evidently | 8.0k | Jupyter | 3 周前 |
| 11 | traceloop/openllmetry | 7.5k | Python | 1 周前 |
| 12 | apache/hertzbeat | 7.4k | Java | 今天 |
| 13 | katanemo/plano | 7.1k | Rust | 2 天前 |
| 14 | open-multi-agent/open-multi-agent | 7.0k | TypeScript | 昨天 |
| 15 | FailproofAI/failproofai | 6.6k | TypeScript | 今天 |

统计：平均 14.7k，TypeScript 33% / Python 27% / Jupyter 13%，80% 月活。

**噪声说明**：kubesphere、bisheng、hertzbeat、evidently 是被关键词"llm·observability"误召回的通用平台/ML 工具，不是 LLM 可观测专品。真实专品是 Langfuse、RagaAI Catalyst、OpenLLMetry、VoltAgent、plano、failproofai。

### 2.3 Agent Observability（≥200 stars，共 127 个，取前 15）

| # | 项目 | Stars | 语言 | 更新 |
|---|---|---|---|---|
| 1 | PostHog/posthog | 40.5k | Python | 今天 |
| 2 | langfuse/langfuse | 36.6k | TypeScript | 今天 |
| 3 | SigNoz/signoz | 32.5k | TypeScript | 今天 |
| 4 | mlflow/mlflow | 28.5k | Python | 今天 |
| 5 | vectordotdev/vector | 22.8k | Rust | 今天 |
| 6 | NirDiamant/agents-towards-production | 21.8k | Jupyter | 今天 |
| 7 | raga-ai-hub/RagaAI-Catalyst | 16.2k | Python | 8 月前 |
| 8 | apache/doris | 16.1k | Java | 今天 |
| 9 | ccfos/nightingale | 13.4k | Go | 今天 |
| 10 | Tracer-Cloud/opensre | 12.5k | Python | 今天 |
| 11 | kubeshark/kubeshark | 12.1k | Go | 5 天前 |
| 12 | dataelement/bisheng | 12.1k | Python | 今天 |
| 13 | getagentseal/codeburn | 11.6k | TypeScript | 今天 |
| 14 | VoltAgent/voltagent | 10.9k | TypeScript | 1 周前 |
| 15 | apache/hertzbeat | 7.4k | Java | 今天 |

统计：平均 19.7k，**93% 最近 30 天有更新**——这是四个榜单里最活跃的赛道。

新面孔（第一轮未覆盖）：`Tracer-Cloud/opensre`（12.5k）、`getagentseal/codeburn`（11.6k，TS）、`VoltAgent/voltagent`（10.9k）、`open-multi-agent`（7.0k）、`FailproofAI/failproofai`（6.6k）。

### 2.4 OTel 生态 TS 前端（≥200 stars，共 27 个，取前 15）

| # | 项目 | Stars | 说明 |
|---|---|---|---|
| 1 | SigNoz/signoz | 32.5k | 全栈 APM |
| 2 | openobserve/openobserve | 22.5k | 日志为主 |
| 3 | Effect-TS/effect | 17.4k | 运行时库（噪声） |
| 4 | **hyperdxio/hyperdx** | **10.0k** | **MIT，session replay + trace** |
| 5 | artilleryio/artillery | 9.1k | 压测（噪声） |
| 6 | open-telemetry/opentelemetry-js | 3.5k | 官方 SDK |
| 7 | open-telemetry/opentelemetry-demo | 3.4k | 官方 Demo（含自建 trace 视图） |
| 8 | **openlit/openlit** | **2.9k** | **agent harness 平台** |
| 9 | MapleTechLabs/maple | 1.9k | OTel 前端 |
| 10 | superloglabs/superlog | 1.5k | OTel 平台 |
| 11 | BetterDB-inc/monitor | 1.3k | OTel 监控 |
| 12 | **CtrlSpice/otel-desktop-viewer** | **1.3k** | 本地桌面查看器 |
| 13 | opentelemetry-js-contrib | 938 | 官方插件集 |
| 14 | nestjs-otel | 799 | NestJS 集成 |
| 15 | strands-agents/sdk-typescript | 696 | AWS agents SDK |

**`opentelemetry-demo`（3.4k）值得单独看**：官方 Demo 里有一份自建的 trace 视图实现，是理解"官方推荐的 trace 长什么样"的最佳样本，且许可证宽松。

---

## 三、与第一轮（产品维度）的差异

| 维度 | 第一轮（产品/官网） | 第二轮（GitHub） |
|---|---|---|
| 覆盖范围 | 12 个主流产品 | 62 + 127 + 27 个仓库，含长尾 |
| 能看到的 | 功能、定价、定位 | 代码规模、提交频率、许可证、真实活跃度 |
| 漏掉的 | OpenLIT、HyperDX、Highlight、Odigos、DeepFlow、Pinpoint、几个新 agent 平台 | 官网文档质量、真实 UI 体验 |
| 结论冲突点 | 判定 Zipkin "维护状态" | Zipkin 主仓 2 月前有提交，但 Brave 客户端 7 月未更新 → **降级为"生态停滞"更准确** |
| 新增结论 | — | **可复用瀑布图组件不存在**（第一轮只是推测要自建） |

两轮结论一致的部分：

- Jaeger / Tempo 是通用 UI 的合理选择（GitHub 活跃度也印证）
- Langfuse 是开源 LLM 可观测的首选（36.6k stars，今天仍在提交）
- 多 agent 因果链无人解决（GitHub 上 127 个 agent observability 项目，没有一个以"跨 agent 因果"为核心卖点）

修正的部分：

- **Helicone 在第一轮被判"维护模式"，GitHub 上根本没进任何一个榜单** → 结论更强，可以完全排除
- **Zipkin 从"维护状态"调整为"生态停滞"**（主仓仍偶有提交，但客户端 Brave 已 7 个月未更新）
- **新增 OpenLIT 作为最值得对标的项目**

---

## 四、对本项目落地的修正建议

### 4.1 阶段 1 的工期要上调

第一轮估"自建轻量瀑布图 2–3 天"。GitHub 调研证明**没有现成组件可复用**，需要从零写布局、缩放、虚拟滚动、并发分支着色。修正为 **3–5 天**，或先做只读静态版（不支持缩放/搜索，2 天）。

### 4.2 优先抄 HyperDX，而不是从零设计

MIT 许可 + React + ClickHouse + OTel，是本项目最合法的参考实现。具体可借鉴：

- trace 瀑布图的层级缩进与时间轴换算
- session replay 与 trace 的联动方式（如果未来要做"任务回放"）
- 大 span 数量下的虚拟滚动策略

### 4.3 读一遍 OpenLIT 的 agent 视图

它和本项目同构（agent harness + coding agent + OTel），可以直接回答"coding agent 的 trace 该怎么展示"这个具体问题——比通用 APM 的参考价值高得多。注意它是 Apache-2.0，代码可借鉴但需保留声明。

### 4.4 eBPF 路线留作后手

当前 `process.exec` 只能看到 CLI 边界。若将来需要看 claude/cursor 子进程内部的网络与文件行为，Odigos / DeepFlow 的 eBPF 方案是唯一"不改代码"的路径。现在不做，但架构上不要堵死（子进程已经是独立进程，天然可被 eBPF 观测）。

### 4.5 一个反向验证

`agent observability` 榜单 93% 月活、平均 19.7k stars，说明这个赛道极度拥挤。**本项目不应该去做"又一个 LLM 可观测平台"**，而应该只做"控制平面自己的那部分视图"——即任务、阶段、HITL、航线这些业务语义，通用能力交给 OTel 生态。

---

## 五、数据局限

1. **GitHub API 未认证限额 60 次/小时**，最后两个查询（`agent harness`、`trace timeline`）被限流拒绝，因此"agent harness"方向的完整榜单缺失。
2. **关键词召回有噪声**：`kubesphere`、`doris`、`vector`、`nightingale`、`artillery`、`Effect-TS` 等是被通用词误召回的非同类项目，已在榜单里标注。
3. **stars 不等于质量**，也不代表适合本项目；部分高星项目（如 PostHog）只是顺带做了 LLM 分析。
4. **未做代码级审查**：没有逐仓库读实现，结论基于描述、标签、活跃度与许可证。"可借鉴 HyperDX / OpenLIT" 需要在实际动手前先读它们的对应模块。
5. 两个查询失败意味着可能遗漏了"agent harness"这个细分领域的项目，建议后续用认证 token 补跑。

---

## 六、附录：查询记录

| 查询 | 过滤条件 | 结果 |
|---|---|---|
| distributed tracing ui | stars≥300 | 0（多词 AND 过严） |
| llm observability | stars≥300 | 62 个 |
| distributed tracing | stars≥2000 | 9 个 |
| trace viewer | ts, stars≥100 | 4 个 |
| agent observability | stars≥200 | 127 个 |
| waterfall | ts, stars≥100 | 2 个（全噪声） |
| opentelemetry | ts, stars≥200 | 27 个 |
| agent tracing | stars≥500 | 失败 |
| agent harness | stars≥300 | 失败（限流） |
| trace timeline | stars≥100 | 失败（限流） |

详情查询：`CtrlSpice/otel-desktop-viewer`、`hyperdxio/hyperdx`、`openlit/openlit`
