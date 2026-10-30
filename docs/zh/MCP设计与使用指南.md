# Sortie Deck 远程 MCP 设计与使用指南

> 版本：0.5.0 · 更新：2026-10-31
> 对应代码：`src/sortie_deck/mcp_server.py`、`service.py`、`api_keys.py`、`cli_mission.py`

---

## 0. 一页速览

| 问题 | 答案 |
|---|---|
| 暴露的是什么 | 12 个「任务级」MCP 工具，不是 117 个 REST 接口的镜像 |
| 跑在哪 | `sortie api` 进程内的 `/mcp` 路由，与 API 共享 `Orchestrator` 与 `data/` |
| 协议 | MCP Streamable HTTP，**无状态**（不依赖协议级会话 / `Mcp-Session-Id`） |
| 身份 | 用户级 API Key（`sk_sortie_...`），工具参数里没有 `user_id` |
| 长任务 | `start_mission` 立即返回 id，`await_mission` 长轮询到门禁/终态 |
| 门禁 | 结构化对象（summary / risk / irreversible / action_help），三端（网页 / CLI / MCP）互通 |
| 怎么接 | 网页「API 密钥」→ 新建 → 复制 `.mcp.json` 片段 → 粘到本地 Claude Code |

三条命令上手：

```bash
# 1) 起服务（本地）
sortie api

# 2) 网页 http://127.0.0.1:8787 → 右上角「API 密钥」→ 新建 → 复制片段

# 3) 命令行也可以直接用
sortie login --url http://127.0.0.1:8787 --key sk_sortie_xxx
sortie mission start --brief "给用户中心加一个健康检查接口" --track standard --wait
```

---

## 1. 需求与目标

原始诉求（用户视角）：

1. 用户在自己账号下新建 API Key，系统生成"在本地 Claude Code 里配置 MCP"的片段，**复制即用**；
2. 用本地 Claude Code 跑出来的任务**只在自己账号可见**，且网页工作台**实时**能看到；
3. 任务跑到门禁时，三端任选其一处理：Claude Code 里自然语言同意、命令行显式同意、网页点按钮；
4. 配套 CLI 命令；不指定任务 id 时，默认操作"刚刚那个任务"。

由此推导出四个非功能约束：

| 约束 | 来源 |
|---|---|
| 任务归属必须来自凭据，不能来自参数 | 否则任何人可冒充他人建任务 |
| 前端必须实时 | 用户明确要求"界面也能看得到" |
| 门禁必须三端一致 | 否则会出现"网页点了没用、CLI 说已完成"的割裂 |
| 不能把 117 个路由做成工具 | 会污染模型上下文、权限语义混乱 |

---

## 2. 设计思路

### 2.1 MCP 服务端是「一个路由」，不是「一个服务」

```
本地 Claude Code ──HTTP──► api 容器 :8787
                            ├─ /            前端静态（apps/web/dist）
                            ├─ /api/*       现有 REST + SSE
                            └─ /mcp         Streamable HTTP MCP（无状态）
                                 │
                          共享 volume /data（sqlite + missions/）
                                 │
                        worker × N（同一镜像，command=worker）
```

**为什么不做独立进程**：`Orchestrator` 是进程内单例，SSE 读的是 `data/events.sqlite`。
同进程挂载后，"MCP 建的任务在网页上实时可见"是**免费**的——两者天然共享同一份状态。
拆成独立容器就必须共享同一个 `data/` 卷（且只能同机），复杂度上升、收益为零。

代价：MCP 请求与 API 共享事件循环。可接受，因为重活（各阶段 Agent）本来就交给 worker。

### 2.2 无状态：`mission_id` 就是规范里说的那个 handle

2026-07-28 版 MCP 规范移除了协议级会话和 `Mcp-Session-Id`，任意请求可打到任意副本；
需要跨调用状态的服务器自己发一个 handle，当普通参数传。

本项目需要的跨调用状态只有一个——**任务 id**。所以：

- `streamable_http_app(stateless_http=True)`；
- 不维护任何进程级会话表；
- 副本可随便扩、进程重启后客户端用原 Key 继续调用。

### 2.3 身份只来自 Header，工具参数里没有 `user_id`

```python
def _principal_from(ctx: Context) -> Principal:
    headers = {str(k).lower(): str(v) for k, v in (ctx.headers or {}).items()}
    token = headers.get("authorization") or f"Bearer {headers.get('x-api-key','')}"
    return resolve_principal(token, source="mcp")
```

