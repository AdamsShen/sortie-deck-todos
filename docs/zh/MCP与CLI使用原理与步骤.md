# MCP 与 CLI 使用原理与步骤

> 配套文档：[MCP 设计与使用指南](./MCP设计与使用指南.md)（设计取舍、修复记录）
> 本文只讲两件事：**它是怎么跑起来的**，以及**你要按什么顺序操作**。

---

## 0. 我该看哪一节

| 你的情况 | 看 |
|---|---|
| 想在本机 5 分钟跑通 | §3.1 |
| 要部署到云服务器给团队用 | §3.2 |
| 想让本地 Claude Code 调用 | §3.3 |
| 想在终端里用 `sortie mission` 操作云端任务 | §3.4 |
| 想接进 CI / 脚本 | §3.5 |
| 只想在本机调试、不想起服务 | §3.6 |
| 云上起来了，本地配好能不能直接用 | §6 |
| 确认用的是不是 Streamable HTTP | §6.1 |
| 怀疑反代/网关把 MCP 弄坏了 | §6.3、§5 |
| 出问题了 | §5 |

先记住一句话：**MCP 只解决"模型怎么调工具"，不解决"任务在哪台机器上跑"。**
任务跑在哪，取决于你起了哪个服务端。

---

## 1. 三个角色，别搞混

```
① 服务端（必须常驻）
   sortie api  ── 对外 :8787，暴露 /（前端）、/api/*（REST）、/mcp（MCP）
   sortie worker ── 不出网，消费队列、真正执行任务（云端部署时）

② 客户端 A：Claude Code
   读 .mcp.json 里的 url → 启动会话时向 /mcp 发 initialize → 拿到 12 个工具
   它不启动服务端，只是"连过去"

③ 客户端 B：终端里的 sortie CLI
   看 ~/.sortie/config.json 有没有 url：
     有 → 发 HTTP 到 /api/*
     没有 → 不联网，自己在进程里 new 一个 Orchestrator
```

关键点：**MCP 配置写的是 URL（HTTP 传输），不是 command（stdio）。**
所以"配了就自动起"这件事不存在——stdio 才会由客户端 spawn 子进程，本项目不是那种。

---

## 2. 原理

### 2.1 MCP 的传输与生命周期

| 项 | 值 |
|---|---|
| 传输 | Streamable HTTP |
| 端点 | `POST {base}/mcp` |
| 会话 | **无状态**，不依赖 `Mcp-Session-Id` |
| 跨调用状态 | 只有一个：`mission_id`（由服务端生成，客户端当普通参数回传） |
| 客户端启动时发生什么 | 发 `initialize` + `tools/list`，仅此而已 |

无状态的含义：任意请求可以打到任意 api 副本；服务端重启后客户端用原 Key 继续调，不需要重新握手。

协议能力对照（实测）：

| 方法 | 状态 | 说明 |
|---|---|---|
| `initialize` | ✅ | 无状态，不返回 session |
| `tools/list` | ✅ 12 个 | |
| `tools/call` | ✅ | 返回 `content` + `structuredContent` |
| `resources/list` | ✅（空数组） | 资源以**模板**形式声明，不是具体列表 |
| `resources/templates/list` | ✅ 2 个 | `mission://{id}/state`、`mission://{id}/artifact/{path}` |
| `resources/read` | ✅ | |
| `prompts/list` / `prompts/get` | ✅ 1 个 | `plan_mission` |
| `ping` | ✅ | SDK 内置 |
| 未知方法 | ✅ | 返回 JSON-RPC `-32601`，不是 HTTP 500 |
| `GET /mcp`（SSE 长连接） | ✅ 200 `text/event-stream` | 可选能力，客户端不用也没关系 |
| `notifications/progress` | ❌ | 长任务走 `await_mission` 轮询 |
| `resources/subscribe` | ❌ | 用 `get_events(since_seq)` 拉增量 |
| `logging/*` / `completion/*` | ❌ | 未实现，客户端应容忍 |
| `sampling` / `roots` | ❌ | 服务端不反向请求客户端 |
| Tasks 扩展 | ❌ | 规范仍是 experimental，客户端支持度不一 |

