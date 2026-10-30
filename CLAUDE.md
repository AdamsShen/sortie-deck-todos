# CLAUDE.md

本文件为 Claude Code / AI 编码助手提供本仓库（**Sortie Deck**）的工作指引。请在改动代码前先阅读本文件。

## 项目是什么

**Sortie Deck**（PyPI `sortie-deck` / 导入 `sortie_deck`）是一个 **AI 原生的办公迭代控制平面**。它为每个任务（mission / initiative）编排一组角色 Agent，用 **LangGraph** 驱动流水线，在关键节点保留 **人在环（HITL）** 审批，并通过 **产物契约（contract）** 在阶段之间传递结果 —— 让 Product → Eng → QA → Deploy 不再依赖人工复制粘贴。

- 定位：**控制平面**，而非又一个聊天机器人。任务、阶段、HITL、产物都是一等公民。
- 可插拔执行器：`mock`、`claude_code`、`cursor_cli`、`deploy_shell`，可选 LLM。
- 技术栈：Python 3.12+ / FastAPI / LangGraph / Pydantic v2；前端 React 19 + TypeScript + Vite。
- 许可证：Apache-2.0。当前版本 `0.1.0`（大量 `[Unreleased]` 变更见 `CHANGELOG.md`）。

## 常用命令

```bash
# 安装（含开发与文档依赖）
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,docs]"

# 运行测试 / Lint（CI 校验项）
pytest -q
ruff check src tests
ruff format src tests            # 如需格式化

# 冒烟：用 mock 管线跑 interrupt/resume 全流程
sortie smoke                     # --brief "..." --no-auto-approve
sortie doctor                    # 运行时体检（secret / data_dir / 端口 / 存储后端）

# 启动 API（默认 http://127.0.0.1:8787）
sortie api

# 执行端独立扩容（需 TDT_RUN_BACKEND=sqlite + TDT_STORAGE=sqlite；
# API 端配 TDT_RUN_WORKER=false，否则它会抢同一个 job）
sortie worker --id w1

# 任务操作（默认嵌入式直连；配了 ~/.sortie/config.json 则走远程 API）
sortie login --url http://<host>:8787 --key sk_sortie_...
sortie mission start --brief "..." --track standard [--wait] [--json]
sortie mission status [ini_xxx]      # 省略 id = 最新任务
sortie mission hitl [--approve|--reject|--instruction "..."]
sortie mission artifacts [--cat prd.md]
sortie mission watch

# 启动前端工作台（默认 http://127.0.0.1:5173，需 API 已运行）
cd apps/web && npm install && npm run dev
npm run typecheck                # tsc -b，CI 校验项
npm run build

# 文档站（MkDocs Material，中英双语）
mkdocs serve                     # http://127.0.0.1:8000
mkdocs build --strict            # CI 校验项

# 一键同时拉起 API + 前端
bash scripts/start-dev.sh
```

一次完整改动交付前的验证顺序：`ruff check src tests` → `pytest -q` → `apps/web` 内 `npm run typecheck` → 改文档则 `mkdocs build --strict`。

## 架构总览

```
Web 工作台 (apps/web, React+TS)
        │  HTTP / SSE
        ▼
FastAPI 控制面 (src/sortie_deck/api/app.py)
        │
        ▼
Orchestrator (orchestrator.py) ──► LangGraph 图 (graph.py)
        │                              │
        ├─► JSON/SQLite 存储 (store.py, repositories/)
        ├─► 产物存储 (artifacts.py / artifacts_s3.py)
        ├─► Run 队列 (run_queue.py, 多 worker)
        └─► 执行器注册表 (registry.py + plugins/) ──► claude_code / cursor_cli / deploy_shell / mock
```

数据流：创建任务 → **航线规划官** 提案阶段 → 人工确认 → 编译 LangGraph → 逐阶段执行（交付物写 `data/missions/<id>/artifacts/<stage>/`，过程文件写 `logs/<stage>/`，输入资料在 `input/`）→ 命中 HITL 时 `interrupt()` 暂停并等 `Command(resume=...)` → 全程经 SSE 推送事件到工作台。

## 关键文件地图