三个要点：

1. **只用 `Context.headers`**，不用 ContextVar。原因是 SDK 的 SessionManager 可能在新任务里处理请求，ContextVar 不保证传递（第一版就踩了这个坑）。
2. 外层中间件做 **fail-fast 门禁**：凭据无效直接 401，不进入 JSON-RPC 处理，错误信息对机器客户端更清晰。
3. 工具层再解析一次作为唯一身份依据——两次解析成本是一次 sha256 + 一次索引查询，可忽略。

### 2.4 工具面：12 个，按「任务语义」命名

映射原则来自业界共识：**暴露 outcomes，不是 endpoints**。所以不暴露
`POST /api/initiatives` + `confirm` + `start` 三步，而是一个 `start_mission`。

| 工具 | 读/写 | 语义 |
|---|---|---|
| `start_mission` | 写（可逆） | 建任务 + 确认航线 + 启动，立即返回 `mission_id` + `queue_depth` |
| `await_mission` | 读 | 长轮询：命中门禁/终态立即返回，超时返回 `still_running` |
| `get_mission` | 读 | 状态 + 门禁；`response_format=concise\|detailed` |
| `list_missions` | 读 | 我创建或被共享的任务 |
| `submit_hitl` | 写（受门禁保护） | `approve` / `reject` / `edit_instruction` / `stop` |
| `stop_mission` | 写（destructive） | 停止任务 |
| `list_artifacts` | 读 | 产物清单 |
| `read_artifact` | 读 | 产物内容（>200k 字符截断） |
| `get_events` | 读 | 按 `seq` 拉事件 |
| `list_tracks` | 读 | 可用航线与阶段 |
| `retrieve_knowledge` | 读 | 知识库检索 |
| `create_memory` | 写 | 写入共享记忆 |

外加资源与提示词：

- `mission://{mission_id}/state` — 任务快照（detailed）
- `mission://{mission_id}/artifact/{path}` — 单个产物文本
- `plan_mission` — 把模糊目标整理成可提交的任务简报

### 2.5 长任务：绝不阻塞工具调用

一个 mission 跑完 `standard` 航线要 10–60 分钟，而工具调用超时通常是几十秒到几分钟。
所以：

```
start_mission(brief)      → {mission_id, status, queue_depth, next: "await_mission ..."}
await_mission(id, 120)    → 命中 waiting_hitl / done / failed 立即返回
                            超时返回 {still_running: true, hint: "..."}
```

第一版**不接 MCP Tasks 扩展**：客户端支持度参差，且规范要求服务端先确认客户端声明了该扩展。
轮询是安全基线，且实现成本最低。

### 2.6 门禁：结构化对象，而不是一句提示语

模型要"自然语言同意"，必须先理解在批准什么。所以门禁是这样的：

```json
{
  "stage": "product_prd",
  "prompt": "Review outputs of `product_prd` and choose an action.",
  "summary": "`product_prd` 产出 prd.md、acceptance.json",
  "risk": "只读评审：产物可回看，驳回会回退到本阶段重做",
  "irreversible": false,
  "options": ["approve", "reject", "edit_instruction", "stop", "reroute", "switch_harness"],
  "action_help": {"approve": "认可本阶段产物，继续下一阶段", "reject": "回退到本阶段重做"}
}
```

- `risk` / `irreversible` 由阶段类型推断：`deploy*` / `ship*` / `release*` / `publish*` 自动标记不可逆；
- `summary` 取阶段自身产物文件名，回落到 upstream；
- `action_help` 用中文说明每个动作的后果，供 agent 复述给人。

### 2.7 三端解门禁：同一个方法 + 通道审计

```
网页按钮 ─┐
CLI      ─┼─► Orchestrator.submit_hitl(decision, author_name, source) ─► 该 mission 的运行锁
MCP 工具 ─┘
```

- `HitlDecision.source` 记录 `web / cli / mcp`，写进时间线与 `meta.last_hitl_by`；
- 先到者生效；后到者若任务已不在 `WAITING_HITL`，返回 `conflict` 并说明"已被 X 处理"；
- 任务被批准后**立刻可能产生下一个门禁**——这是正常行为，不是重复执行。

### 2.8 分层：`cli` / `mcp` → `service` → `orchestrator`