**响应是 SSE 帧，不是裸 JSON**；`Accept` 头必须同时包含 `application/json` 与
`text/event-stream`——这是规范要求，缺了可能被拒。curl 调试写法与逐项自检见 §6.4。

> 为什么不用 stdio：任务要跑 10–60 分钟，stdio 子进程会随客户端关闭而被杀，任务必丢。

### 2.2 身份从哪来

```
Authorization: Bearer sk_sortie_xxx
        │
        └─ sha256 ──► data/api_keys.sqlite ──► user_id ──► PublicUser
```

三条硬规则：

1. **工具参数里没有 `user_id`**，身份只认 Header —— 否则任何人都能冒充别人建任务。
2. Key 只在创建时明文返回一次，库里只存哈希。
3. 吊销立即生效：下一次请求就 401。

两种失败要分清：

| 返回 | 含义 |
|---|---|
| `401`（HTTP 层） | Key 无效/过期/吊销 —— 根本进不了工具 |
| 工具返回 `forbidden` | Key 有效，但这条任务不属于你 |

### 2.3 一次 `start_mission` 实际发生了什么

```
Claude Code
  │ tools/call start_mission {brief, track}
  ▼
/mcp 路由
  │ ① 从 Header 解析 Principal（Middleware 已 fail-fast 过一次）
  │ ② 调 MissionService.start_mission()  ← 和 REST、CLI 用的是同一个方法
  ▼
Orchestrator
  │ ③ create_initiative(created_by=你)
  │ ④ 确认航线（按 track / 关键词规划阶段）
  │ ⑤ start_pipeline → 入队 or 直接 create_task
  ▼
执行端（同进程 inline，或云端 worker 进程）
  │ ⑥ 取该 mission 的运行锁 → graph.astream 一路跑到 interrupt
  │ ⑦ 每步写 initiatives.sqlite + events.sqlite，并扇出给 SSE 订阅者
  ▼
工具立即返回 {mission_id, status, queue_depth, next:"await_mission ..."}
```

注意 ⑥ 里的**按 mission 加锁**：同一任务的 start/resume 必须串行（否则两个 run 写同一份 checkpoint），不同任务可以并行。

### 2.4 长任务为什么不阻塞调用

MCP 工具调用有超时，而 mission 要跑几十分钟。所以采用「句柄 + 轮询」：

```
start_mission          → 立刻返回 id
await_mission(id, 120) → 命中 waiting_hitl / done / failed 就返回
                         超时返回 {still_running: true}
get_mission(id)        → 随时查一次
```

`await_mission` 是服务端内部轮询后一次性返回，不是长连接。客户端看到 `still_running` 就再调一次，直到门禁。

### 2.5 CLI 的两种模式怎么判定

```python
# src/sortie_deck/cli_mission.py
url = os.environ.get("SORTIE_URL") or cfg.get("url") or ""
key = os.environ.get("SORTIE_API_KEY") or cfg.get("key") or ""
```

| | `url` 为空 → embedded | `url` 有值 → remote |
|---|---|---|
| 连谁 | 谁都不连 | `{url}/api/*` |
| 代码路径 | `Orchestrator()` 进程内直连 | httpx + Bearer |
| 数据落点 | **本机** `TDT_DATA_DIR`（默认仓库 `data/`） | 云端 `/data` |
| 谁持有任务 | 这个 CLI 进程 | 云端 worker |
| 进程退出 | 任务中断 | 任务继续 |
| `start` 行为 | **一直等到门禁/终态** | 加 `--wait` 才等，否则立即返回 |

优先级：环境变量 > `~/.sortie/config.json`（由 `sortie login` 写入，权限 600）。

嵌入式必须等的原因：进程本身就是执行者。早期版本不等，退出时任务被取消、状态落成 `failed` 并打印 `Connection closed`，现已修正。

### 2.6 remote 模式下每条命令发什么请求

| 命令 | 请求 |
|---|---|
| `start` | `POST /api/initiatives` → `POST .../pipeline/confirm` → `POST .../start` |
| `status` | `GET /api/initiatives/{id}` |
| `list` | `GET /api/initiatives?mine=1&limit=N` |
| `hitl --approve` | `POST /api/initiatives/{id}/hitl` |
| `artifacts` / `--cat x.md` | `GET .../artifacts` / `GET .../artifacts/x.md` |
| `watch` | `GET .../events`（SSE，按 `seq` 续读） |