**核心（改动前必读）**
- `src/sortie_deck/orchestrator.py` — 编排中枢：任务生命周期、图编译/调度、HITL 提交、房间事件扇出（`Orchestrator` 类 + `get_orchestrator()` 单例）。
- `src/sortie_deck/graph.py` — 把航线编译为 LangGraph 状态机：`build_graph_from_template()`、阶段节点、并行波次节点、HITL 门禁（`interrupt` / `Command`）。
- `src/sortie_deck/models.py` — 全部领域模型、枚举与请求体（`Initiative`、`StageContext`、`StageResult`、`HitlRequest`、`PipelineStageSpec` …）。数据契约的源头。
- `src/sortie_deck/pipeline.py` — 航线模板、`plan_pipeline()` 规划官提案、`normalize_specs()` 归一化（强制 intake … done 三明治）。
- `src/sortie_deck/settings.py` — 全部配置项、默认值与存储路径（env 前缀 `TDT_`）。
- `src/sortie_deck/api/app.py` — 单文件 FastAPI 应用，承载全部 REST + SSE 路由与鉴权。
- `src/sortie_deck/registry.py` + `src/sortie_deck/plugins/` — 执行器注册表与内置执行器实现。
- `src/sortie_deck/store.py` + `src/sortie_deck/repositories/` — Initiative 持久化（JSON 默认 / SQLite）。

**重要**
- `service.py`（身份/权限/任务服务，API·CLI·MCP 共用）、`api_keys.py`（用户级 API Key）、`mcp_server.py`（`/mcp` Streamable HTTP 服务端）、`cli_mission.py`（`sortie mission` 子命令）。
- `run_queue.py`（SQLite 租约式运行队列，多 worker）、`event_log.py`（mission 事件表，SSE 的唯一数据源，跨进程续读）、`parallel.py`（并行波次/依赖边纯函数）、`stage_consult.py`（阶段上下游咨询）、`pipeline_llm.py`（LLM 规划 + 规则回退）。
- `mission_input.py`（任务输入快照：原件落盘 + 文本抽取 + `input/manifest.json` 索引 + `input/brief.md`）、`layout.py`（`missions/<id>/{artifacts,input,logs,workspace,chat,memory}` 布局与旧布局迁移）、`maintenance.py`（孤儿 intake 目录回收）。
- `auth.py`（PBKDF2 密码 + HMAC 令牌）、`artifacts.py` / `artifacts_s3.py`、`memory.py`、`runtime.py`（doctor/日志/目录）、`org_policy.py`（组织策略）、`contracts.py`（阶段产物契约校验）。
- `harness/__init__.py`（通用 agent 循环，独立于阶段执行器）、`cli.py`（`sortie` 子命令）。

**支撑**：`templates.py`、`rooms.py`、`llm.py`、`agents.py`、`toolkit.py`、`knowledge*.py` / `weknora.py`、`mission_briefing.py`、`mcp_runtime.py`、`bridges.py`、`webhooks.py`、`avatars.py` / `media.py`、`workspace.py`。

**前端** `apps/web/src/`：`App.tsx`（页面装配）、`api.ts`（后端客户端）、`types.ts`、`i18n.ts`（中英文案）、`themes.ts`、`storage.ts`、`components/*.tsx`。

**资源与模板**
- `packages/templates/` — 航线 YAML（`default_pipeline.yaml` / `express_pipeline.yaml` / `standard_pipeline.yaml`）与 `personas/*.md` 角色人设。
- `packages/examples/` — 可导入的 MCP / Skill 样例；`packages/policies/` — 组织策略样例。
- `docs/en/` `docs/zh/` — 双语 MkDocs 文档（`design/`、`architecture/`、`modules/`），是理解模块细节的第一手资料。

## 核心概念

