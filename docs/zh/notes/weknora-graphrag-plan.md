# WeKnora 混合检索 + GraphRAG 落地方案

> 时间：2026-10-19。目标：让本项目（Sortie Deck）配置的 WeKnora 在本机正常运行，检索走混合检索（hybrid-search），并通过 knowledge-chat 链路真正吃到 GraphRAG（知识图谱增强）。
>
> **状态：已落地（2026-10-19）**。部署位置 `/Library/git-root/weknora`（本机 OrbStack + docker compose `--profile neo4j`）；知识库 `sortie-knowledge`（vector+keyword+graph 全开）；项目 `.env` 已切换 `TDT_KB_BACKEND=weknora` + `TDT_WEKNORA_RETRIEVAL_MODE=graph`；单测 43 通过；API 级端到端验证通过（`/api/knowledge/status` 返回 `graph_enabled=true`，`/api/knowledge/retrieve` graph 模式返回命中）。

## 0. 实际落地结果与踩坑记录

| 项 | 结果 |
|---|---|
| WeKnora 版本 | main（2026-10，v0.8.3+），docker compose，6 容器（app/docreader/paradedb/redis/neo4j/frontend） |
| Neo4j 端口冲突 | 本机已有 neo4j 容器占 7474/7687，用 `docker-compose.override.yml` 映射到宿主机 17474/17687（容器内通信不受影响） |
| 管理员 | `admin@sortie.local / SortieWk2026!`（`WEKNORA_BOOTSTRAP_SYSTEM_ADMIN_EMAIL` 自举提权，需要 `--force-recreate` 生效） |
| 模型 | chat=`z-ai/glm-5.2`（走 newapi 网关）、embedding=`bge-m3`（本机 Ollama，1024 维） |
| 知识库 | `e9f3d1b9-1a29-42d3-a0b8-ac6428ba80b1`，图谱抽取 tags=`[模块,接口,服务,依赖,属于,实现,使用,产出]` + few-shot |
| API Key | 项目 `.env` 的 `TDT_WEKNORA_API_KEY`（scope=tenant，full_access） |
| 文档 | 15 篇（本地 catalog 全量），Neo4j 193 实体 |
| 检索延迟 | hybrid ~0.3s；graph ~10-40s（拿到 references 即断开 SSE） |

踩坑（重要）：

1. **网关模型都关不掉思考**：`chat_template_kwargs.enable_thinking=false` 网关忽略，deepseek-v4-flash 思考量随 prompt 增长，超过 WeKnora 硬编码的 8192 max_tokens 后正文为空 → 图谱抽取全量失败。解法：换 `z-ai/glm-5.2`（思考量适中）；仍有少量长 chunk 抽取失败（asynq 重试 3 次后放弃，不阻塞其他 chunk）。
2. **OrbStack 容器访问内网 10.x**：TCP 可建连但读响应超时。解法：宿主机 LaunchAgent 跑 asyncio TCP 转发（`~/.local/bin/tcp_forward_newapi.py`，13000 → 10.216.2.135:3000），模型 base_url 指向 `http://host.docker.internal:13000/v1`（需加 SSRF 白名单 `SSRF_WHITELIST_EXTRA=newapi.zbjt.com,host.docker.internal`）。
3. **系统代理劫持 127.0.0.1**：VPN 工具设置 macOS 系统代理后，Python httpx/urllib（读系统代理）访问本机 WeKnora 全部 502，curl 正常。解法：`WeKnoraClient` 创建 httpx.Client 时 `trust_env=False`。
4. **SSRF 校验**：WeKnora 对模型 base_url 做解析校验，内网域名/IP 一律拒绝，必须显式加白。
5. **拉镜像**：Docker Hub 直连被墙，镜像加速源极慢；开 VPN 后直连秒下。OrbStack daemon 配置在 `~/.orbstack/config/docker.json`（`registry-mirrors`），拉完记得清掉。

## 1. 前置结论（调研核实，影响方案）

