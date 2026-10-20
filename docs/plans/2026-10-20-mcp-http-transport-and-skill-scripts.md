# 实施计划：MCP http/sse 传输层 + skill 脚本执行沙箱

- 日期：2026-10-20
- 依据：`docs/plans/2026-10-20-tool-search-meta-tool.md`（3.5 节 http/sse 池化、P1 skill scripts 沙箱）及其
  实施文档中的两处降级项
- 状态：草案，待确认后实施

## 1. 需求背景

上一轮实施留下两个降级项：

1. **http/sse 不池化**：`McpSession` 只实现了 stdio JSON-RPC 客户端，transport 为 `http`/`sse`
   的条目直接 skip（`mcp_runtime.py:36-38`），起不了会话、调不了工具。预置条目里 browser MCP
   就是 sse 型（`toolkit.py:171-176`），属于实际阻塞。
2. **skill 脚本无执行通道**：`settings.skill_run_enabled`（`settings.py:98`）自项目初期就存在
   但没有任何消费者；`toolkit_pick` 上浮 skill 时只把正文注入 brief，截断标注"完整正文请经
   skill 执行获取"是空头支票，脚本既不进上下文也没有执行入口。

## 2. 业内调研（真实检索结果）

| 来源 | 要点 | 对本项目的意义 |
|------|------|----------------|
| [MCP 规范 · Streamable HTTP](https://modelcontextprotocol.io/specification/latest/basic/transports/streamable-http) | 2025-03-26 引入，替代 2024-11-05 的 HTTP+SSE；服务端暴露**单一 MCP 端点**，客户端把每个 JSON-RPC 请求 POST 上去，响应为 `application/json`（单对象）或 `text/event-stream`（SSE 流）二选一；会话经 `Mcp-Session-Id` 响应头维护（initialize 返回，后续请求带回）；`DELETE` 终止会话；2026-07-28 修订移除 GET 流端点与协议级会话，客户端需向后兼容 | transport=`http` 按此实现：POST + 可选 SSE 响应解析 + session id 头 |
| [MCP 规范 · 2024-11-05 HTTP+SSE（legacy）](https://modelcontextprotocol.io/specification/2024-11-05/basic/transports) | 旧传输：客户端先 GET 服务端 `/sse` 建立事件流，服务端经 `endpoint` 事件告知 POST 地址；所有 JSON-RPC 请求 POST 到该地址，响应经 SSE 流异步返回 | transport=`sse` 按此实现：常驻 GET 流 + 解析 endpoint 事件 + 响应按 id 匹配 |
| [httpx + SSE 客户端参考](https://github.com/duynghien/n8n-custom-mcp/blob/main/docs/sse-integration-guide.md) | 用 httpx 流式读 SSE、逐行解析 `data:` 字段即可，无需专用 SDK | 项目已有 `httpx>=0.27`，自研解析器 ~40 行，零新增依赖 |
| [ContextForge 池化警告](https://ibm.github.io/mcp-contextforge/latest/manage/tuning/)（上轮已引） | 仅无 per-user 状态的 server 可跨客户端安全共享 | http 会话默认进**共享桶**跨任务复用；`stateful: true` 仍按任务隔离 |

与既有约定的兼容点：授权集中（编排层执行）、write 确认门、白名单、审计留痕全部沿用，不新增授权面。
冲突点：无。

## 3. 技术设计

### 3.1 MCP http/sse 传输层（新模块 `src/sortie_deck/mcp_http.py`）

统一实现与现有 `McpSession` 相同的鸭子接口（`.item` / `.tools` / `.log` / `start()` /
`call_tool(name, arguments)` / `close()`），池与 invoker 无感知分派：

```
HttpMcpSession（transport="http"，Streamable HTTP）
  start():  POST initialize（Accept: application/json, text/event-stream）
            → 存 Mcp-Session-Id 响应头（若有）
            → notifications/initialized → POST tools/list
  call_tool(): POST tools/call（带 session id 头）
            响应 application/json 直接解析；text/event-stream 则流式解析
            data: 行直到出现本请求 id 的 JSON-RPC 响应
  close():  尽力 DELETE（Mcp-Session-Id 头）+ 关闭 httpx.AsyncClient

SseMcpSession（transport="sse"，legacy HTTP+SSE）
  start():  后台任务 GET url 常驻读 SSE 流
            → 收到 endpoint 事件记下 POST 地址（相对则按 url 拼接）
            → POST initialize/tools/list（响应经事件流按 id 匹配，挂 asyncio.Event 等待）
  call_tool(): POST 到 endpoint，等待事件流中同 id 响应（超时保护）
  close():  取消 GET 流任务 + DELETE endpoint（尽力）

通用：
  - httpx.AsyncClient 复用连接；超时 = connect 5s / 调用 30s（常量可调）
  - 认证：读 item.config["headers"]（dict，原样注入，**不落日志**）、
    item.config["bearer_token"]（转 Authorization: Bearer …）
  - 响应里的 JSON-RPC error 显式抛出，由池记 dead
```

### 3.2 池化改造（`mcp_runtime.py`）

- `acquire()` 按 transport 分派：`stdio` → 现有 `McpSession`；`http`/`sse` → 3.1 对应会话。
- 池键不变：`(item_id, config_hash, task_id)`；**http/sse 且非 stateful 时 task_id 桶固定为
  `"shared"`**——无进程成本，跨任务直接复用同一会话（方案 3.5 "直接多任务共享"）；
  `stateful: true` 的 http server 仍按真实 task_id 隔离，防跨任务状态泄露。
- 回收判据、TTL、LRU、in_flight、`release_task` 全部沿用：http/sse 会话同样参与空闲回收与
  容量上限；`_entry_alive` 对 http 会话为"未标记 dead"（调用/建连失败置 dead，下次借出重建）。
- `McpToolInvoker.open()` 里原来 `if item.kind != "mcp" or not item.enabled: continue` 之外，
  池化路径不再跳过非 stdio（分派交给池）。

### 3.3 skill 脚本执行沙箱（新模块 `src/sortie_deck/skill_runner.py`）

```
run_skill_script(item, script_path, args, *, workspace_dir, timeout) -> dict
  1. 校验：settings.skill_run_enabled 开启；item.kind == "skill" 且 enabled；
     item.executable == True（与 probe 语义一致，未标记 executable 拒绝）；
     script_path 必须命中 item.scripts 里登记的 path（禁止任意路径）
  2. 落盘：脚本 content 写入 <workspace_dir>/.tdt_skills/<item_id>/<script.path>
     （内容来自工具箱存储，模型接触不到也不需要读源码）
  3. 执行：按 language 选解释器（py → sys.executable，sh/bash，js/node，
     其余拒绝）；cwd = 上面目录；env = os.environ + item.env；timeout =
     settings.skill_run_timeout_seconds，超时 kill
  4. 返回 {ok, exit_code, stdout(截断 8000 字符), stderr(截断), duration_ms}
     ——进程级隔离（独立子进程）即"沙箱"；不提供网络/文件系统白名单（P2 可加）
```

### 3.4 graph 接线：`skill_run` 意图（沿用"声明意图、编排层执行"模式）

- executor 在 `StageResult.meta` 声明：`{"skill_run": {"item_id": "...", "script": "scripts/x.py",
  "args": [...], "confirmed": false}}`
- `run_toolkit_meta` 扩展处理该意图：pick 过的上浮条目直接允许；未 pick 过的条目要求
  先 pick（保持"先上浮后使用"的闭环语义）；校验链复用 `pick_candidate`；每阶段执行预算
  `SKILL_RUN_BUDGET_PER_STAGE = 4`（新常量）；`confirmed=False` 且脚本名疑似写操作时
  拒绝（沿用 `tool_looks_like_write` 启发式 + confirm 门）
- 结果：stdout 摘要回填 brief_append（模型可见执行结果，闭环）；完整输出落
  `skill_runs.json` 阶段产物；events/`toolkit_meta.json` 记录（`op: "skill_run"`）
- `PipelineState` 新增 `skill_run_budget: dict[str, int]`（与 pick 预算同模式）
- executor 协议说明书 `_TOOLKIT_META_PROTOCOL` 补第 4 步 skill_run 说明

## 4. 文件与改动范围

| 文件 | 改动 |
|------|------|
| `src/sortie_deck/mcp_http.py` | 新增：`HttpMcpSession` / `SseMcpSession` + SSE 行解析器 |
| `src/sortie_deck/mcp_runtime.py` | `acquire` 分派 http/sse；共享桶 `task_id="shared"`；`_entry_alive` 适配 |
| `src/sortie_deck/skill_runner.py` | 新增：`run_skill_script` |
| `src/sortie_deck/loadout.py` | 新常量 `SKILL_RUN_BUDGET_PER_STAGE = 4`、`SKILL_OUTPUT_FLOAT_LIMIT = 8000` |
| `src/sortie_deck/graph.py` | `run_toolkit_meta` 处理 `skill_run` 意图；PipelineState 加 `skill_run_budget` |
| `src/sortie_deck/harness_providers/executor.py` | 协议说明书补 skill_run 步骤 |
| `tests/` | `tests/test_mcp_http.py`（fake http server 覆盖两种 transport）、`test_skill_runner.py`、`test_toolkit_meta.py` 增 skill_run 用例、`test_mcp_pool.py` 增共享桶用例 |

预计规模：后端 ~400 行 + 测试 ~250 行。

## 5. 分期

- **本期（一次做完，两项互相独立可并行验收）**：
  - A：http/sse 传输层 + 池化共享桶（`mcp_http.py` → 池分派 → 测试）
  - B：skill 执行沙箱 + `skill_run` 意图链路（`skill_runner.py` → graph 接线 → 测试）
- **不做**：脚本级网络/文件系统白名单沙箱（进程隔离已满足当前风险面，P2 评估）；
  2026-07-28 修订版"无会话"语义的主动探测（服务端不返回 session id 头即视为无会话，天然兼容）。

## 6. 备选方案

1. **引入官方 `mcp` Python SDK 的 streamablehttp_client**：协议兼容性最好，但引入一整套
   anyio/依赖树，与现有自研 minimal 客户端风格冲突，且池需要自己包一层。→ 不采纳。
2. **http/sse 每次调用新建会话、不池化**：实现最省，但 initialize+tools/list 每次重跑，
   SSE 型还要重建 GET 流；session 状态丢失（部分 server 有会话内状态）。→ 不采纳，池化
   增量成本很低（复用全部回收机制）。
3. **skill 脚本经 docker/nsjail 强沙箱**：隔离最彻底，但部署面陡增（容器内嵌容器）。
   → 延后，先进程级隔离 + 超时 + confirm 门。

## 7. 测试要点

- http：application/json 与 SSE 两种响应路径；session id 头保留与回传；服务端 500/JSON-RPC
  error → dead 标记与重建；
- sse：endpoint 事件解析（绝对/相对地址）、响应按 id 匹配、GET 流断开重建；
- 池：http 会话跨任务共享（不同 task_id 拿到同一实例）；stateful http 按任务隔离；TTL 空闲
  回收与 LRU 对 http 实例同样生效；
- skill：executable=False 拒绝、未登记 path 拒绝、开关关闭拒绝、超时 kill、输出截断、
  写脚本名无 confirmed 拒绝、预算耗尽拒绝、pick 前置校验；
- 回归：stdio 路径与现有 22 个用例零回归。
