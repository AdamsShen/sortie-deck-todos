# 远程 MCP（Streamable HTTP）

## 1. 概述

把任务能力以 MCP 工具的形式暴露给外部 agent（本地 Claude Code、Cursor 等）。
服务端是 **API 进程里的 `/mcp` 路由**，不是独立服务——这样它和 API 共享同一个
`Orchestrator` 单例与 `data/` 目录，MCP 建的任务在工作台里能实时看到。

| 项 | 值 |
|----|-----|
| 代码 | `mcp_server.py`（服务端）、`mcp_http.py`（客户端，自测用） |
| 端点 | `POST/GET {public_base_url}/mcp` |
| 认证 | `Authorization: Bearer sk_sortie_...`（用户级 API Key） |
| 状态 | **无状态**：不依赖协议级会话，任意副本可服务 |

## 2. 为什么无状态

2026-07-28 版 MCP 规范移除了协议级会话与 `Mcp-Session-Id`。本项目需要跨调用
保存的状态只有一个——**任务 id**，它本身就是规范所说的 server-minted handle，
作为普通参数传递即可。

好处：MCP 端点可以水平扩副本、不需要 sticky session、进程重启后客户端直接用原
Key 继续调用。

## 3. 工具面（12 个）

| 工具 | 读/写 | 说明 |
|------|-------|------|
| `start_mission` | 写（可逆） | 建任务 + 确认航线 + 启动，立即返回 `mission_id` 与队列深度 |
| `await_mission` | 读 | 长轮询：命中门禁/终态立即返回，超时返回 `still_running` |
| `get_mission` | 读 | 状态 + 门禁详情（`response_format=concise\|detailed`） |
| `list_missions` | 读 | 我创建或被共享的任务 |
| `submit_hitl` | 写（受门禁保护） | `approve` / `reject` / `edit_instruction` / `stop` |
| `stop_mission` | 写 | 停止任务 |
| `list_artifacts` / `read_artifact` | 读 | 产物清单与内容（>200k 字符截断） |
| `get_events` | 读 | 按 `seq` 拉事件 |
| `list_tracks` | 读 | 可用航线 |
| `retrieve_knowledge` | 读 | 知识库检索 |
| `create_memory` | 写 | 写入共享记忆 |

资源：`mission://{mission_id}/state`、`mission://{mission_id}/artifact/{path}`；
提示词：`plan_mission`。

**刻意不做的两件事**：不镜像 117 个 REST 路由；工具参数里不接受 `user_id`——
身份只能来自 Key。

## 4. 门禁语义

门禁是结构化对象，不是一句提示语：

```json
{
  "stage": "product_prd",
  "prompt": "Review outputs of `product_prd` and choose an action.",
  "summary": "`product_prd` 产出 prd.md、acceptance.json",
  "risk": "只读评审：产物可回看，驳回会回退到本阶段重做",
  "irreversible": false,
  "options": ["approve", "reject", "edit_instruction", "stop", "reroute", "switch_harness"],
  "action_help": {"approve": "认可本阶段产物，继续下一阶段", "...": "..."}
}
```

`deploy*` / `ship*` / `release*` / `publish*` 阶段自动标记 `irreversible: true`，
或在航线配置里用 `irreversible: true` 显式覆盖。

三端（网页 / CLI / MCP）处理同一个门禁：先到者生效，其余收到 `conflict` 并带上
"已被谁处理"。

## 5. 配置

| 变量 | 默认 | 说明 |
|------|------|------|
| `TDT_PUBLIC_BASE_URL` | 空（回落 `http://{host}:{port}`） | 生成 MCP 配置片段用的对外地址 |
| `TDT_MCP_ALLOWED_HOSTS` | 空 | Host 白名单；留空关闭 DNS-rebinding 防护（身份由 Key 保证） |
| `TDT_API_KEY_RATE_LIMIT_PER_MINUTE` | `240` | 每个 Key 的进程内软限流 |

## 6. 客户端配置

在网页「API 密钥」里新建一把，复制生成的片段：

```json
{
  "mcpServers": {
    "sortie-deck": {
      "type": "http",
      "url": "http://<host>:8787/mcp",
      "headers": { "Authorization": "Bearer sk_sortie_..." }
    }
  }
}
```

或：

```bash
claude mcp add --transport http sortie-deck http://<host>:8787/mcp \
  --header "Authorization: Bearer sk_sortie_..."
```

## 7. 部署注意

- MCP 与 API **同进程**：拆成独立容器时必须共享同一个 `data/` 卷，否则工作台看不到
  任务、事件也读不到。
- 端口必须对客户端可达（内网 IP 或域名 + 反向代理）；`127.0.0.1` 只适合本机调试。
- 上公网前请启用 TLS：API Key 是长期凭据，明文过网等于账号可被接管。

## 8. 自测

```python
from sortie_deck.mcp_http import HttpMcpSession
from sortie_deck.toolkit import ToolkitItem

item = ToolkitItem(kind="mcp", name="sortie", transport="http", url="http://127.0.0.1:8787/mcp",
                   config={"headers": {"Authorization": "Bearer sk_sortie_..."}})
session = HttpMcpSession(item)
await session.start()          # tools/list
await session.call_tool("start_mission", {"brief": "..."})
```

`tests/test_mcp_server.py` 正是用这条路径做协议一致性测试。

## 9. 延伸阅读

完整版说明（设计决策、本次修复的 8 个问题、CLI/网页/Claude Code 三端用法、部署与验收记录）见
[MCP 设计与使用指南](../MCP设计与使用指南.md)。