权限判定原本长在路由里（`_ensure_mission_access` 等，直接 `raise HTTPException`）。
如果 MCP 和 CLI 各抄一份，三份权限代码必然漂移。

所以新增 `service.py`：

```python
class Principal:            # user + source + key_id
class ServiceError:         # code → HTTP 状态码 / CLI 退出码 / MCP 错误文本
def ensure_access/control/delete/share_owner(ini, principal)
class MissionService:       # CLI 与 MCP 的唯一入口
```

现有 117 个路由**保持原样**（只把 `require_user` 改成双鉴权），避免大范围回归。

---

## 3. 接口契约

### 3.1 API Key

| 项 | 值 |
|---|---|
| 格式 | `sk_sortie_` + 32 字节 urlsafe 随机 |
| 存储 | `data/api_keys.sqlite`，只存 SHA-256，列表只显示前缀 |
| 明文 | **只在创建响应里出现一次** |
| 字段 | `id / user_id / name / prefix / key_hash / scopes / created_at / last_used_at / revoked_at` |
| `last_used_at` | 60 秒节流（MCP 会高频轮询，没必要每次都写库） |
| 限流 | 每 Key 每分钟 240 次（进程内令牌桶，多副本各算各的） |

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/keys` | 我的 Key 列表（无明文） |
| `POST` | `/api/keys` | 新建，返回 `{key, plaintext, mcp:{url, json, command}}` |
| `DELETE` | `/api/keys/{id}` | 吊销，立即失效 |

创建响应里的 `mcp` 字段已经拼好可直接粘贴的内容：

```json
{
  "url": "https://sortie.example.com/mcp",
  "json": {"mcpServers": {"sortie-deck": {"type": "http", "url": "...", "headers": {"Authorization": "Bearer sk_sortie_..."}}}},
  "command": "claude mcp add --transport http sortie-deck https://.../mcp --header \"Authorization: Bearer sk_sortie_...\""
}
```

### 3.2 鉴权

`require_user` 现在接受两种凭据：

1. 登录 token（浏览器）
2. API Key（MCP / CLI）

两者都解析成 `PublicUser`，下游路由不需要知道调用方是谁。

### 3.3 任务工具的返回结构

```json
{
  "mission_id": "ini_xxx",
  "title": "…",
  "status": "waiting_hitl",
  "stage": "product_prd",
  "updated_at": "2026-10-30T15:08:40+00:00",
  "gate": { "stage": "...", "prompt": "...", "summary": "...", "risk": "...", "irreversible": false, "options": [...], "action_help": {...} },
  "artifact_count": 3,
  "still_running": false
}
```

`get_mission(response_format="detailed")` 额外带 `brief` / `artifacts` / `timeline`。

---

## 4. 本次修改的问题清单

### 4.1 阻塞性问题：没有 LLM key 时任务永远卡在 intake（P-1）

**现象**：不配 `TDT_LLM_API_KEY`（或 key 为空）时，任务打印
`manager: step intake marked complete` 后再不推进，状态永远 `running`。
配任意 key（哪怕地址不可达）反而正常。

**根因**（三层叠加）：

1. `stage_consult.py`、`manager.py`、`agent_forge.py` 里**硬编码** `LlmClient(cli_fallback=True)`，
   把 `TDT_LLM_CLI_FALLBACK=0` 直接绕过去了；
2. 于是每个阶段都去 shell 调 `claude -p`，而 CLI 在非交互场景会挂住；
3. 挂住后按默认 `timeout=60s` 才返回——测试的 30s 预算直接被吃掉。

**修复**：

- 三处改为 `LlmClient()`，尊重 `TDT_LLM_CLI_FALLBACK` / `settings.llm_cli_fallback`；
- 新增 `TDT_LLM_CLI_TIMEOUT`（默认 60s），让 CLI 回退超时可单独收紧；
- `CancelledError` 时杀子进程，避免 claude 变孤儿进程；
- `sortie doctor` 增加 `llm` 检查，半配置（有 base_url/model 没 key）会明确提示。

**效果**：此前失败的 39 个用例从 45–106 秒降到 5.8 秒；全量测试从 4–6 分钟降到 **26 秒**。

### 4.2 挂载 `/mcp` 的 307 重定向

`app.mount("/mcp", sub_app)` 会让 Starlette 把 `POST /mcp` **307 重定向**到 `/mcp/`，
而 MCP 客户端（含 Claude Code）通常不跟随 POST 重定向，表现为 `initialize` 返回空响应。

**修复**：子应用路径设为 `/mcp`，挂载点用根路径：

```python
app.mount("/", build_mcp_app(path="/mcp"))
```

### 4.3 DNS-rebinding 防护导致的 421

SDK 在 host 为 `127.0.0.1` 时会自动开启 DNS-rebinding 保护，并把 Host 白名单限制成
localhost。容器/反代场景 Host 是外网域名，请求直接被判 `421 Invalid Host header`。

**修复**：默认关闭保护（身份由 API Key 保证），需要时用 `TDT_MCP_ALLOWED_HOSTS` 显式开启白名单。

### 4.4 ContextVar 拿不到当前请求

第一版用中间件把 `Principal` 写进 ContextVar，工具里读。但 SDK 的 SessionManager
可能在新任务中处理请求，ContextVar 不保证传递，导致工具里拿不到身份。

**修复**：改用 `Context.headers`（SDK 注入的请求上下文），中间件只保留 fail-fast 401。

### 4.5 容器里前端 404 / 数据丢失

- `WEB_DIST` 是通过 `Path(__file__).parents[3]` 推导的，只有「仓库布局 + editable 安装」成立；
  装进 site-packages 就指错目录。
  **修复**：新增 `TDT_WEB_DIST`，容器里显式设为 `/app/apps/web/dist`。
- `data_dir` 默认是仓库下的 `data/`，容器里不挂卷就是容器层，重建即丢。
  **修复**：容器固定 `TDT_DATA_DIR=/data` + 命名 volume。

### 4.6 门禁信息不足以让 agent 决策

原来只有 `stage / prompt / allowed_actions / artifacts`，模型只能盲批。

**修复**：`HitlRequest` 增补 `artifact_summary` / `risk` / `irreversible` / `action_help`；
`deploy*`/`ship*`/`release*`/`publish*` 自动判不可逆。

### 4.7 三端处理没有审计

**修复**：`HitlDecision.source`，并在 `orchestrator.submit_hitl` 把通道写进
时间线与 `meta.last_hitl_by`；`stop()` 也带 `source`，避免"停止"覆盖掉审批记录。

### 4.8 测试环境依赖外部服务

本地 `.env` 指向真实 LLM 与 WeKnora，导致单测结果随外部服务漂移。

**修复**：`tests/conftest.py` 补 `TDT_MEMORY_RETRIEVAL=local`
（此前已有 `TDT_KB_BACKEND` / `TDT_TOOLKIT_RECALL_BACKEND` / `TDT_LLM_API_KEY=`）。
现在全量 **678 passed**。

---

## 5. 使用方法

### 5.1 网页（推荐起点）

1. 打开工作台（默认 `http://127.0.0.1:8787`，开发模式前端在 `http://localhost:5173`）；
2. 右上角点 **「API 密钥」**；
3. 输入名称（如"我的 MacBook Claude Code"）→ **新建密钥**；
4. 弹窗里立刻出现三样东西，任选其一复制：
   - `.mcp.json` 片段
   - `claude mcp add ...` 命令
   - 明文 Key（**关闭后不可再看**）
