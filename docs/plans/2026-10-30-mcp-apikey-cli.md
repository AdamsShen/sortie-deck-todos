# 计划：用户级 API Key + 远程 MCP + sortie mission CLI

- 日期：2026-10-30
- 状态：**已实施**（P-1 ~ P6 全部完成；见 §11 实施结果）
- 部署前提（已确认）：单机 Docker Compose；先内网 IP、不套 TLS；worker 副本数参数化，默认 2
- 关联：[`2026-10-30-mission-concurrency.md`](./2026-10-30-mission-concurrency.md)（并发/存储改造，已完成）

---

## 1. 需求

### 1.1 用户原始诉求

1. 用户在自己账号下新建 API Key，系统自动生成"在本地 Claude Code 里配置 MCP"的片段，复制粘贴即可用；MCP 里带的就是这个 Key。
2. 用户用本地 Claude Code 通过该 MCP 建的任务，**只在自己账号可见**，且前端工作台**实时**能看到。
3. 任务跑起来后，门禁解除支持三条路：Claude Code 里自然语言同意（模型调工具）、命令行显式同意、网页点按钮，三者互通。
4. 需要配套 CLI 命令；不指定 `ini_xxx` 时，**默认操作"本地 Claude Code 跑出来的最新任务"**。

### 1.2 期望的 CLI 命令（用户给定）

| 场景 | 命令 |
|---|---|
| 跑一次任务 | `sortie mission start --brief "..." --track standard` |
| 看任务卡在哪 | `sortie mission status ini_xxx` |
| 解门禁 | `sortie mission hitl ini_xxx --approve` |
| 看产物 | `sortie mission artifacts ini_xxx --cat prd.md` |
| 跟实时过程 | `sortie mission watch ini_xxx` |
| CI 回归 | `sortie mission start ... --wait --json` |

### 1.3 非目标（本次不做）

- A2A 协议（本项目是工具侧编排器，不是 agent 网络对等节点）。
- OAuth 2.1 授权服务器（v1 复用现有 bearer token 体系承载 API Key）。
- 公网 TLS 与域名（本次先内网 IP 跑通；Caddy 作为 compose 的可选 profile 提供，不默认启用）。
- MCP Tasks 扩展（客户端支持度参差，v1 用轮询）。
- 重写现有 117 个路由（只做 `service.py` 薄抽取）。

---

## 2. 业内调研结论