1. **现有网关没有 embedding 模型**：`GET /v1/models` 只有 4 个 chat 模型（`MiniMax-M3`、`z-ai/glm-5.2`、`deepseek/deepseek-v4-flash-20260731`、`deepseek/deepseek-v4-pro`）。WeKnora 建索引必须有 embedding，默认方案是**本机安装 Ollama + 拉取 bge-m3**（约 1.2GB）；chat 与图谱抽取走现有网关。
2. **GraphRAG 不在 `hybrid-search` 接口里**：图谱增强只挂在 `knowledge-chat` 会话管线（`PluginExtractEntity` → QUERY_UNDERSTAND、`PluginSearchEntity` → ENTITY_SEARCH）和 Agent 工具 `query_knowledge_graph` 上。项目侧要真正吃到 GraphRAG，必须改造调用方式。
3. **GraphRAG 需要两级开关同时满足**：
   - 全局：`NEO4J_ENABLE=true` + `NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD`（Neo4j 必须带 APOC 插件，compose 内置 `neo4j` 服务已预装）；
   - 知识库级：`IndexingStrategy.GraphEnabled=true` 且 `ExtractConfig.Enabled=true`（tags / nodes / relations / custom_instructions）。
   - 文档需**重新入库**才会触发实体关系抽取（每个 chunk 一次 LLM 调用，独立 `QueueGraph` 队列，成本/耗时明显上升）。
4. **资源约束**：本机 16GB 内存、约 20GB 剩余磁盘、Docker daemon 当时未启动。全量镜像（app / docreader / paradedb / neo4j / redis / frontend）+ bge-m3 约 10–12GB，可行但偏紧。

## 2. 部署步骤（阶段一：本机 WeKnora）

```bash
# 1) 启动 Docker Desktop，确认 daemon
docker info

# 2) 拉取源码（compose / .env 模板在仓库内）
git clone https://github.com/Tencent/WeKnora.git /Library/git-root/weknora
cd /Library/git-root/weknora

# 3) 配置
cp .env.example .env
# 修改：
#   APP_PORT=8080
#   FRONTEND_PORT=8088          # 避免占用 80
#   NEO4J_ENABLE=true           # GraphRAG 全局开关
#   NEO4J_PASSWORD=<本地密码>
#   TZ=Asia/Shanghai

# 4) 启动（neo4j 是 profile，必须带上）
docker compose --profile neo4j up -d

# 5) 自检
curl -s http://127.0.0.1:8080/api/v1/system   # 期望返回 graph engine = "Neo4j"
# UI: http://localhost:8088 注册管理员
```

## 3. 模型与知识库配置（阶段二）

1. 安装 Ollama：`ollama pull bge-m3`；WeKnora `.env` 中 `OLLAMA_BASE_URL=http://host.docker.internal:11434`（默认即是）。
2. WeKnora「设置 → 模型」新建：
   - **Chat**：OpenAI 兼容，`http://newapi.zbjt.com:3000/v1`，key 复用现有，模型 `deepseek/deepseek-v4-flash-20260731`
   - **Embedding**：source=local，name=`bge-m3`
   - **Rerank**：暂不配（可选，后续加云端 rerank）
3. 新建知识库：
   - 分块：`auto` / chunk 512 / overlap 80（默认即可）
   - 打开 **GraphEnabled + ExtractConfig**：配置 tags（如 `依赖/属于/实现/调用`），附少量 nodes / relations few-shot 示例
4. 重新导入文档：用项目 `POST /api/knowledge/import` 灌入现有 catalog 文档与任务产物；确认每篇 `parse_status=completed` 且图谱抽取完成（看 WeKnora 任务队列 / 日志）。

> 注意：换 embedding 模型必须重建索引（含重跑图谱抽取）。

## 4. 项目配置与代码改造（阶段三）

### 4.1 配置（`.env` / `.env.example`）