5. 把片段粘到项目根目录的 `.mcp.json`，或在终端执行那条命令。

> 吊销：同一面板里点「吊销」，使用该 Key 的 MCP / CLI 立刻 401。

### 5.2 MCP 配置

`.mcp.json`：

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

或命令行：

```bash
claude mcp add --transport http sortie-deck http://<host>:8787/mcp \
  --header "Authorization: Bearer sk_sortie_..."
```

### 5.3 在 Claude Code 里用（自然语言）

```
你：用 sortie-deck 跑一个任务，给用户中心加一个健康检查接口
（模型调用 start_mission → 返回 mission_id，你会看到它开始跑）

你：现在到哪了？
（模型调用 await_mission 或 get_mission，复述当前阶段）

你：让它继续
（模型调用 submit_hitl approve）

你：PRD 里验收标准写得太粗糙，让它补充后再继续
（模型调用 submit_hitl edit_instruction + instruction）
```

模型能"看懂"门禁，是因为返回里带了 `summary` / `risk` / `action_help`，
而不是只有一句 `Review outputs and choose an action`。

### 5.4 CLI

```bash
# 保存凭据（写入 ~/.sortie/config.json，权限 600）
sortie login --url http://<host>:8787 --key sk_sortie_...

# 建任务（默认嵌入式直连；配了 url/key 则自动走远程 API）
sortie mission start --brief "..." --track standard --wait
sortie mission start --brief "..." --json          # 纯 JSON 输出，给脚本/CI
sortie mission start --brief "..." --dry-run       # 只打印将要做什么

# 查看（省略 id = 最近一个任务）
sortie mission status
sortie mission status ini_abc123 --json
sortie mission list --status waiting_hitl

# 解门禁
sortie mission hitl --approve
sortie mission hitl ini_abc123 --reject
sortie mission hitl --instruction "补充错误码约定后继续"

# 产物与跟随
sortie mission artifacts --cat prd.md
sortie mission watch                 # 人类：进度行；--json：NDJSON
```