对比：MCP 的 `start_mission` 是**一次调用**完成这三步。两条路径代码不同，但都落到同一个 `MissionService`，所以状态永远一致。

### 2.7 "不带 id = 最近一个"怎么找

1. 显式传入的 `mission_id`
2. `~/.sortie/last_mission.json`（`start` 时写入，**按服务端 url 隔离**，换环境不会串）
3. 服务端兜底：`GET /api/initiatives?mine=1&limit=1`

第 3 条必须存在——任务可能是 Claude Code 通过 MCP 建的，本地没有指针。每次解析都会打印用了哪一条，避免多终端批错任务。

### 2.8 三端解同一个门禁

```
网页按钮 ─┐
CLI      ─┼─► Orchestrator.submit_hitl(decision, author, source)
MCP 工具 ─┘
```

- `source` 记录 `web / cli / mcp`，写入时间线与 `meta.last_hitl_by`；
- 先到者生效，后到者收到 `conflict` + 「已被 X 处理」；
- **批准后会继续跑到下一个门禁**，这是正常现象，不是重复执行。

---

## 3. 步骤

### 3.1 本机跑通（最短路径）

```bash
# 1) 安装（Python ≥ 3.11）
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2) 准备配置
cp .env.example .env          # 至少确认 TDT_DATA_DIR 指向你想放数据的位置

# 3) 体检（会检查存储、队列、LLM 三项）
sortie doctor

# 4) 起服务
sortie api                    # 默认 127.0.0.1:8787
```

打开 `http://127.0.0.1:8787`。如果 `/` 是 404，说明前端还没构建：

```bash
cd apps/web && npm install && npm run build && cd ../..
sortie api
```

只是想改前端代码，用开发模式（API :8787 + Vite :5173）：

```bash
scripts/start-dev.sh
```

### 3.2 云上常驻（Docker Compose）

```bash
cp .env.docker.example .env
```

`.env` 里**三项必须改**：

| 变量 | 为什么要改 |
|---|---|
| `TDT_TOKEN_SECRET` | 默认值在 `TDT_ENV=production` 下会直接拒绝启动 |
| `TDT_PUBLIC_BASE_URL` | 网页生成的 MCP 片段用它拼地址；漏了会生成 `127.0.0.1`，用户粘上去必然连不上 |
| `TDT_ENV=production` | 开启生产校验 |

```bash
docker compose build
docker compose up -d
docker compose exec api sortie doctor
curl -fsS http://<host>:8787/api/health
```

组成与扩容：

```bash
# api：对外服务，不消费队列（TDT_RUN_WORKER=false）
# worker：只执行，副本数 = 并发任务数
WORKER_REPLICAS=3 docker compose up -d --scale worker=3
```

要 HTTPS（API Key 是长期凭据，上公网必须 TLS）：

```bash
SORTIE_DOMAIN=sortie.example.com docker compose --profile tls up -d
```

### 3.3 接入 Claude Code（MCP）

1. 网页右上角 **「API 密钥」** → 输入名称 → **新建密钥**；
2. 弹窗里有三份内容，任选其一：
   - `.mcp.json` 片段
   - `claude mcp add ...` 命令
   - 明文 Key（**关掉就看不到了**）
3. 粘到项目根目录 `.mcp.json`：

```json
{
  "mcpServers": {
    "sortie-deck": {
      "type": "http",
      "url": "https://sortie.example.com/mcp",
      "headers": { "Authorization": "Bearer sk_sortie_..." }
    }
  }
}
```

或直接执行：

```bash
claude mcp add --transport http sortie-deck https://sortie.example.com/mcp \
  --header "Authorization: Bearer sk_sortie_..."
```

4. 验证：

```bash
claude mcp list          # sortie-deck 应显示 connected
```

5. 在会话里用自然语言驱动：

```
你：用 sortie-deck 跑一个任务，给用户中心加健康检查接口
    → start_mission，返回 mission_id

你：现在到哪了
    → await_mission / get_mission，模型复述阶段

你：让它继续
    → submit_hitl approve

你：PRD 验收标准太粗，补充后再继续
    → submit_hitl edit_instruction + instruction
```

