# MCP 会话池机制详解

- 日期：2026-10-20
- 关联：`docs/plans/2026-10-20-tool-search-meta-tool.md`（3.5 节）、
  `docs/plans/2026-10-20-mcp-http-transport-and-skill-scripts.md`（http/sse 传输层计划）、
  `src/sortie_deck/mcp_runtime.py`（当前实现）

## 1. 先分清三个概念：server、会话、池

讨论"池化"时最容易混淆的是把 **server** 和**会话（session）**当成一回事，它们是两层东西：

### server（服务端程序）

提供工具的那个程序本身——filesystem server、browser server 等。它运行在别处（子进程或远程
主机），**天然可以被任意多个客户端同时连接**。尤其是 HTTP 型 server，本来就是
"一个服务进程，N 个客户端"的标准 Web 模型。

所以"复用同一个 MCP server"这件事是天然成立的，从来不是问题，也**不是池化要解决的**。

### 会话（session，客户端侧连接状态）

**我们这一侧**维护的连接状态，包含：

- `initialize` 握手结果 + 服务端返回的 `Mcp-Session-Id`
- `tools/list` 拉回来的工具清单（白名单注册依赖它）
- 一条已建立的连接（httpx 的 TCP/TLS 连接，或子进程的 stdin/stdout 管道）
- stdio 场景下：spawn 出来的那个**子进程**本身

不复用会话 = 每次要用工具都重新走一遍冷启动：TCP/TLS 握手 → initialize → tools/list
（sse 型还要重建 GET 长连接）。**server 还是同一个 server，但每次都是一次全新连接。**

### 池（pool）

**预先建好、用完不拆、借出/归还的一组会话对象**，外加一套回收规则：

| 规则 | 设计 | 原因 |
|------|------|------|
| 在途保护 | 有未完成调用（in_flight > 0）的会话不回收 | 防止误杀 2 分钟级长调用 |
| 空闲 TTL | 无在途请求且空闲超过 300s 才关闭 | 远大于冷启动成本，避免"关了又开"抖动 |
| 容量上限 | 全局实例数上限（默认 50），LRU 驱逐无在途实例 | 几百个 server 场景必须有硬顶 |
| 任务结束 | `release_task` 立即回收该任务全部实例（含 stateful） | 任务结束不占空闲窗口 |
| 健康检查 | 借出前存活检查，死实例先逐出再重建 | server 半路挂掉要显式降级，不静默失败 |

## 2. 三种 transport 下的"会话"长什么样

工具箱条目的 `McpTransport = Literal["stdio", "sse", "http"]`（`src/sortie_deck/toolkit.py:18`），
三种传输的会话本体和共享能力完全不同：

| | stdio | http（Streamable HTTP） | sse（legacy HTTP+SSE） |
|---|---|---|---|
| 会话本体 | 一个本地子进程 | httpx 连接 + session id | 常驻 GET 事件流 + POST 端点 |
| 创建成本 | 高（npx 类冷启动数秒） | 极低 | 低 |
| 一对一？ | **是**：一个进程的 stdin/stdout 只能挂一个客户端 | 否：服务端天然服务多客户端 | 否 |
| 跨任务共享 | 物理上不可能 | 可以（无 per-user 状态时） | 可以（同上） |
| 隔离需求 | 每任务必须独立实例 | `stateful: true` 的按任务隔离 | 同左 |

## 3. 池键与共享桶（设计的核心）

池键统一为 `(item_id, config_hash, task_id)`：

- `item_id` + `config_hash`：配置变了必须起新会话，所以连接配置的指纹参与键；
- `task_id` 决定**隔离域**：

```
stdio（不管是否 stateful）
  → 键用真实 task_id：每任务独立实例，任务内复用
    （消灭任务内反复冷启动；stateful server 也无跨任务泄露）

http / sse 且 stateful=false
  → 键的 task_id 固定为 "shared"（共享桶）
    任务 A 调完工具，会话（握手成果 + 工具清单 + 连接）不销毁，
    任务 B 来借时直接拿同一会话用，省掉整段冷启动
    ——这就是"几乎零进程成本，直接多任务共享"

http / sse 且 stateful=true
  → 键用真实 task_id：仍按任务隔离，防跨任务状态泄露
    （浏览器会话、登录态等 server 有 per-user 状态时必须隔离）
```

对上层 `McpToolInvoker` 完全透明：它只管 `acquire` / `call` / `release`，按条目的
transport 由池内部分派到对应会话实现。

## 4. 一个容易产生的误解

> "池化 = 复用同一个 MCP server？"

不是。**server 一直是同一个、天然共享**——任何客户端任何时候连的都是同一个 server 进程。

池化共享的是**我们这侧已经建好的连接状态**：

- 对 stdio，连接状态 = 子进程，一对一、有进程成本，只能每任务一份；
- 对 http/sse，连接状态很便宜且（无状态时）无隔离问题，所以可以全平台共用一份。

## 5. 业内参照

- **IBM ContextForge MCP Gateway** 的 Session Pool
  （[Tuning Guide](https://ibm.github.io/mcp-contextforge/latest/manage/tuning/)）：
  `MCP_SESSION_POOL_TTL=300s`（空闲强制关闭）、`MCP_SESSION_POOL_MAX_PER_KEY=10`
  （每 key 池上限），官方称 10-20x 延迟改善；并明确警告**仅当上游 server 无 per-user
  状态时才可安全共享**——与本文"共享桶 + stateful 隔离"一一对应。
- **MCP 官方规范**
  （[Streamable HTTP](https://modelcontextprotocol.io/specification/latest/basic/transports/streamable-http)）：
  会话靠 `Mcp-Session-Id` 头维护；2026-07-28 修订移除 GET 流与协议级会话（服务端不返回
  session id 头即视为无会话，客户端天然兼容）。

## 6. 现状与计划对照

| 能力 | 现状 |
|------|------|
| stdio 池化（每任务独立实例、TTL、LRU、任务结束回收） | ✅ 已实现（`mcp_runtime.py`） |
| http/sse 会话实现 | ❌ 目前 skip（`mcp_runtime.py:36-38`），计划见 `docs/plans/2026-10-20-mcp-http-transport-and-skill-scripts.md` |
| http/sse 共享桶 | 计划中（同上） |