**「最近一个任务」的解析顺序**（每次都会打印用了哪一条，避免多终端批错任务）：

1. 显式 `--id ini_xxx`
2. 本地指针 `~/.sortie/last_mission.json`（`mission start` 时写入，按服务端地址隔离）
3. 服务端兜底：`GET /api/initiatives?mine=1&limit=1`

第 3 条不能省：任务可能是 Claude Code 通过 MCP 建的，本地指针根本不存在。

**退出码**（脚本可据此分支）：

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 用法错误 |
| 3 | 认证失败 / 无权限 |
| 4 | 任务不存在 |
| 5 | 冲突（已在运行、门禁已被处理） |
| 6 | 参数校验失败 |
| 7 | 可重试（网络、超时、限流） |

### 5.5 三端解同一个门禁

| 通道 | 操作 | 记录 |
|---|---|---|
| 网页 | 门禁卡片点「通过」 | `source=web` |
| 命令行 | `sortie mission hitl --approve` | `source=cli` |
| Claude Code | "让它继续" → `submit_hitl` | `source=mcp` |

先到者生效。后到者收到 `conflict`，消息里带「已被 Ada（Claude Code）处理」。
注意：批准后任务会继续跑到**下一个**门禁，这是正常的，不是重复执行。

### 5.6 自测（不依赖客户端）

```python
from sortie_deck.mcp_http import HttpMcpSession
from sortie_deck.toolkit import ToolkitItem

item = ToolkitItem(kind="mcp", name="sortie", transport="http",
                   url="http://127.0.0.1:8787/mcp",
                   config={"headers": {"Authorization": "Bearer sk_sortie_..."}})
session = HttpMcpSession(item)
await session.start()                                   # tools/list
await session.call_tool("start_mission", {"brief": "..."})
```

`tests/test_mcp_server.py` 用的正是这条路径——**拿项目自带的客户端打自己的服务端**，
既是协议一致性测试，也证明两边能互通。

---

## 6. 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `TDT_PUBLIC_BASE_URL` | 空（回落 `http://{host}:{port}`） | **生成 MCP 片段用的对外地址**，部署必配 |
| `TDT_WEB_DIST` | 空（= `apps/web/dist`） | 前端产物目录，容器里必须显式指定 |
| `TDT_MCP_ALLOWED_HOSTS` | 空 | Host 白名单；留空关闭 DNS-rebinding 防护 |
| `TDT_API_KEY_RATE_LIMIT_PER_MINUTE` | `240` | 每 Key 进程内软限流 |
| `TDT_LLM_CLI_TIMEOUT` | `60` | `claude` CLI 回退超时（挂在阶段关键路径上） |
| `TDT_STORAGE` | `sqlite` | 仅 sqlite，配 json 启动即报错 |
| `TDT_DATA_DIR` | 仓库 `data/` | 容器内应为 `/data` |
| `TDT_RUN_BACKEND` | `inline` | `sqlite` 时配合 `sortie worker` 多副本 |
| `TDT_RUN_WORKER` | `true` | API 副本置 `false`，执行交给 worker |

> ⚠️ `TDT_PUBLIC_BASE_URL` 忘了配，生成的片段里会是 `http://127.0.0.1:8787/mcp`，
> 在别的机器上连不上。这是部署时最容易漏的一项。

---

## 7. 部署

### 7.1 单机 Docker Compose（推荐起步）

```bash
cp .env.docker.example .env       # 填 TDT_TOKEN_SECRET、TDT_PUBLIC_BASE_URL
docker compose build
docker compose up -d
docker compose exec api sortie doctor
curl http://<host>:8787/api/health
```

组成：