模型能"看懂门禁"，是因为返回里带了 `summary` / `risk` / `irreversible` / `action_help`。

> 连不上先看 §5 第一行：服务端是否在跑、url 是否可达。

### 3.4 本地 CLI 连云端

```bash
# 1) 保存凭据（写入 ~/.sortie/config.json，权限 600）
sortie login --url https://sortie.example.com --key sk_sortie_...

# 2) 建任务（立即返回，执行在云端）
sortie mission start --brief "给用户中心加健康检查接口" --track standard

# 3) 查看（省略 id = 最近一个）
sortie mission status

# 4) 解门禁
sortie mission hitl --approve

# 5) 看产物（--cat 支持三种写法：完整路径 / stage/文件 / 唯一文件名）
sortie mission artifacts
sortie mission artifacts --cat qa_verify/test_cases.md
sortie mission artifacts --cat test_cases.md

# 6) 持续跟随（SSE）
sortie mission watch
```

想等它停在门禁再返回，加 `--wait`：

```bash
sortie mission start --brief "..." --track standard --wait
```

`--json` 让 stdout 只有 JSON（提示信息走 stderr），方便脚本解析。

### 3.5 接进 CI / 脚本

```bash
export SORTIE_URL=https://sortie.example.com
export SORTIE_API_KEY=sk_sortie_...

sortie mission start --brief "$CHANGE_DESC" --track express --json
sortie mission status --json | jq -r '.status, .stage'
```

`SORTIE_URL` 优先级高于 `~/.sortie/config.json`，CI 里不要落盘。

退出码（可直接分支）：

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 用法错误 |
| 3 | 认证失败 / 无权限 |
| 4 | 任务不存在 |
| 5 | 冲突（已在运行、门禁已被处理） |
| 6 | 参数校验失败 |
| 7 | 可重试（网络、超时、限流） |

### 3.6 本机调试（embedded）

```bash
# 不 login，直接跑
sortie mission start --brief "本地试一下" --track express
```

- 不监听端口、不联网、不经过 MCP；
- 数据写本机 `data/`；
- **`start` 会一直等到门禁/终态**才退出（进程即执行者）。想让它"后台跑"，请改用 §3.4。

退出调试环境：

```bash
sortie logout      # 删掉 ~/.sortie/config.json，回到 embedded
```

---

## 4. 速查

### 4.1 MCP 工具（12 个）

| 工具 | 参数 | 用途 |
|---|---|---|
| `start_mission` | `brief`, `title?`, `track?`, `harness?` | 建+确认+启动，立即返回 |
| `await_mission` | `mission_id`, `timeout_seconds=120` | 等到门禁/终态 |
| `get_mission` | `mission_id`, `response_format=concise\|detailed` | 查状态与门禁 |
| `list_missions` | `status?`, `limit?` | 我可见的任务 |
| `submit_hitl` | `mission_id`, `action`, `instruction?` | approve / reject / edit_instruction / stop |
| `stop_mission` | `mission_id` | 停止 |
| `list_artifacts` | `mission_id` | 产物清单 |
| `read_artifact` | `mission_id`, `path` | 产物原文（>200k 截断） |
| `get_events` | `mission_id`, `since_seq=0`, `limit=100` | 事件补播 |
| `list_tracks` | — | 航线与阶段 |
| `retrieve_knowledge` | `query`, `top_k?` | 知识库检索 |
| `create_memory` | `content`, ... | 写共享记忆 |

资源：`mission://{id}/state`、`mission://{id}/artifact/{path}`
提示词：`plan_mission`（把模糊需求整理成任务简报）

### 4.2 CLI

| 命令 | 说明 |
|---|---|
| `sortie login --url U --key K` | 保存凭据 |
| `sortie logout` | 删除凭据，回到 embedded |
| `sortie mission start --brief "..." [--track] [--harness] [--wait] [--json] [--dry-run]` | 建任务 |
| `sortie mission status [id] [--json]` | 查状态 |
| `sortie mission list [--status] [--limit]` | 列任务 |
| `sortie mission hitl [id] --approve\|--reject\|--stop\|--instruction "..."` | 解门禁 |
| `sortie mission artifacts [id] [--cat 文件名]` | 产物 |
| `sortie mission watch [id]` | 跟随进度 |
| `sortie api` / `sortie worker` / `sortie doctor` / `sortie smoke` | 服务端与体检 |