- **Initiative（任务/出击）**：一次迭代的载体，包含 `status`（draft/running/waiting_hitl/stopped/failed/done）、`current_stage`、`thread_id`、`roles`、`timeline`、`pending_hitl`、`meta`。
- **航线（Track / Pipeline）**：三条内置轨道 —— `express`（热修：Intake→build→QA→ship）、`standard`（完整：PRD→cases→build→self-test→integrate→handoff→QA→ship）、`default`（办公迭代：PRD→build→QA→ship）。**创建后必须确认**（UI 或 `/confirm`）才能 `/start`，未确认会抛 `PipelineNotConfirmedError`（API code `pipeline_not_confirmed`）。
- **HITL**：图内 `interrupt(payload)` 暂停，人工通过 `Command(resume=..., goto=...)` 恢复。动作：`approve` / `reject` / `edit_instruction` / `stop` / `reroute`。编码执行器的权限阻断会转成 `needs_hitl`。
- **执行器（Executor）**：实现 `StageExecutor` Protocol（`name` + `async run(ctx) -> StageResult`）。按阶段/角色解析：`intake→mock_intake`、`product_prd→mock_product`、编码阶段按 `coding_executor` 选 `mock_eng` / `claude_code` / `cursor_cli`。CLI 缺失时执行器 **soft-fail** 并给出可操作提示（不静默回退 mock）。
- **产物（Artifacts）**：写入 `data/missions/<initiative_id>/artifacts/<stage>/`，由 `contracts.py` 按 `output_contract` 校验必需文件。
- **输入快照（Input）**：用户导入的资料落 `data/missions/<id>/input/`（原件 + 抽取文本 + manifest），pinned 长文本只给路径指针，执行期通过 `StageContext.inputs` 注入 `## Input index` 由 agent 按需读取。
- **房间（Squad Rooms）**：每任务一个 `.jsonl` 消息流；支持 `@product`/`@eng`/… 与斜杠命令 `/confirm` `/start` `/approve` `/reject` `/rewrite` `/stop`。

## 代码约定

- 风格：`ruff`，`line-length = 100`，规则集 `E, F, I, UP, B, SIM`。忽略 `B008`（FastAPI `Depends`/`Header` 默认值）、`E501`、`UP042`（有意保留 `str` Enum）。`orchestrator.py` 额外忽略 `BLE001/S110/SIM105`，`cli.py` 忽略 `BLE001`。
- 类型：全量类型标注；文件首部 `from __future__ import annotations`；联合类型用 `X | None`；协议用 `typing.Protocol`（部分 `@runtime_checkable`）；领域模型用 Pydantic `BaseModel`，内部结构用 `@dataclass`。
- 命名：模块/函数 `snake_case`，类 `PascalCase`，内部辅助加 `_` 前缀；ID 一律用 `new_id(prefix)` 生成（`ini_`/`thr_`/`squad_`/`evt_`/`msg_`/`mem_`/`usr_`/`job_` …）。
- 异步：以 `asyncio` 为主；并发用 `asyncio.gather`（波次）、`asyncio.Semaphore`（`ConcurrencyQueue` / `ENG_QUEUE`）、`asyncio.create_task`（inline 调度）、`asyncio.Queue`（SSE 扇出）。持久化多为同步 `sqlite3` / 文件 IO（`check_same_thread=False` + `PRAGMA journal_mode=WAL`）。
- 错误处理：内部用轻量异常（`RuntimeError`/`ValueError`/`PermissionError`）+ 自定义（`PipelineNotConfirmedError`、`ContractError`、`ArtifactPathError`、`WeKnoraError`）。**在外部边界处有意「降级兜底」**（`except Exception` + `# noqa: BLE001`），例如 LLM 失败回退 mock —— 这些是有意为之，不要盲目删除。
- 后端可切换：storage（json/sqlite）、artifact（local/s3）、run（inline/sqlite）、kb（local/weknora）、coding executor、LLM。改动时注意两条实现路径都要覆盖（如 `repositories/factory.py`、`artifacts_s3.py::build_artifact_store`）。
- 国际化：面向 UI 的文案中英混排，`PipelineStageSpec` 同时含 `label`（中文）与 `label_en`；前端文案集中在 `apps/web/src/i18n.ts`。
- 历史命名：项目由 **Rally** 更名而来。`tdt` / `tdt-api` CLI 别名、`rally_*` 存储键、`rally.executors` 入口组仍被兼容接受，不要当作死代码删除。

## 配置（env 前缀 `TDT_`）

全部见 `sortie_deck.settings.Settings`（加载 `.env`）。高频项：

