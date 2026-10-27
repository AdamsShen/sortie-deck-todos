# OpenTelemetry 全链路监控集成方案

> 目标：给 Sortie Deck 的「HTTP 请求 → 任务编排 → LangGraph 阶段 → 执行器 → LLM / 工具 / 子进程 → 产物」整条链路接入 OpenTelemetry，输出标准 OTLP trace，可直连 Jaeger / Tempo / Grafana / Datadog 任意后端。
>
> 状态：**待确认，未开始编码**

## 1. 需求背景

多 Agent 编排系统的失败模式与普通服务不同：一次任务失败往往不是抛异常，而是「模型选错了工具」「上下文被污染」「预算耗尽后半途停住」。这类问题只能靠 trace 还原，单看 `timeline` 事件列表无法定位到具体是哪次 LLM 调用、哪次工具调用出的偏差。

当前项目的可观测手段：

- `TimelineEvent` 业务事件流（`src/sortie_deck/models.py:228`），经 SSE 推前端，粒度为「阶段 / 角色 / 消息」，**不含执行耗时、token、调用层级**；
- `Guardrails.record()` 的因果链（`src/sortie_deck/guardrails.py:171`），有 `trace_id`/`parent` 字段，但只有一个平面列表、上限 500 条、且没有跨进程能力；
- `docs/zh/modules/graph.md:52` 列出的「每阶段 OTel span」是第 5 节「技术方案（目标）」和第 6 节 P1 计划，**代码中 0 处 `opentelemetry` 引用，尚未实现**。

因此本方案是首次引入，不是改造。

## 2. 现状核查：为什么不能 `pip install` 了事

三个硬约束决定了必须手写埋点：

**① 自动埋点不可依赖。** LLM 出口在 `src/sortie_deck/llm_chain.py`（LangChain `ChatOpenAI`），另有 `claude -p` 子进程兜底（`llm.py` 的 `_chat_claude_cli`）。OpenLLMetry / OpenInference 对 LangChain 有自动埋点，但会绑定 SDK 版本、且默认拿不到业务字段（阶段、角色、initiative）；在唯一出口手埋一处更可控，也不会因为依赖升级而悄悄失效。

**② token 数需要主动读。** `AIMessage.usage_metadata` 业务代码原本完全没用到，`gen_ai.usage.*` 必须在出口显式写入 —— 这是成本归因的唯一来源，缺失就没法算「一次任务花了多少」。

**③ 重活不在请求生命周期内。** `orchestrator.py:1866` 的 `graph.astream(...)` 可能在后台 task 中跑几分钟到几小时，而 FastAPI auto-instrumentation 只覆盖请求期间。全量开启 `opentelemetry-instrumentation-fastapi` 会得到大量空 span，并把 SSE 长连接的持续时间算进延迟统计。

## 3. 业内调研结论