---

## 5. 排错对照

| 症状 | 原因 | 处理 |
|---|---|---|
| `claude mcp list` 显示 failed / 工具为空 | 服务端没起，或 url 不可达 | `curl {base}/api/health`；确认容器在跑 |
| `/mcp` 返回 401 | Key 无效、被吊销、Header 写错 | 重新建 Key；确认是 `Bearer sk_sortie_...` |
| 返回 404（路径找不到） | 少了 `/mcp`，或反代没转发该路径 | 检查 url 结尾；Caddy/Nginx 需 `/mcp` 透传 |
| 生成的片段里是 `127.0.0.1:8787` | 没配 `TDT_PUBLIC_BASE_URL` | 配成对外域名后重启，重建 Key 弹窗 |
| CLI 建的任务云上看不到 | 没 `login`，走了 embedded | `sortie login` 后重跑 |
| `没有可用任务` | 没传 id、本地无指针、服务端也没有 | `sortie mission list` 找 id 显式传入 |
| `await_mission` 一直 `still_running` | 在排队（看 `queue_depth`），或 worker 没起 | 加 worker 副本；`docker compose logs worker` |
| 网页不实时 | api 与 worker 的 `TDT_DATA_DIR` 不是同一个卷 | 检查 compose 的 volume 挂载 |
| 网页能看到任务但点不动门禁 | 不是任务 owner 且未被共享 | 用任务所有者账号，或 `shared_with` 授权 |
| `sortie worker` 启动即报错 | 不是 `sqlite` 运行后端 | 需 `TDT_RUN_BACKEND=sqlite` + `TDT_STORAGE=sqlite` |
| 每个阶段都卡 60 秒才动 | 没配模型，但机器上有 `claude`，走了 CLI 回退并等超时 | 设 `TDT_LLM_CLI_FALLBACK=0`（或配好 `TDT_LLM_API_KEY`） |
| `--cat 文件名` 报 404 | 旧版本只认完整存储路径 | 新版本三种写法都支持；确认为最新代码 |
| 工具报错只有 `Error executing tool xxx` | SDK 屏蔽了普通异常消息 | 已改成 `ToolError`；若仍出现，检查是不是非业务异常（栈内异常本身不外泄） |
| 反代后 `initialize` 无响应 / 工具列表空 | Nginx 默认 `proxy_buffering on`，SSE 帧被缓冲 | `proxy_buffering off; proxy_read_timeout 300s; proxy_http_version 1.1;` |

---

## 6. 上线验收与协议细节

云上 `sortie api` 起来、本地 `.mcp.json` 配好之后，**绝大多数情况下就能直接用**。
这一节说明"为什么能"、"什么情况会不能"，以及怎么在 30 秒内验证。

### 6.1 用的是 Streamable HTTP

位置：`src/sortie_deck/mcp_server.py:475`（`build_mcp_app`）

```python
_sub_app = server.streamable_http_app(
    streamable_http_path=path,      # "/mcp"
    json_response=False,            # 走 SSE 帧
    stateless_http=stateless,       # True
    transport_security=security,
)
```

| 参数 | 值 | 效果 |
|---|---|---|
| `streamable_http_path` | `/mcp` | **单端点**，所有 JSON-RPC 都 POST 到这里 |
| `stateless_http` | `True` | 不发 `Mcp-Session-Id`，任意副本都能处理 |
| `json_response` | `False` | 响应走 SSE 帧（`event: message` + `data: {...}`） |

明确不是另外两种：

| 传输 | 为什么不用 |
|---|---|
| **stdio** | 需要客户端 spawn 子进程；会话一关进程被杀，10–60 分钟的任务必丢 |
| **旧版 HTTP+SSE（2024-11-05）** | `/sse` + `/messages` 双端点，已被规范废弃 |

所以客户端配置写 `"type": "http"`（不是 `"sse"`、不是 `"stdio"`）。

### 6.2 能直接用的四个前提