| 变量 | 默认 | 说明 |
|------|------|------|
| `TDT_HOST` / `TDT_PORT` | `127.0.0.1` / `8787` | API 绑定 |
| `TDT_CORS_ORIGINS` | 含 5173 | 工作台来源，逗号分隔 |
| `TDT_ENV` | `development` | 设 `production` 时拒绝默认 token secret |
| `TDT_TOKEN_SECRET` | `tdt-dev-secret` | HMAC 令牌密钥 |
| `TDT_STORAGE` | `sqlite` | **仅 sqlite**；JSON 后端已移除，旧 `initiatives.json` 首次启动自动导入并备份 |
| `TDT_RUN_BACKEND` | `inline` | `inline` / `sqlite` 多 worker 队列 |
| `TDT_RUN_WORKER` | `true` | 本进程是否消费队列；API 多副本时置 false，另起 `sortie worker` |
| `TDT_WORKER_ID` | 空（= `{hostname}-{pid}`） | worker 身份，多副本必须唯一 |
| `TDT_EVENT_RETENTION_DAYS` | `7` | mission 事件表保留天数（SSE 补播窗口） |
| `TDT_PUBLIC_BASE_URL` | 空 | 生成 MCP/CLI 配置片段用的对外地址 |
| `TDT_WEB_DIST` | 空（= `apps/web/dist`） | 前端产物目录，容器里必须显式指定 |
| `TDT_MCP_ALLOWED_HOSTS` | 空 | `/mcp` 的 Host 白名单；留空关闭 DNS-rebinding 防护 |
| `TDT_API_KEY_RATE_LIMIT_PER_MINUTE` | `240` | 每个 API Key 的进程内软限流 |
| `TDT_LLM_CLI_TIMEOUT` | `60` | `claude` CLI 回退超时（挂在阶段关键路径上） |
| `TDT_ARTIFACT_BACKEND` | `local` | `local` / `s3`（需 `pip install '.[s3]'`） |
| `TDT_DEPLOY_EXECUTOR` | `mock` | `mock` / `deploy_shell`（配 `TDT_DEPLOY_CMD`） |
| `TDT_DEFAULT_CODING_EXECUTOR` | `auto` | 编码阶段执行器 |
| `TDT_ENG_CONCURRENCY` | `3` | 开发阶段并发 |
| `TDT_PIPELINE_LLM` / `TDT_ROOM_LLM` | `true` | LLM 规划 / 房间讨论（需 API key） |
| `TDT_LLM_API_KEY` / `OPENAI_API_KEY` | — | OpenAI 兼容 key |
| `TDT_MCP_RUNTIME` | `false` | 把 MCP 工具接入编码执行器提示 |
| `TDT_OTEL_ENABLED` | `false` | 全链路追踪总开关（需装 `[otel]` extra） |
| `TDT_OTEL_ENDPOINT` | — | OTLP/HTTP 地址，如 `http://127.0.0.1:4318`（自动补 `/v1/traces`） |

开发种子用户：`admin`/`admin123`（admin）、`operator`/`operator123`（member）。

## 开发注意事项

- 改模型/契约：优先改 `models.py`，再检查 `contracts.py`、`graph.py`、`pipeline.py`、`api/app.py` 与前端 `apps/web/src/types.ts` 是否同步。
- 改航线模板或阶段：同步检查 `pipeline.py` 的 `normalize_specs()` 三明治约束、`packages/templates/*.yaml`、`docs/*/modules/pipeline.md`，并补/改 `tests/test_pipeline*.py`。
- 改执行器：在 `plugins/` 实现并在 `plugins/factory.py::build_default_registry` 注册；社区插件走入口组 `sortie_deck.executors`（`plugins/entrypoints.py`）。
- 改 API 路由：全部集中在 `api/app.py`，遵循既有 `/api` 前缀与 `require_user` / `require_admin` 依赖。
- 前端：API 调用统一走 `apps/web/src/api.ts`；类型放 `types.ts`；提交前跑 `npm run typecheck`。
- 测试：`pytest` 使用 `asyncio_mode = "auto"`（`pyproject.toml`），异步测试无需手动加 `@pytest.mark.asyncio`；测试集中在 `tests/`，按模块一一对应。
- 文档：本仓库文档是产物的一部分，行为变更请同步更新 `docs/en/` 与 `docs/zh/`（含 `CHANGELOG.md`）。

## 参考文档（比本文件更细）

- `README.md`（英） / `README.zh-CN.md`（中）— 功能与配置速览
- `DESIGN.md` → `docs/*/design/` — 设计目标、管线与图、人在环、威胁模型、ADR
- `ARCHITECTURE.md` → `docs/*/architecture/` — 架构总览与数据流
- `docs/*/modules/*.md` — 按模块的详细规格（orchestrator / graph / pipeline / executors / auth / cli / knowledge / memory / artifacts / web 等）
- `TODO.md` — 后续能力 backlog（真人频道绑定、知识目录/标签、记忆分层、工具技能拆分、自迭代、Jev 决策层）