| 结论 | 依据 |
|---|---|
| 少数 task-shaped 工具，禁止镜像 REST；"暴露结果而非接口" | [Anthropic: Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)、[aimdoc](https://aimdoc.ai/blog/mcp-server-examples-for-saas) |
| 读宽写窄：Tier1 读放开 / Tier2 幂等可逆写 / Tier3 破坏性必须人工确认 + `destructiveHint` | [What to Expose in an MCP Server](https://aimdoc.ai/blog/what-to-expose-to-your-customers-ai-agents) |
| 2026-07-28 移除协议级会话，任意请求可打任意副本；跨调用状态用 server-minted handle 当普通参数 | [MCP changelog 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/changelog) |
| 远程 MCP 令牌需 audience 绑定，禁止令牌透传；service identity 丢失"谁发起的" | [MCP 授权安全章节](http://mcp-docs.cn/specification/2026-07-28/basic/authorization/security-considerations)、[juliatech](https://juliatech.com/blog/auth-patterns-remote-mcp-servers) |
| 长任务：轮询是安全基线，通知是优化；Tasks 需客户端声明能力 | [ecorpit](https://ecorpit.com/mcp-tasks-long-running-async-agent-tools-build-guide-2026)、[Upstash](https://upstash.com/blog/mcp-tasks-and-events) |
| 工具返回加 `response_format: concise\|detailed`（实测 206→72 tokens） | Anthropic 同上 |
| CLI 应"数据与呈现解耦"：`--json` 纯 stdout、日志走 stderr、语义化退出码、默认非交互 | [Google Cloud CLI 规范综述](https://sotasync.com/reader/2026-03-31-cli-for-ai-agents-and-humans/)、[clispec.dev](https://clispec.dev/spec/v0.2) |
| 门禁"是/否提示不够"，必须给出工具名、参数、影响范围、不可逆说明 | [Confirmation Gates](https://www.agentpatterns.ai/security/human-in-the-loop-confirmation-gates/) |

**对本项目的三条直接影响**

1. `mission_id` 就是规范里的 server-minted handle → 无状态 MCP + 轮询，天然对齐。
2. 身份只能从 Key 来，**工具参数里不接受 `user_id`/`owner`**。
3. 门禁是本项目的差异化能力，必须结构化到 agent 可读，而不是一句通用 prompt。

---

## 3. 现状盘点（代码锚点）

| 主题 | 现状 |
|---|---|
| 认证 | `auth.py` 的 `AuthStore`（`data/users.json`）+ PBKDF2；`api/app.py:351 require_user` 解析 `Authorization`。**无 API Key 概念** |
| 权限判定 | 在路由层，直接抛 `HTTPException`：`api/app.py:99/113/129/144/164` |
| 编排 | `orchestrator.py`：`create_initiative:1026`、`confirm_pipeline:1264`、`start_pipeline:1411`、`submit_hitl:2146`、`stop:2397` |
| 并发 | 运行锁按 mission 划分（`:544`），`submit_hitl` 会等该 mission 的运行段结束 |
| 事件 | `event_log.py`（`append/since/latest_seq`），`Orchestrator.events`，SSE 按 `seq` 续读 |
| 存储 | 仅 sqlite（`settings.py:28`），旧 JSON 已自动迁移 |
| MCP 客户端 | 已完备：`mcp_runtime.py`（stdio）、`mcp_http.py:164`（Streamable HTTP，`protocolVersion=2025-03-26`，兼容 JSON/SSE 双响应） |
| MCP 服务端 | **不存在** |
| MCP SDK | 已装 `mcp 2.3.0`（传递依赖）；v2 中 `FastMCP` → `MCPServer`；`streamable_http_app(stateless_http=True)` 可用 |
| CLI | `smoke/doctor/knowledge/api/worker`，**无 mission 命令**；`cli.py:18 _resolve_owner_id` 用"猜第一个 admin"绕开身份问题 |
| 列表接口 | `GET /api/initiatives` 只有 `status` 参数，无 `mine`/`limit` |
| 门禁模型 | `models.py:255 HitlRequest`：stage / prompt / allowed_actions / artifacts（半结构化） |

---

## 4. 总体设计

### 4.1 数据流

```
本地 Claude Code
  │  POST https://<host>/mcp   Authorization: Bearer sk_sortie_xxx
  ▼
FastAPI 进程（同一进程内）
  ├─ /mcp          MCP 无状态服务端（12 工具 + 3 资源）
  ├─ /api/*        现有工作台接口
  │        │
  │        └── MissionService ──► Orchestrator（单例）
  │                                   │
  ├─ 事件表 data/events.sqlite ◄──────┘ _emit
  └─ /api/initiatives/{id}/events （SSE 按 seq 续读）──► 前端工作台（实时）
```

**同进程挂载是关键决策**：MCP 与 API 共享单例 `Orchestrator` 与同一 `data_dir`，所以「MCP 建任务 → 工作台实时可见」不需要任何额外同步机制。

### 4.2 分层

```
cli.py / mcp_server.py          ← 两个入口，参数解析 / 工具 schema
        │
   service.py                   ← 本次新增：Principal + 权限 + 异常 + MissionService
        │
   orchestrator.py              ← 不变
```

`service.py` 只做**薄抽取**，现有路由保持原样（避免 117 个路由的回归风险），但把身份与权限判定收敛成可复用的函数。

---

## 5. 分阶段实施

### P-1 — 修复「无可用 LLM 时卡在 intake」（1 天，先做）

**现象**：不配 LLM key（或 key 为空）时，任务在 `intake` 打印 `manager: step intake marked complete` 后再不推进，状态永远 `running`；配任意 key（哪怕地址不可达）则正常走降级并推进到下一个门禁。已在改动前基线上复现两次，与并发/存储改造无关。

**已知线索**：
- `llm.py:26` 默认 `timeout=60.0`；`:182` 有 claude CLI 回退分支 `proc.communicate(timeout=self.timeout)`；`:70/:79/:118` 按 `api_key` 是否存在分流。
- `manager.py:331 completion_note()` 是上述日志的唯一来源。
- `tests/conftest.py` 已置空 `TDT_LLM_API_KEY` 并关闭 `TDT_LLM_CLI_FALLBACK`，但测试仍会卡住 → 说明阻塞不在 CLI 回退这一条路径上。

**要做的事**：
1. 在 `intake` 阶段（`graph.py` 的 intake 节点 / `pipeline_llm.plan_pipeline_smart` / `manager._llm_*`）加断点式日志，确认究竟是哪一次 `await` 不返回。
2. 消除"无 LLM"路径上的无限等待：所有 LLM 调用都必须有**有限超时 + 明确降级**，且降级后要显式发出「继续推进」信号，而不是让阶段静默挂住。
3. 补回归测试：`TDT_LLM_API_KEY=""` 且 `TDT_LLM_CLI_FALLBACK=0` 时，mock 管线必须能在 10 秒内推进到 `product_prd` 门禁。
4. 顺带支持启动期校验：`llm_base_url`/`llm_model` 配了但 `llm_api_key` 为空时，`doctor` 给出告警（半配置状态最容易出这种问题）。

**验收**：无 key 环境下 `sortie smoke` 与 `tests/test_orchestrator_flow.py` 全绿；配不可达 key 时行为不变。

> 不修这个，后面所有 MCP / CLI 的端到端验收都得靠"设一个假地址"绕过去，不可接受。

### P0 — 基座：API Key + service 层（2 天）

**新增 `src/sortie_deck/api_keys.py`**

```python
class ApiKey(BaseModel):
    id: str            # key_xxxxxxxx
    user_id: str
    name: str          # 用户可见的备注，如 "我的 MacBook Claude Code"
    prefix: str        # sk_sortie_live_ab12（列表只显示这个）
    key_hash: str      # sha256(明文)，明文永不落库
    scopes: list[str]  # v1 固定 ["*"]，预留
    created_at: str
    last_used_at: str | None
    revoked_at: str | None

class ApiKeyStore:      # sqlite: data/api_keys.sqlite（WAL + busy_timeout + 线程锁）
    def create(self, user_id, name) -> tuple[ApiKey, str]   # 返回 (记录, 明文)
    def verify(self, raw: str) -> PublicUser | None          # 校验 + 刷新 last_used_at
    def list_for_user(self, user_id) -> list[ApiKey]
    def revoke(self, user_id, key_id) -> None
```

- Key 格式：`sk_sortie_live_` + 32 字节 urlsafe 随机；**只在创建响应里明文出现一次**。
- hash 用 SHA-256（随机高熵，无需慢哈希）；比较用 `hmac.compare_digest`。
- 日志一律不打印明文与完整 hash（只打 `prefix`）。

**新增 `src/sortie_deck/service.py`**

```python
@dataclass
class Principal:
    user: PublicUser
    source: str          # "web" | "cli" | "mcp"
    key_id: str | None = None

class ServiceError(Exception):
    code: str            # not_found / forbidden / conflict / validation / pipeline_not_confirmed ...
    http_status: int     # 映射到 HTTP；CLI 侧映射到退出码

def resolve_principal(authorization: str | None, *, source: str) -> Principal
def ensure_access(ini, principal) -> None      # 迁移 api/app.py:144 的语义
def ensure_control(ini, principal) -> None     # 迁移 api/app.py:129 的语义
def ensure_delete(ini, principal) -> None      # 迁移 api/app.py:99 的语义

class MissionService:                          # CLI 与 MCP 共用
    async def create(...) -> Initiative
    async def start(mission_id) -> Initiative
    async def status(mission_id) -> Initiative
    async def hitl(mission_id, decision) -> Initiative
    async def artifacts(mission_id) -> list[ArtifactRef]
    async def read_artifact(mission_id, path) -> str
    async def events(mission_id, since_seq) -> list[tuple[int, dict]]
    async def resolve_latest(principal) -> Initiative     # 见 §6.5
```

**改造 `api/app.py`**
- `require_user` 增加 API Key 分支：先试 `AuthStore.resolve`（登录 token），再试 `ApiKeyStore.verify`（`sk_sortie_` 前缀）。
- 新增 `POST/GET/DELETE /api/keys`（只能操作自己的；创建响应含 `plaintext`）。

**改造 `settings.py`**：新增 `public_base_url: str = ""`（为空时回落 `http://{host}:{port}`）。

**验收**
- 用 Key 调任意鉴权接口返回 200；用户 A 的 Key 无法访问 B 的任务（403）。
- `GET /api/keys` 不返回明文；吊销后立即失效。

---

### P1 — CLI：`sortie mission`（2 天）

**新增 `src/sortie_deck/cli_mission.py`**（`cli.py` 只挂子解析器，避免单文件膨胀）

```bash
sortie login   --url https://host --key sk_...     # 写 ~/.sortie/config.json (chmod 600)
sortie logout
sortie mission start   --brief "..." [--track standard] [--title "..."]
                       [--harness mock|claude_code] [--wait] [--json] [--dry-run]
sortie mission status   [ini_xxx] [--json]
sortie mission hitl     [ini_xxx] --approve | --reject | --instruction "..." [--json]
sortie mission artifacts [ini_xxx] [--cat <path>] [--json]
sortie mission watch    [ini_xxx] [--json]        # 人类：进度渲染；--json：NDJSON
sortie mission list     [--status running] [--json]
```

**运行模式**
- 默认**嵌入式**：进程内构造 `Orchestrator` + `MissionService`（复用 `cli.py:39 _smoke` 已验证的路径），需要 `data_dir` 可达。
- `--remote`：走 HTTP 客户端（`httpx`），适合 CI 打远程服务。
- 默认值来源：`~/.sortie/config.json` 的 `url`/`key`，可用环境变量 `SORTIE_URL` / `SORTIE_API_KEY` 覆盖。

**输出约定（对齐业界）**
- `--json`：**纯数据到 stdout**；进度、日志、提示全部走 stderr。
- 退出码：`0` 成功 / `2` 用法错误 / `3` 认证失败 / `4` 任务不存在 / `5` 冲突（已在运行、门禁已被处理）/ `6` 参数校验失败 / `7` 可重试（网络、超时）。
- 每个会改变状态的命令支持 `--dry-run`（只打印将执行的请求与目标任务）。

**验收**
- 嵌入式与 `--remote` 两种模式跑通「start → status → hitl → done」。
- `--json` 输出可直接被 `jq` 消费；失败时 stdout 为空、stderr 有结构化错误。
- `--dry-run` 不产生任何写入。

---

### P2 — 门禁结构化 + 三端来源审计（1.5 天）

**改造 `models.py`**

```python
class HitlRequest(BaseModel):
    stage: str
    prompt: str
    allowed_actions: list[HitlAction]
    artifacts: list[ArtifactRef]
    # 新增
    artifact_summary: str = ""          # 一句话：这批产物是什么、看哪个文件
    risk: str = ""                      # "只读评审" / "会推代码" / "不可逆发布"
    irreversible: bool = False
    action_help: dict[str, str] = {}    # 每个 action 的语义说明，给 agent 读

class HitlDecision(BaseModel):
    ...
    source: str = "web"                 # "web" | "cli" | "mcp"
```

**改造 `orchestrator.submit_hitl`**
- 接收 `source`，写进 timeline 与房间消息（"Ada 通过 Claude Code 批准"）。
- 已被处理（非 `WAITING_HITL`）时，返回体带上 `handled_by` / `handled_source`，三端据此显示"已被处理"。

**门禁生成点**：`graph.py` 构造 `HitlRequest` 处补 `artifact_summary` / `risk` / `irreversible` / `action_help`（deploy 阶段标 `irreversible=True`）。

**验收**
- 三端对同一门禁并发处理：只有第一次生效，其余返回 conflict 且带"已被 X 于 T 处理"。
- 时间线与房间消息能看出处理通道。

---

### P3 — MCP 服务端（4 天）

**依赖**：`pyproject.toml` 增加 `mcp>=2.3,<3`（当前是传递依赖，必须显式声明并锁定）。

**新增 `src/sortie_deck/mcp_server.py`**

```python
from mcp.server.mcpserver import MCPServer

server = MCPServer("sortie-deck", instructions="...")

@server.tool()
async def start_mission(brief: str, title: str | None = None,
                        track: str = "auto", wait_seconds: int = 0) -> dict: ...

# ... 其余工具
```

**挂载**（`api/app.py`）

```python
app.mount("/mcp", build_mcp_app())   # streamable_http_app(streamable_http_path="/mcp",
                                     # json_response=False, stateless_http=True)
```

注意：`streamable_http_app` 返回的是独立 ASGI app，需要用中间件把 `Authorization` 解析出的 `Principal` 放进请求上下文（每请求解析，不依赖进程状态，符合无状态语义）。

**工具面（12 个）**

| 工具 | 语义 | 读/写 | 关键返回 |
|---|---|---|---|
| `start_mission` | 建任务 + 确认航线 + 启动（一步到位） | 写（可逆） | `mission_id`、航线、`queue_position` |
| `get_mission` | 单任务详情（`response_format`） | 读 | status / stage / 门禁摘要 / 最近产物 |
| `list_missions` | 我的任务列表 | 读 | `[{id,title,status,stage,updated_at}]` |
| `await_mission` | 长轮询，命中门禁/终态立即返回 | 读 | 同上 + `still_running: bool` |
| `submit_hitl` | 批准/驳回/改指令 | 写（受门禁保护） | 新状态 + `handled_by` |
| `stop_mission` | 停止 | 写 | 状态 |
| `list_artifacts` | 产物清单 | 读 | `[{path,kind,stage,size}]` |
| `read_artifact` | 读单个产物 | 读 | 内容（带截断上限） |
| `get_events` | 按 `since_seq` 取事件 | 读 | `{events, next_seq}` |
| `list_tracks` | 可用航线与阶段 | 读 | 航线 → 阶段 |
| `retrieve_knowledge` | 知识库检索 | 读 | 命中片段 |
| `create_memory` | 写入共享记忆 | 写 | 记录 id |

**资源**：`mission://{id}/state`、`mission://{id}/artifact/{path}`、`mission://{id}/events`。
**prompt**：`plan_mission`（引导模型按航线补齐 brief）。

**硬性规则**
1. 工具参数**不含** `user_id` / `owner`，身份只来自 Key。
2. `start_mission` 立即返回，不阻塞；超时控制由 `await_mission(timeout_s)` 承担。
3. 每个工具标注 MCP annotations：`readOnlyHint` / `destructiveHint`（`stop_mission`、未来的 ship/deploy 为 true）。
4. 防递归：拒绝把本服务自身 URL 写入工具箱（在 `toolkit` 绑定处校验）。

**自测夹具**：直接用 `mcp_http.py:164 HttpMcpSession` 指向自身 `/mcp`，断言 `tools/list` 数量与 `start_mission` 的返回结构。

**验收**
- Claude Code 通过 `.mcp.json` 调通 `tools/list` / `start_mission` / `await_mission`。
- 服务端重启后同一 Key 仍可用（无状态）。
- 两个并发请求分别打到不同副本（模拟）互不影响。

---

### P4 — 前端：API Keys 管理 + 连接片（2 天）

- 账号设置页新增 **API Keys**：列表（名称、前缀、创建时间、最后使用、吊销）。
- 新建弹窗：名称输入 → 一次性展示明文 + 两段可复制内容：
  - `.mcp.json` 片段（`"type": "http"` + `url` + `Authorization` 头）
  - `claude mcp add --transport http sortie-deck https://<host>/mcp --header "Authorization: Bearer ..."`
- 任务详情页：门禁卡片显示 `risk` / `irreversible` / 产物摘要；处理后显示"已被 X（通道）处理"。
- 文案进 `apps/web/src/i18n.ts`（中英）。
- 注意：`App.tsx` / `i18n.ts` 刚被 `dfcb680` 改过，需基于最新内容改。

**验收**
- 复制片段在本地 Claude Code 粘贴即通（实测一次）。
- 吊销后本地 MCP 立即 401，界面有明确提示。

---

### P5 — 联调、加固、文档（1.5 天）

- 越权矩阵测试：A 的 Key 访问 B 的任务 / B 的门禁 / B 的产物，全部 403。
- API Key 限流（简单令牌桶，按 key 维度）+ 日志脱敏检查。
- `await_mission` 超时语义、进程重启后 `running` 任务的处置说明。
- 文档：`README`（配置项 `TDT_PUBLIC_BASE_URL`）、`CLAUDE.md`（新模块 `api_keys.py` / `service.py` / `mcp_server.py`）、`docs/zh|en/modules/cli.md`、新增 `docs/zh|en/modules/mcp.md`、`CHANGELOG.md`。

---

### P6 — 容器化与单机 Compose 部署（2.5 天）

**已定前提**：单机 Docker Compose；**先内网 IP 跑通、不套 TLS**；worker 副本数参数化（默认 2）。

#### 拓扑

```
浏览器 / 本地 Claude Code
        │  HTTP  http://<内网IP>:8787
        ▼
┌─────────────────────────────────────────┐
│ api 容器（与 worker 共用同一镜像）        │
│   /            前端静态 apps/web/dist    │
│   /api/*       现有 REST + SSE           │
│   /mcp         Streamable HTTP MCP       │
└───────────────┬─────────────────────────┘
                │  volume: sortie-data → /data
      ┌─────────┴──────────┐
      │ worker-1 … worker-N │  同镜像，command: sortie worker
      └────────────────────┘
```

**为什么 `/mcp` 放在 api 容器里**：复用进程内 `Orchestrator` 单例与 `data/events.sqlite`，MCP 写入的事件能被 API 进程的 SSE 立刻读到 →「Claude Code 建的任务，网页不刷新可见」零成本成立。若拆成独立容器，就必须保证两个容器挂同一个 volume 且同机，复杂度更高、收益为零。

#### 新增文件

| 文件 | 内容 |
|---|---|
| `Dockerfile` | 多阶段：`node:22` 构建 `apps/web/dist` → `python:3.12-slim` 运行时；`pip install -e .`（注意见下）；非 root 用户；`HEALTHCHECK` 打 `/api/health` |
| `docker-compose.yml` | `api`（ports 8787）+ `worker`（`deploy.replicas: ${WORKER_REPLICAS:-2}`）+ 命名 volume `sortie-data`；共用 `env_file: .env`；`restart: unless-stopped` |
| `.dockerignore` | 排除 `.venv` `node_modules` `data` `apps/web/dist` `.git` |
| `.env.docker.example` | 容器专用环境变量模板（`TDT_DATA_DIR=/data`、`TDT_PUBLIC_BASE_URL`、`TDT_RUN_BACKEND=sqlite`、`TDT_STORAGE=sqlite`、`TDT_RUN_WORKER`） |
| `deploy/caddy.Caddyfile`（可选 profile） | 后续上域名时启用：`caddy` 服务 + `--profile tls` |
| `scripts/backup-data.sh` | 停写窗口内 `sqlite3 .backup` + 打包 `missions/` 到 `/data/backups/`，保留 N 份 |

#### 三个必须处理的镜像细节

1. **`WEB_DIST` 是相对源码路径算出来的**
   `api/app.py:2659`：`WEB_DIST = Path(__file__).resolve().parents[3] / "apps" / "web" / "dist"`。只有「仓库根目录 + editable 安装」时才成立；若 `pip install .` 装进 site-packages，`parents[3]` 会指到 site-packages 里，前端 404。
   → **方案：新增 `TDT_WEB_DIST` 配置项**（默认沿用现有计算），镜像里显式设为 `/app/apps/web/dist`。这样镜像既可用 editable 也可用普通安装，不依赖目录布局魔法。

2. **数据目录必须可配**
   `settings.py:15` 默认 `data_dir = REPO_ROOT / "data"`。容器里必须 `TDT_DATA_DIR=/data` 并挂 volume，否则数据落在容器层，重建即丢。

3. **worker 必须与 api 用同一镜像同一 volume**
   `sortie worker` 要读同一个 `runs.sqlite` / `events.sqlite` / `missions/`；且 api 端必须 `TDT_RUN_WORKER=false`，否则 api 进程也会抢 job。

#### 部署步骤（写进 README）

```bash
cp .env.docker.example .env          # 填 TDT_TOKEN_SECRET、TDT_PUBLIC_BASE_URL=http://<内网IP>:8787
docker compose build
docker compose up -d
docker compose exec api sortie doctor
curl http://<内网IP>:8787/api/health
```

- 首次启动自动完成 `initiatives.json → sqlite` 迁移（若 volume 里是旧数据）。
- 升级：`docker compose pull/build && docker compose up -d`；迁移与建表都在启动时幂等执行。
- 扩容：`WORKER_REPLICAS=3 docker compose up -d --scale worker=3`（并发数 = worker 副本数）。

#### 验收

- 全新机器（空 volume）`docker compose up -d` 后：`/api/health` 200、`/` 返回前端页面、`/mcp` 可被 Claude Code 连接。
- 从旧数据卷升级：17 条任务完整迁移，备份文件出现在卷里。
- `docker compose stop api` 期间 worker 不报错；`start` 后继续消费队列。
- 杀掉 worker 容器中的任务进程 → 租约到期后任务可被另一个 worker 重新领取（`heartbeat` 已实现）。

---

## 6. 关键设计细节

### 6.1 为什么 MCP 与 API 同进程

- 单例 `Orchestrator` + 同一 `data_dir` → SSE 与 MCP 写的事件天然同源，前端实时性零成本。
- 插件不需要额外进程管理；`sortie api` 一个命令起全部。
- 代价：MCP 请求与 API 共享事件循环，重活仍是 `worker` 在做，影响可控。

### 6.2 为什么 v1 不做 OAuth

现有 `AuthStore` 已是 bearer token；API Key 只是"更长的 token"。引入 OAuth 2.1 要额外维护授权服务器、client 注册、PKCE、audience 校验，收益只在"对外开放给第三方应用"。内部/单租户场景不划算。

### 6.3 为什么 `start_mission` 一步到位

用户诉求是"本地 Claude Code 跑一个任务"。若照搬 REST 的三步（建 → 确认 → 启动），模型要在三个工具调用间保持中间状态，极易出错。`start_mission` 内部按 `auto_confirm=True` 走完，返回值给出最终航线，模型可据此复述。

### 6.4 门禁为什么必须结构化

模型要"自然语言同意"，必须先理解在批准什么。只有 `prompt + 六种 action` 时，模型只能瞎猜。补 `risk` / `irreversible` / `artifact_summary` / `action_help` 后，模型可以：

```
任务在 product_prd 门禁。产物 prd.md（12 个验收点），风险：只读评审，可逆。
可选：approve / reject / edit_instruction / stop。
```

### 6.5 「默认最新任务」解析顺序

```
1. 显式 --id ini_xxx
2. ~/.sortie/last_mission.json（mission start 写入；按 user+host 隔离）
3. 服务端兜底：GET /api/initiatives?mine=1&limit=1（需要新增参数）
```

第 3 条不可省：任务可能是 Claude Code 通过 MCP 建的，CLI 本地根本没有指针。

**每次解析都打印来源**：

```
→ 使用最新任务 ini_a1b2c3（2 分钟前创建，waiting_hitl @ product_prd）
```

歧义处理：`--id` 与本地指针都失效时，若服务端返回多条近 10 分钟内创建的任务，直接报错要求显式指定，不猜。

---

## 7. 备选方案

| 方案 | 做法 | 结论 |
|---|---|---|
| A. 官方 SDK `MCPServer`（推荐） | `streamable_http_app(stateless_http=True)` 挂载 | 已装 2.3.0，实现量最小 |
| B. 手写 JSON-RPC 适配 | 复用 `mcp_http.py` 的解析逻辑反向 | 无新依赖，但要自己实现协议细节，约 500 行，不划算 |
| C. `fastapi_mcp` 从 OpenAPI 生成 | 自动把路由转工具 | 会把 117 个路由全暴露，且权限语义混乱，否决 |
| D. 独立 MCP 进程 | `run_streamable_http_async()` | 需保证与 API 共享 data_dir 且不抢 worker；留作后续扩容形态 |

---

## 8. 风险与未决问题

| 风险 | 影响 | 处置 |
|---|---|---|
| **既有 bug：无 LLM key 时任务卡在 intake** | MCP/CLI 端到端验收会卡死 | 验收环境显式设一个不可达的 `TDT_LLM_BASE_URL`（实测可正常走降级）；建议单独开一条修复 |
| `claude mcp add` 具体语法需实测 | P4 文档可能不准 | P3 阶段先用 `.mcp.json` 验证；`claude mcp add` 在 P4 实测后再写死 |
| 一个 worker 一次只跑一个任务 | MCP 并发提交排队 | `start_mission` 返回 `queue_position`；文档说明扩容 = 加 worker 副本 |
| 进程重启后 `running` 任务不恢复 | 客户端可能永远等 | `await_mission` 返回 `still_running` + `last_event_at`，超时后提示"可能已失联" |
| SQLite 不支持跨机器共享 | MCP 副本只能与 API 同机 | 文档写明；跨机器需先补 Postgres repository |
| API Key 长期有效且公网可达 | 泄露即账号接管 | 只显示一次、可吊销、记 `last_used_at`、限流、日志脱敏 |
| `auth.py` 仍是 JSON 存储 | 多进程下 login/logout 有丢更新风险 | 本次不动；API Key 单独走 sqlite |
| 自建 MCP 被绑进任务工具箱 | agent 递归调用自己 | `toolkit` 绑定处加自身 URL 黑名单 |
| `WEB_DIST` 按源码相对路径计算（`api/app.py:2659`） | 镜像里前端 404 | 新增 `TDT_WEB_DIST` 配置项，镜像显式指定 |
| 容器内 `data_dir` 默认落在仓库目录 | 重建容器丢数据 | `TDT_DATA_DIR=/data` + 命名 volume |
| 内网 IP 无 TLS，API Key 明文过网 | 内网嗅探即失窃 | 本阶段限定内网；上公网前必须启用 Caddy profile 或前置反代 |
| 单机单卷是单点 | 宿主机故障全停 | 文档给出 `/data` 备份脚本与恢复步骤；数据库跨机器方案另行规划 |

---

## 9. 验收清单（整体）

1. 网页新建 Key → 复制 `.mcp.json` → 本地 Claude Code `tools/list` 成功。
2. `start_mission` 建的任务在网页上以自己的账号出现，运行中事件**不刷新页面**即可见。
3. 同一门禁：Claude Code 自然语言批准 / `sortie mission hitl --approve` / 网页按钮，任一处理后另两端显示"已被处理"，且只执行一次。
4. `sortie mission status`（不带 id）能定位到 MCP 刚创建的任务，并打印解析来源。
5. `mcp_http.py` 自客户端回环测试通过。
6. 越权矩阵：A 的 Key 访问 B 的一切资源均 403；工具参数传 `user_id` 无效。
7. 空配置（无 LLM key）环境下任务能正常推进到第一个门禁，不再卡在 `intake`。
8. 干净机器 `docker compose up -d` 起完整系统；`/api/health`、`/`、`/mcp` 三者同时可用。
9. `WORKER_REPLICAS=3` 时三个任务能并行推进（每个 worker 一个）。
10. `ruff check` 对新增文件零告警；`pytest -q` 不因本次改动新增失败。

---

## 10. 工期

| 阶段 | 内容 | 天数 |
|---|---|---|
| P-1 | 修复「无 LLM 卡 intake」 | 1 |
| P0 | API Key + service 层 + 双鉴权 | 2 |
| P1 | `sortie mission` CLI | 2 |
| P2 | 门禁结构化 + 来源审计 | 1.5 |
| P3 | MCP 服务端（12 工具 + 3 资源） | 4 |
| P4 | 前端 Key 管理 + 连接片 | 2 |
| P5 | 联调加固 + 文档 | 1.5 |
| P6 | 容器化 + 单机 Compose 部署 | 2.5 |
| | **合计** | **16.5** |

每阶段独立可验收：

- P-1 结束 → 空配置也能跑通 mock 全流程，测试环境不再依赖假地址；
- P1 结束 → CLI 能独立跑通 start/status/hitl/artifacts/watch；
- P3 结束 → 本地 Claude Code 能通过 `/mcp` 驱动任务；
- P6 结束 → 一条 `docker compose up -d` 在干净机器上起来完整系统。

P6 可以在 P3 之后并行开工（不依赖 P4 前端细节），实际日历工期约 **14 天**。


---

## 11. 实施结果（2026-10-30）

| 阶段 | 结果 | 关键验证 |
|---|---|---|
| P-1 | 完成 | `stage_consult` / `manager` / `agent_forge` 三处硬编码 `cli_fallback=True` 改为读配置；CLI 回退独立超时 `TDT_LLM_CLI_TIMEOUT`；取消时杀子进程。`tests/test_llm_offline.py` 3 项；此前超时的 39 个用例从 45–106s 降到 5.8s |
| P0 | 完成 | `api_keys.py`（sha256 存储 + 前缀 + last_used 节流）、`service.py`（Principal / ServiceError / ensure_* / MissionService）、`require_user` 双鉴权、`POST/GET/DELETE /api/keys` + MCP 片段生成。`tests/test_api_keys.py` 10 项 |
| P1 | 完成 | `sortie login/logout` + `sortie mission start/status/list/hitl/artifacts/watch`；嵌入式 / 远程双模式；`--json` 纯 stdout；语义化退出码；latest 解析（显式 > 本地指针 > 服务端）。`tests/test_cli_mission.py` 6 项，实机跑通 start→status→hitl→artifacts |
| P2 | 完成 | `HitlRequest` 增 `artifact_summary/risk/irreversible/action_help`；`HitlDecision.source`；三端来源写入时间线与 `meta.last_hitl_by`；重复提交返回 conflict 并说明"已由谁处理"。`tests/test_hitl_audit.py` 4 项 |
| P3 | 完成 | `mcp_server.py`：12 工具 + 2 资源 + 1 prompt；`stateless_http=True`；身份只取 `Context.headers`；挂载在根路径避免 `Mount("/mcp")` 的 307。`tests/test_mcp_server.py` 3 项（用项目自带 `HttpMcpSession` 打自己）+ 真实进程端到端 |
| P4 | 完成 | `ApiKeysPanel.tsx`（新建/列表/吊销 + 一键复制 `.mcp.json` 与命令）、`api.ts`/`types.ts` 封装、中英文案；`npm run typecheck` 与 `npm run build` 通过 |
| P5 | 完成 | 每 Key 令牌桶限流（429）、跨用户越权用例、`docs/{zh,en}/modules/mcp.md`、README/CLAUDE/CHANGELOG 更新；`mkdocs build --strict` 仅剩 3 条既有断链 |
| P6 | 完成（镜像未在本机构建） | `Dockerfile`（多阶段，base image 可参数化）、`docker-compose.yml`（api + worker×N + 可选 Caddy）、`docker/entrypoint.sh`、`.env.docker.example`、`scripts/backup-data.sh`；`docker compose config` 通过；用等价镜像实跑验证 health / 首页 / `/mcp` 401 / 建 Key 后调通 tools/list |

**全量测试**：`678 passed`（此前 3 个因本地 `.env` 指向真实 LLM/WeKnora 而偶发失败的用例，已通过 `tests/conftest.py` 固定走降级路径修掉）。

**未在本机验证的一环**：`Dockerfile` 的 `apt-get install git` 需要访问 Debian 源，本机网络不可达；已加 `--build-arg APT_MIRROR=` 供内网切换。上线前请在目标机器执行一次 `docker compose build`。