| 结论 | 来源 |
|---|---|
| OTel GenAI 语义约定（`gen_ai.*`）虽仍标记 experimental，但已是事实标准，Langfuse / Phoenix / LangSmith / Datadog 全部按此摄入 | [Dabl Club 2026 全景](https://www.dablclub.com/research/observability)、[Arize 工具对比](https://arize.com/blog/best-ai-observability-tools-for-autonomous-agents-in-2026) |
| span 必须严格嵌套（编排 → LLM / tool / retrieval 挂在当前活跃的编排 span 下），平铺会丢失因果 | [OpenTelemetry GenAI span 类型](https://mortalapps.com/agents/production-engineering/opentelemetry-span-types-for-agents) |
| 多 Agent 跨进程靠 W3C `traceparent` 透传，断链点集中在异步队列与 streaming 边界 | 同上 |
| prompt/completion 全文默认不记录，需显式开启（OpenAI Agents SDK 的 `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` 即此设计） | [OpenAI Agents SDK 追踪](https://openai.github.io/openai-agents-python/zh/tracing) |
| 高并发场景用 `BatchSpanProcessor` + Collector 端 tail sampling，保留全部错误 trace | [OpenTelemetry Agent 可观测性](https://agentpatterns.ai/standards/opentelemetry-agent-observability) |
| LangGraph 自身无内置 OTel exporter，走 LangSmith 的 `LANGSMITH_OTEL_ENABLED` 转 OTLP；本项目未用 LangChain 模型对象，该通道拿不到 LLM 语义 | [LangGraph 追踪问答](https://tessary.ai/answers/langgraph-evals/how-do-i-trace-a-langgraph-agent)、[LangSmith OTel 文档](https://docs.langchain.com/langsmith/trace-with-opentelemetry) |

## 4. 技术设计

### 4.1 设计原则

1. **单一接触面**：业务代码只 import `sortie_deck.observability`，任何模块不直接碰 `opentelemetry.sdk`。这样关掉开关就是零行为变化，也方便将来换后端。
2. **默认关闭、装了才动**：`opentelemetry-api` 进主依赖（纯接口，无传递依赖，未配 SDK 时 tracer 自动退化为 no-op）；`sdk` + `exporter` 进 `[otel]` optional extra。
3. **不改执行器**：全部 16 个 executor 都是 `async def run(ctx) -> StageResult`，在 `run_stage` 一处包装即可覆盖，插件零改动。
4. **不采样内容**：默认只记元数据，prompt/completion 全文需显式开 `TDT_OTEL_CAPTURE_CONTENT`。
5. **trace 与业务事件双向可跳**：`TimelineEvent` 增加 `trace_id` 字段，前端点击事件可直接跳到 trace 详情。
6. **span 不跨越人工等待**：HITL `interrupt()` 时正常关闭 span，恢复时开新 trace，用 `initiative_id` 做跨段关联。

### 4.2 新增模块

`src/sortie_deck/observability.py`（约 220 行）：

```python
"""OpenTelemetry 全链路追踪的唯一接触面。

只依赖 opentelemetry-api；sdk / exporter 走可选 extra，未安装时 tracer 是
no-op，热路径开销 < 1 微秒。业务代码统一走 span() / current_trace_id()，
不得直接 import opentelemetry.sdk，保证「关掉开关 = 零行为变化」。
"""

def init_tracing(cfg) -> None: ...
    # 幂等。cfg.otel_enabled=False 或 endpoint 为空 → 不建 provider（避免内存堆积）

def shutdown_tracing() -> None: ...
    # flush + shutdown，挂到 FastAPI lifespan 收尾

@contextmanager
def span(name: str, /, **attrs) -> Iterator[Span]: ...
    # 异常自动 record_exception + set_status(ERROR) + 重新抛出

def current_trace_id() -> str: ...
    # 32 位小写 hex；无活跃 trace 时返回 ""

def headers_from_context() -> dict[str, str]: ...
    # 注入 W3C traceparent，用于 run_queue 的 job payload

def context_from_headers(carrier: dict[str, str]) -> AbstractContextManager: ...
    # 从 job payload 还原父级 context
```

**命名约定**：span name 用小写点分层，属性官方 `gen_ai.*` 优先，业务字段统一 `sortie.*` 前缀，避免与将来官方 spec 冲突。

### 4.3 Span 树

```
POST /api/initiatives/{id}/start            [server]  http.method, http.route
└─ mission.run                              sortie.initiative_id, sortie.segment, sortie.track
   ├─ stage.run intake                      sortie.stage, sortie.executor, sortie.role
   │  ├─ retrieval.recall                   sortie.query_count, sortie.hit_count
   │  ├─ gen_ai.chat                        gen_ai.provider.name, gen_ai.request.model,
   │  │                                     gen_ai.usage.input_tokens/output_tokens,
   │  │                                     gen_ai.response.finish_reasons
   │  ├─ execute_tool                       gen_ai.tool.name, gen_ai.tool.call.id
   │  ├─ process.exec                       process.command, sortie.exit_code, sortie.duration_ms
   │  └─ contract.validate                  sortie.contract_ok, sortie.missing_files
   ├─ hitl.gate                             sortie.hitl_kind, sortie.decision  (span event)
   └─ stage.run build                       sortie.parallel_branch
      └─ …
```

每层的关键属性：

| span | 必带属性 |
|---|---|
| `mission.run` | `sortie.initiative_id`、`sortie.segment`（第几次启动/续跑）、`sortie.track`、`sortie.stage_count` |
| `stage.run` | `sortie.stage`、`sortie.executor`、`sortie.role`、`sortie.qa_attempt`（首轮 QA 有特殊重试逻辑） |
| `gen_ai.chat` | `gen_ai.provider.name`、`gen_ai.request.model`、`gen_ai.request.temperature`、`gen_ai.usage.input_tokens`、`gen_ai.usage.output_tokens`、`gen_ai.response.finish_reasons` |
| `execute_tool` | `gen_ai.tool.name`、`gen_ai.tool.call.id`、`sortie.tool.source`（mcp/skill） |
| `process.exec` | `process.command`（截断到 200 字符）、`sortie.exit_code`、`sortie.timeout` |

### 4.4 配置项

在 `Settings` 中新增（`src/sortie_deck/settings.py:209` 附近，`log_level` 之后）：

```python
# --- OpenTelemetry 全链路追踪（见 docs/plans/2026-10-27-otel-full-tracing.md）---
otel_enabled: bool = False           # 总开关
otel_endpoint: str = ""              # OTLP HTTP endpoint，如 http://127.0.0.1:4318
otel_service_name: str = "sortie-deck"
otel_sample_ratio: float = 0.1       # head sampling 比例，开发期调 1.0
otel_capture_content: bool = False   # 是否记录 prompt/completion 全文（含 PII，默认关）
otel_headers: str = ""               # 形如 "authorization=Bearer xxx"，多租户后端鉴权用
```

### 4.5 文件改动清单

> 下表是立项时的初版估算，实际落点见第 10.1 节（LLM 出口已迁到 `llm_chain.py`，stage / mission span 采用「改名 + 外层包装」而非原地插入）。

| 文件 | 改动 | 预估行数 |
|---|---|---|
| `pyproject.toml:29` | 主依赖加 `opentelemetry-api>=1.29.0`；:55 加 `otel` extra | +6 |
| `src/sortie_deck/observability.py` | **新增**，见 4.2 | +220 |
| `src/sortie_deck/settings.py:209` | 6 个 `otel_*` 配置 | +8 |
| `src/sortie_deck/api/app.py:246` | lifespan 开头 `init_tracing`、yield 后 `shutdown_tracing` | +6 |
| `src/sortie_deck/api/app.py:276` | 加一个轻量 ASGI middleware 做 HTTP span（排除 SSE 路径） | +25 |
| `src/sortie_deck/orchestrator.py:1866` | `astream` 前后包 `mission.run`，`segment` 从 `ini.meta` 递增取 | +15 |
| `src/sortie_deck/orchestrator.py:479` | `_append_timeline` 写入 `event.trace_id = current_trace_id()` | +3 |
| `src/sortie_deck/graph.py:1171` | `run_stage` 包 `stage.run` span（覆盖串行 + 并行波次两条路径） | +12 |
| `src/sortie_deck/graph.py:987` | `_run_mcp_calls` 内每个 tool 调用包 `execute_tool` | +12 |
| `src/sortie_deck/graph.py:507` | `run_stage_recall` 包 `retrieval.recall` | +8 |
| `src/sortie_deck/llm.py:122` | `_chat_http` 包 `gen_ai.chat`；补读 `resp.json()["usage"]` | +25 |
| `src/sortie_deck/llm.py:151` | `_chat_claude_cli` 包 `gen_ai.chat`（无 usage，标 `sortie.usage_unavailable=true`） | +12 |
| `src/sortie_deck/plugins/coding.py:101` | `_run_cmd` 包 `process.exec`（子进程唯一出口） | +12 |
| `src/sortie_deck/plugins/deploy.py:26` | `create_subprocess_shell` 包 `process.exec` | +8 |
| `src/sortie_deck/models.py:228` | `TimelineEvent` 加 `trace_id: str = ""` | +2 |
| `src/sortie_deck/run_queue.py:77` | `claim()` 时把 `traceparent` 写进 job payload | +10 |
| `src/sortie_deck/runtime.py:27` | 日志格式加 trace_id（见 4.8） | +8 |
| `tests/test_observability.py` | **新增**，InMemorySpanExporter 断言 span 树 | +120 |
| `docs/zh/modules/graph.md:52`、`docs/en/modules/graph.md:60` | P1 计划项标记完成 | +4 |

**总改动约 520 行，其中业务代码约 160 行，80% 集中在新模块。**

### 4.6 关键实现要点

**① `run_stage` 是唯一正确的 stage span 位置**

不能放在 `make_stage_node`（`graph.py:1692`），因为并行波次由 `_run_branch`（`graph.py:1979-1989`）直接调 `run_stage`，绕过节点函数。放 `run_stage`（`graph.py:1171`）一处，串行 + 并行 + QA 重试三条路径全覆盖。

```1171:1179:src/sortie_deck/graph.py
    async def run_stage(state: PipelineState, stage: str) -> PipelineState:
        if state.get("stopped"):
            return {**state, "status": "stopped", "stage": stage}

        cfg = stage_by_name.get(stage, {"name": stage, "role": "product"})
        role = _role_id(str(cfg.get("role", "product")))
        executor_name = _executor_name(stage, role.value, state, cfg)
        executor = registry.get(executor_name)
        artifact_dir = str(artifact_store.initiative_dir(state["initiative_id"]) / stage)
```

`executor_name` / `role` / `initiative_id` 在这一段已全部就绪，直接做 span 属性，无需额外查询。

**② 并行分支要能区分**

`_run_branch` 里多个阶段并发，span 属性加 `sortie.parallel_branch=<branch_id>`，否则在瀑布图里看到 N 个同层 `stage.run` 无法判断属于哪个分支。

**③ 异常必须显式标记**

项目大量使用「边界降级兜底」（`except Exception` + `# noqa: BLE001`）。这类降级在 trace 里应表现为 `status=ERROR` + `record_exception`，否则监控会显示「一切正常」而实际在走 fallback。`span()` 上下文管理器统一处理，业务代码不重复写。

**④ LLM usage 补读**

```149:149:src/sortie_deck/llm.py
            return resp.json()["choices"][0]["message"]["content"]
```

改为先取整个 body，再分别读 `choices` 和 `usage`；`usage` 缺失时（部分兼容网关不返回）降级为不写该属性，不能抛异常。

### 4.7 HITL 与跨进程处理

**HITL（人工审批）**

`interrupt()` 触发时 `astream` 正常返回，`mission.run` 随之关闭 —— 这是正确语义：一次 trace 覆盖一次「连续执行段」。人工审批可能等几小时，不能让 span 一直开着（SDK 内存会堆积，且 trace 时长失去参考价值）。

- `mission.run` 属性带 `sortie.segment`（1, 2, 3…），每次 start/resume 递增；
- 审批动作记为 `mission.run` 上的 **span event**（`hitl.decision`，带 `sortie.decision` / `sortie.actor`），而不是独立 span；
- 跨段检索靠 `sortie.initiative_id` 在 Jaeger 里按 tag 搜，或前端用 initiative 详情页聚合。

**跨进程（`TDT_RUN_BACKEND=sqlite` 多 worker）**

`run_queue.py:77` 的 `claim()` 是唯一取任务入口。投递时 `headers_from_context()` 把 `traceparent` 序列化进 `payload`，worker 侧 `context_from_headers()` 还原父 context，这样 worker 里的 span 与 HTTP 请求同一条 trace。

**子进程（claude / cursor / codex CLI）**

CLI 内部不可见，只能记边界：`process.exec` span 带命令行（截断 200 字符）、耗时、退出码、是否超时。`plugins/coding.py:101` 的 `_run_cmd` 是所有 coding executor 的唯一子进程出口，一处覆盖全部。

### 4.8 日志与 trace 关联

`runtime.py:27` 的 `configure_logging` 当前格式为 `%(asctime)s %(levelname)s [%(name)s] %(message)s`。加一个 logging Filter，往 LogRecord 注入 `otel_trace_id`，格式改为：

```
%(asctime)s %(levelname)s [%(name)s] [trace=%(otel_trace_id)s] %(message)s
```

否则排查时仍要靠时间戳人工对齐日志与 trace，等于白做。无活跃 trace 时该字段为 `-`。

### 4.9 采样与成本

| 场景 | 采样策略 |
|---|---|
| 开发 / smoke | `ParentBased(ALWAYS_ON)`，`otel_sample_ratio=1.0` |
| 生产默认 | `ParentBased(TraceIdRatioBased(0.1))` |
| 失败 / HITL 任务 | 无法 head sampling 预知 → 依赖 Collector `tail_sampling`：`keep errors`、`keep latency > p95`、`keep tag sortie.hitl=true` |

**span 量级估算**（default 轨道 4 阶段一次完整跑）：

- 4 × `stage.run` + 1 × `mission.run` ≈ 5
- LLM 调用：规划官 1 + 房间回复 N + 阶段内 M ≈ 5–15
- 工具 / 检索：0–10
- **合计 15–30 span / 任务**，单 span 属性约 1 KB（不开 content capture）

`BatchSpanProcessor` 参数：`max_queue_size=2048`、`schedule_delay_millis=5000`、`max_export_batch_size=512`。`otel_endpoint` 为空时**不建 provider**，避免无人消费的队列堆积。

### 4.10 脱敏

- `TDT_OTEL_CAPTURE_CONTENT=false` 为默认，prompt/completion 只记字符数（`sortie.prompt_chars`）；
- `Authorization` header 永不入 span；`api_key` 若出现在属性中替换为 `***`；
- `process.exec` 的命令行需过滤 `--token`、`--api-key` 一类参数；
- `sortie.initiative_id` 属于内部 ID，可记；用户手机号 / 邮箱等 PII 不记。

## 5. 分阶段实施

| 阶段 | 内容 | 验收 | 工作量 |
|---|---|---|---|
| **P0** | `observability.py` + settings + lifespan 接入 + `mission.run` + `stage.run` + `gen_ai.chat` | 跑 `sortie smoke` 能在 Jaeger 看到三层嵌套的 trace 树 | 1.5 天 |
| **P1** | `process.exec` + `execute_tool` + `retrieval.recall` | 编码阶段能看到 CLI 耗时与退出码；MCP 调用可见 | 1 天 |
| **P2** | `TimelineEvent.trace_id` + run_queue 传播 + 日志 trace_id | 前端事件可跳 trace；多 worker 不断链 | 1 天 |
| **P3（部署与展示，非应用集成）** | Collector 部署 + Grafana 看板 | 见 5.1 | 1 天 |

P0 完成即可验证价值，P1/P2 补齐「全链路」的字面含义。

### 5.1 P3 的边界（2026-10-27 修订）

原先 P3 把三件归属不同的事捆成一行，容易被误读为「OTel 集成的一部分」，现拆开：

| 项 | 归属 | 说明 |
|---|---|---|
| metrics | OTel 三大信号之一 | 但**可以不写代码**：Tempo 的 `metrics_generator` 能从 trace 派生 RED 指标，应用侧零改动；只有在需要 trace 属性覆盖不到的业务指标时才值得加 `MeterProvider`（约 40 行） |
| tail sampling | **Collector 的 processor**，不是 SDK | 必须独立部署 Collector，且要求同一条 trace 的全部 span 落到同一实例（配 loadbalancing exporter + trace-ID 路由）。**本项目单任务 15–30 span、每天数千 span，属低量高价值场景** —— 应 `ratio=1.0` 全采样 + 保留期策略，引入 tail sampling 只会换来运维复杂度，还会带来「出事那次恰好没采到」的概率 |
| Grafana 看板 | 与 OTel 无关 | 展示层。P0 之后直接用 Jaeger 自带 UI 即可，无需任何额外组件 |

结论：P3 属于「部署与展示」，不阻塞 P0–P2。

## 6. 验证方式

**本地手工验证**

```bash
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest

TDT_OTEL_ENABLED=true \
TDT_OTEL_ENDPOINT=http://127.0.0.1:4318 \
TDT_OTEL_SAMPLE_RATIO=1.0 \
sortie smoke

open http://127.0.0.1:16686
```

断言：出现 `mission.run` → `stage.run intake` → `gen_ai.chat` 三层嵌套，且 LLM span 上有 `gen_ai.usage.input_tokens`。

**自动化测试**（`tests/test_observability.py`）

用 `InMemorySpanExporter` 而非真实后端，断言结构与语义：

1. 开关关闭时 `current_trace_id()` 返回 `""`，且不产生 span（零成本）；
2. `span()` 内抛异常 → 该 span `status.code == ERROR` 且带 exception event；
3. 嵌套 `span()` 的父子关系正确；
4. 一次 mock 管线跑完后，span 名称集合包含 `{mission.run, stage.run, gen_ai.chat}`；
5. `headers_from_context()` / `context_from_headers()` 往返后 trace_id 一致。

## 7. 备选方案

| 方案 | 否决理由 |
|---|---|
| LangSmith OTel 导出（`langsmith[otel]` + `LANGSMITH_OTEL_ENABLED`） | 默认绑定商业 SaaS；且本项目 LLM 不走 LangChain 模型对象，拿不到 LLM 层语义 |
| OpenLLMetry / OpenInference 自动埋点 | hook 的是 `openai` SDK 客户端，本项目 `llm.py:142` 是裸 httpx，**零覆盖** |
| 纯自研（只扩展现有 `TimelineEvent` / `TraceEntry`） | 不引入依赖，但失去 OTLP 生态，前端瀑布图、Grafana 看板都要自己写；且 span 语义（父子、时长、状态）本来就要重新发明一遍 |
| FastAPI 全量 auto-instrumentation | 重活在后台 task，请求期 span 为空；SSE 长连接会污染延迟统计 |
| 只做 metrics 不做 trace | 能看总量（token、失败率），但无法回答「这一次为什么失败」 |

## 8. 风险

| 风险 | 缓解 |
|---|---|
| `contextvars` 在 LangGraph `astream` 内部丢失，trace 断成多条 | P0 加断言：`node()` 入口检查 `current_trace_id() != ""`，断链时打 warning；必要时用 `config["callbacks"]` 兜底 |
| 项目现有两套轨迹（`TimelineEvent` / `TraceEntry`）与新 span 三套并存 | 明确分工写入文档：TimelineEvent=业务事实，TraceEntry=任务内因果链，span=执行事实；用 `initiative_id` + `trace_id` 关联，不再新增第四套 |
| 降级分支在 trace 里显示「正常」 | `span()` 对 `except` 一律 `set_status(ERROR)`，并在 fallback 路径打 `sortie.degraded=true` |
| Collector 不可用时影响主链路 | exporter 全异步 + 丢弃式失败；`init_tracing` 包 try/except，初始化失败只打 warning 不阻断启动 |
| 属性基数爆炸（把 stage 内容当属性） | 明确规定：属性只放枚举/ID/数值，自由文本一律不入 span |

## 9. 状态

- [x] 业内调研（见第 3 节）
- [x] 现状核查（见第 2 节）
- [x] 技术设计
- [x] 用户确认（2026-10-27）
- [x] P0 实施（骨架 + mission/stage/LLM span）
- [x] P1 实施（子进程 + MCP 工具 + 检索）
- [x] P2 实施（`TimelineEvent.trace_id` + 队列传播 + 日志关联）
- [ ] P3（部署与展示，见 5.1，非应用集成）

## 10. 实施记录（2026-10-27）

### 10.1 与设计的差异

| 计划 | 实际 | 原因 |
|---|---|---|
| LLM span 打在 `llm.py:122` `_chat_http` | 打在 `llm_chain.py` 的 `_invoke` | 项目已迁移到 LangChain `ChatOpenAI`（见 `docs/plans/2026-10-26-llm-langchain-adapter.md`），`_chat_http` 已删除；`_invoke` 是全项目 chat 的唯一出口，一处覆盖全部业务调用，且能直接读到 `AIMessage.usage_metadata` |
| HTTP middleware 做入口 span | **未做** | `BaseHTTPMiddleware` 会缓冲响应，对 SSE 有回归风险；且重活在 `asyncio.create_task` 的后台任务里，请求期 span 基本为空。`mission.run` 已能作为根 span |
| `run_stage` 直接加 span | 改名 `_run_stage`，外层新增 `run_stage` 包装 | 原函数体一行未改，降低回归风险；同时覆盖串行 / 并行波次 / QA 重试三条路径 |
| `_run_until_interrupt` 内嵌 span | 改名 `_run_until_interrupt_inner`，外层新增包装 | 同上；顺带把 `sortie.status` 收尾属性放进 `finally` |
| `TimelineEvent.trace_id` 由 `_append_timeline` 填 | 同 | 未启用追踪时保持空串，不产生误报 |

### 10.2 实施中发现并修掉的坑

1. **`OTLPSpanExporter(endpoint=...)` 不会自动拼 `/v1/traces`**（与环境变量 `OTEL_EXPORTER_OTLP_ENDPOINT` 行为不同）。只配 `http://host:4318` 会 POST 到根路径，后端一律 404，表现为「初始化成功但一条 span 都看不到」。已在 `_traces_endpoint()` 里补全，两种写法都支持。
2. **CLI 路径不走 FastAPI lifespan**，`sortie smoke` 原本不会初始化追踪；已在 `_smoke` 显式 `init_tracing` / `shutdown_tracing`。
3. **CLI 等待窗口过短**：`12 × 0.4s = 4.8s`，intake 走真实 LLM 时稳定超时，既看不到终态也看不到完整 trace；放宽到 30s。
4. **收尾顺序**：编排跑在 `asyncio.create_task` 里，状态翻转到 `waiting_hitl` 与任务真正返回之间还有几个 await。CLI 在 `shutdown_tracing` 前先让出一次事件循环，否则最后几个 span（含 `mission.run`）会在 provider 关闭后才结束，被 `BatchSpanProcessor` 丢弃。

### 10.3 验证结果

- `tests/test_observability.py` 14 条通过，含一条**真实编排**用例：跑到第一个 HITL，断言 `mission.run` 存在、`stage.run` 挂在其下、`sortie.initiative_id` 与任务 id 一致
- 全量 `pytest` 525 条通过；`ruff check` 对改动文件零告警
- 本地 OTLP sink 实测收到 `POST /v1/traces`，解码可见：
  - `stage.run` → `sortie.initiative_id` / `sortie.stage` / `sortie.role` / `sortie.executor` / `sortie.result_status`
  - `gen_ai.chat` → `gen_ai.request.model=deepseek/...`、`gen_ai.usage.input_tokens=176`、`gen_ai.usage.output_tokens=148`
  - `retrieval.recall` → `sortie.stage`

### 10.4 已知边界

- **进程退出时未结束的 span 会丢**：这是所有 tracing 系统的固有限制。`mission.run` 只有在一次 start/resume 真正返回后才关闭；smoke 若在中途超时退出，就看不到它。
- **CLI 子进程内部不可见**：`claude` / `cursor` 内部行为只能记成一条 `process.exec` 边界 span。
- **`sortie.hitl` 目前只是 span 上的普通属性**，尚未按 IETF AAT 的 `pre_execution` / `post_execution` 双记录模型落地；那是审计层的事，见 `docs/plans` 后续计划。