```bash
TDT_KB_BACKEND=weknora
TDT_WEKNORA_BASE_URL=http://127.0.0.1:8080
TDT_WEKNORA_API_KEY=<WeKnora 创建的 API Key>
TDT_WEKNORA_KB_ID=<知识库 ID>
TDT_WEKNORA_RETRIEVAL_MODE=graph   # hybrid | graph
```

### 4.2 代码改造点

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/weknora.py` | 新增 `create_session(title)`（`POST /api/v1/sessions`）；新增 `knowledge_chat(session_id, query, kb_ids, knowledge_ids)` —— `POST /api/v1/knowledge-chat/:session_id` SSE 解析，只取 `references` 事件转 `WeKnoraHit`，忽略生成正文；失败抛 `WeKnoraError` 交由 gateway 降级 |
| `src/sortie_deck/knowledge_gateway.py` | `retrieve()` 增加模式分支：`hybrid` 沿用 `hybrid_search`；`graph` 先查任务命名空间（该任务文档 `remote_id` 作为 `knowledge_ids` 传入）→ 再查全局池 → 合并去重，命名空间命中保持 +10 加权；任一阶段失败回退 `hybrid_search`。复用 / 按需创建 session，避免每轮检索新建会话 |
| `src/sortie_deck/settings.py` | 新增 `weknora_retrieval_mode: str = "hybrid"`、`weknora_chat_timeout: float = 60.0` |
| `src/sortie_deck/api/app.py` | `/api/knowledge/status` 增加 `retrieval_mode`、`graph_enabled`（探测 WeKnora `/system` 是否返回 Neo4j） |
| `tests/test_weknora_gateway.py` | SSE references 解析、graph 模式降级、mission 命名空间优先、`knowledge_ids` 透传 |

现有调用点无需改动（自动受益）：`plugins/coding.py:499`（Cursor pack）、`harness_providers/executor.py:210`（harness RAG 简报）、`orchestrator.py:457`（产物回写）。

## 5. 端到端验证（阶段四）

1. `GET /api/knowledge/status` → `backend=weknora`、`graph_enabled=true`
2. `POST /api/knowledge/retrieve` 分别用 hybrid / graph 模式跑同一 query，对比命中 chunk 与延迟
3. 构造多跳问题（如"某接口的实现依赖哪个模块"），确认 graph 模式能召回实体关联 chunk
4. `pytest tests/test_weknora_gateway.py tests/test_knowledge*.py`

## 6. 文档沉淀（阶段五）

- 本文档：部署步骤、模型配置、GraphRAG 双开关、验证清单、故障排查
- 更新 `docs/zh/modules/knowledge.md`（检索模式、GraphRAG 边界）与 `mkdocs.yml` Notes 导航
- 成本提示：graph 模式每次检索会触发一次 LLM 生成（只为拿 references），建议固定低成本模型并限制 `limit<=6`

## 7. 主要风险与备选

| 风险 | 应对 |
|---|---|
| 网关无 embedding | 默认 Ollama bge-m3；备选：提供云端 embedding（SiliconFlow / 智谱等），改 `EMBEDDING_*` 并重建索引 |
| 磁盘仅剩约 20GB | 先清理 Docker 缓存；不启 minio / langfuse 等可选 profile |
| graph 模式延迟 3–10s + LLM 成本 | 复用 session、低成本模型、limit<=6；必要时加检索结果缓存 |
| embedding 维度变更 | 换模型必须重建知识库索引（GraphRAG 重跑抽取） |
| WeKnora API 版本变动 | 客户端封装集中在 `weknora.py`，改动面可控 |

## 8. 参考

- WeKnora 官方文档：知识图谱（两级开关 / Neo4j+APOC / 抽取管线）<https://weknora.weixin.qq.com/docs/03-features/09-knowledge-graph>
- WeKnora API：`POST /api/v1/sessions`、`POST /api/v1/knowledge-chat/:session_id`（SSE，事件含 references），鉴权 `X-API-Key`
- 自检接口：`GET /api/v1/system` 返回 `Neo4j` / `Not Enabled`
- 行业调研背景：见 `docs/zh/notes/rag-industry-survey.md`