| # | 前提 | 不满足时的表现 |
|---|---|---|
| 1 | 服务端常驻且端口可达 | `claude mcp list` 显示 failed，工具列表为空 |
| 2 | `TDT_PUBLIC_BASE_URL` 指向对外地址 | 生成的片段里是 `127.0.0.1`，粘到别的机器必然连不上 |
| 3 | Key 有效未吊销 | 请求返回 401，连 `initialize` 都过不去 |
| 4 | 反代放行 `/mcp` 且关闭缓冲 | `initialize` 无响应 / 工具列表空（看起来像服务端没实现） |

第 4 条是最常见的"明明服务正常却用不了"，模板见下。

### 6.3 反向代理模板

**Nginx**

```nginx
location /mcp {
    proxy_pass http://api:8787;
    proxy_http_version 1.1;
    proxy_buffering off;              # 关键：不关会把 SSE 帧缓冲住，客户端收不到响应
    proxy_read_timeout 300s;          # await_mission 可能长轮询，默认 60s 会被切断
    proxy_set_header Authorization $http_authorization;
    proxy_set_header Host $host;
}
```

**Caddy**（项目自带 `docker/Caddyfile`）：基本无需改动，只需确保 `Authorization` 不被剥离。

### 6.4 三步自检（30 秒定位问题）

```bash
BASE=https://sortie.example.com; KEY=sk_sortie_...

# 1) 服务活着吗
curl -fsS $BASE/api/health

# 2) MCP 端点挂上了吗（无凭据应返回 401，而不是 404/502）
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'

# 3) 凭据与工具列表（应看到 12 个工具名）
curl -s -X POST $BASE/mcp \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | sed -n 's/^data: //p' | head -c 300
```

| 现象 | 结论 |
|---|---|
| 1 通过、2 返回 404 | 反代没转发 `/mcp`，或 url 少了 `/mcp` |
| 1 通过、2 返回 502 | 反代后端地址写错 / 容器没起 |
| 2 返回 401、3 返回 401 | Key 无效或未带上 `Bearer ` 前缀 |
| 2 通过但 3 空/超时 | 反代缓冲（`proxy_buffering off`）或 `Accept` 头缺 `text/event-stream` |
| 3 只有 200 但内容为空 | 同上；也可能命中了旧版本的 `/mcp/` 307 重定向 |

三条都通，`claude mcp list` 一定能连上。

### 6.5 工具错误的语义

调用失败时返回 `isError: true`，文本是 **`错误: <code>: <message>`** 形式，例如：

```
Error executing tool get_mission: not_found: 任务不存在: ini_doesnotexist
```

`code` 是稳定契约，模型据此决定重试、换 id 还是放弃：

| code | 含义 | 模型该怎么做 |
|---|---|---|
| `not_found` | 任务不存在，或对你不可见 | 不要重试，换 id 或问用户 |
| `forbidden` | Key 有效但无权操作该任务 | 放弃，或请所有人共享 |
| `conflict` | 门禁已被处理 / 任务状态已变 | 重新 `get_mission` 再决定 |
| `validation` | 参数不合法 | 修正参数 |
| `auth_required` | Key 无效/过期 | 让用户重新签发 |

> SDK 默认会屏蔽普通异常消息（只显示 "Error executing tool xxx"），
> 所以工具内部统一抛 `ToolError`，把业务 `code` 带出去。

### 6.6 已知边界

| 项 | 说明 |
|---|---|
| 要求 `Mcp-Session-Id` 的老客户端 | 我们无状态不发该头，需客户端按 2025-06 及以后规范实现 |
| `notifications/progress` | 未实现；进度用 `await_mission` / `get_events` |
| `resources/subscribe` | 未实现；用 `get_events(since_seq)` 拉增量 |
| Tasks 扩展 | 未接（规范仍是 experimental），长任务靠轮询 |
| 真实公网客户端 | 已用 curl 与内置 `HttpMcpSession` 验证；不同客户端实现差异需按 §6.4 复验 |

---

## 7. 安全清单

- [ ] 公网必须有 TLS（Key 是长期凭据，明文过网等于账号可接管）
- [ ] `TDT_TOKEN_SECRET` 已换成随机值
- [ ] Key 不写进 Git；CI 用环境变量而非 `~/.sortie/config.json`
- [ ] 定期在网页「API 密钥」里吊销不用的 Key
- [ ] 需要精确配额时注意：限流是**进程内**的，多副本时额度 = 副本数 × 240/分钟