| 服务 | 作用 |
|---|---|
| `api` | 前端静态 + REST + `/mcp`；`TDT_RUN_WORKER=false` |
| `worker` | 只消费运行队列；副本数 = 并发任务数 |
| `sortie-data`（volume） | sqlite + `missions/`，api 与 worker 共用 |

扩容：

```bash
WORKER_REPLICAS=3 docker compose up -d --scale worker=3
```

### 7.2 上公网前

1. **必须上 TLS**：API Key 是长期凭据，明文过网等于账号可被接管。
   compose 里已备好可选 profile：
   ```bash
   SORTIE_DOMAIN=sortie.example.com docker compose --profile tls up -d
   ```
2. 把 `TDT_PUBLIC_BASE_URL` 改成 `https://sortie.example.com`；
3. 建议设 `TDT_MCP_ALLOWED_HOSTS=sortie.example.com` 重新打开 Host 校验；
4. 定期备份：`scripts/backup-data.sh`（sqlite 在线备份 + `missions/` 打包，保留 N 份）。

### 7.3 构建镜像的两个环境要求

- 基础镜像默认走 Docker Hub，国内可用 `--build-arg NODE_IMAGE=.../PYTHON_IMAGE=...` 换镜像源；
- `apt-get install git` 需要访问 Debian 源，内网可用 `--build-arg APT_MIRROR=https://<internal>/debian`。

---

## 8. 验收记录（2026-10-30）

| 项 | 结果 |
|---|---|
| 单元/集成测试 | `pytest -q` → **678 passed / 26s** |
| 门禁三端 | `tests/test_hitl_audit.py` 4 项（含并发冲突、来源审计） |
| API Key | `tests/test_api_keys.py` 10 项（含 HTTP 往返、跨用户越权） |
| MCP 协议 | `tests/test_mcp_server.py` 3 项（自客户端回环、无凭据 401、路由挂载） |
| CLI | `tests/test_cli_mission.py` 6 项 + 实机 start→status→hitl→artifacts |
| 真实进程端到端 | 建 Key → `tools/list`（12 个）→ `start_mission` → `await_mission` 到 `product_prd` 门禁 → REST 可见 → `submit_hitl` |
| 容器 | 等价镜像实跑：`/api/health` 200、`/` 200、无凭据 `/mcp` 401、带 Key `tools/list` 正常 |
| 文档站 | `mkdocs build --strict` 无新增告警 |

---

## 9. 已知限制与后续

| 限制 | 影响 | 建议 |
|---|---|---|
| 进程崩溃后 `running` 任务不自动恢复 | 调用方会一直等一个不动的任务 | `await_mission` 已返回 `still_running` + 提示；后续做启动时接管 |
| 一个 worker 一次只跑一个任务 | MCP 并发提交会排队 | `start_mission` 返回 `queue_depth`；扩容 = 加 worker 副本 |
| 跨机器共享存储不支持 | MCP 副本只能与 API 同机 | 需要外部数据库（Postgres repository） |
| 未接 MCP Tasks 扩展 | 无法用规范级异步任务 | 等客户端支持度上来再切；轮询是当前安全基线 |
| 限流是进程内的 | 多副本时实际额度 = 副本数 × 240 | 需要精确配额时换集中式限流 |
| 自身可被配进工具箱 | agent 递归调用自己 | 在 toolkit 绑定处加自身 URL 黑名单（待补） |

---

## 10. 文件索引

| 文件 | 作用 |
|---|---|
| `src/sortie_deck/mcp_server.py` | MCP 服务端：12 工具 + 2 资源 + 1 提示词 + 鉴权中间件 |
| `src/sortie_deck/service.py` | Principal / ServiceError / 权限判定 / MissionService |
| `src/sortie_deck/api_keys.py` | API Key 存储与校验 |
| `src/sortie_deck/cli_mission.py` | `sortie mission` 子命令 |
| `src/sortie_deck/mcp_http.py` | Streamable HTTP 客户端（自测用） |
| `apps/web/src/components/ApiKeysPanel.tsx` | 网页「API 密钥」面板 |
| `docs/zh/modules/mcp.md` / `docs/en/modules/mcp.md` | 站点文档（简版） |
| `docs/plans/2026-10-30-mcp-apikey-cli.md` | 实施计划与逐阶段验收 |
| `docker-compose.yml` / `Dockerfile` / `docker/entrypoint.sh` | 部署 |
