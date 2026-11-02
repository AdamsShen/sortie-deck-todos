# 云端部署方案：Sortie Deck 前后端 + WeKnora（全新部署，零数据迁移）

- 状态：待确认（未经确认不执行部署动作）
- 日期：2026-11-02（修订：去除全部数据迁移步骤）
- 目标形态：单台云 Linux，docker compose 两套编排，外网仅 80/443，供内部多人使用
- 数据策略：**不迁移任何历史数据**，WeKnora 与 Sortie Deck 均从空库开始

## 1. 目标与约束

| 项 | 取值（假设） |
|---|---|
| 服务器 | 单台云主机，8C16G 起，磁盘 ≥100GB，Ubuntu 22.04 / Debian 12 |
| 编排 | Docker Compose（不引入 K8s） |
| 租户模型 | 单租户、内部使用 |
| 对外入口 | 仅 80/443（Caddy 自动 HTTPS），SSH 22 限来源 IP |
| 知识库 | 新建 3 个空库，后续按需上传文档 |
| harness | 本期按 A 方案（agent 二进制进 worker 镜像），先 Claude Code |

## 2. 不迁数据带来的变化

好消息：`SYSTEM_AES_KEY`、`JWT_SECRET`、`DB_PASSWORD`、`REDIS_PASSWORD` 全部可以重新生成，不存在"解密旧凭据"的依赖；也不需要 `pg_dump`/卷拷贝/Neo4j 迁移。

代价：新实例上有 **3 类对象必须重新创建**，且其中两类的标识要回填到 Sortie 的 `.env`。

| 对象 | 本机现状 | 云上要做的事 | 不回填的后果 |
|---|---|---|---|
| 模型配置（`models` 表） | 4 类模型，其中 3 个指向**本机服务**：embedding → Ollama `127.0.0.1:11434`（bge-m3，1024 维）、rerank → 本地 FastAPI `:8012`、KnowledgeQA → 本地 `:13000`（glm-5.2）；chat → `newapi.zbjt.com:3000` | 重新选型并配置（见 4.2） | 文档向量化/检索/问答全 500 |
| 3 个知识库 | `sortie-knowledge` / `sortie-toolkit` / `sortie-memory` | 新建空库，拿到新 id（见 4.3） | 静默降级到本地实现：主库空 id → 不走向量检索；toolkit 空 id → 本地词元打分；memory 空 id → 回落主库 |
| API Key（`X-API-Key`） | 已有 key | 新实例重新生成（见 4.3） | 所有 WeKnora 调用 401 |

关键代码依据：
- 知识库只有在 `backend=weknora` **且** `kb_id` 非空时才走远端（`src/sortie_deck/knowledge_gateway.py:75-76`），否则用本地 JSON 目录兜底。
- toolkit / memory 的库 id 留空的降级行为见 `.env` 注释：toolkit 始终降级 local；memory 会回落主库 id（会污染主库，务必显式指定）。

## 3. 目标拓扑

```
公网 80/443
   │
 Caddy（单容器，自动证书）
   ├── sortie.example.com  →  sortie-api:8787      （后端 + 已构建前端 dist）
   └── kb.example.com      →  WeKnora-frontend:80  （知识库管理 UI，nginx 反代 /api 到 app）

docker network: sortie-net（external，两套 compose 共用，仅容器内互通）
   ├── sortie-api / sortie-worker
   ├── WeKnora-app / postgres / redis / neo4j / docreader
   └── （可选）ollama / rerank 服务
```

要点：
1. Sortie Deck 只需一个域名：后端自身吐前端产物（镜像 `TDT_WEB_DIST=/app/apps/web/dist`，`Dockerfile:24`）。
2. WeKnora 的 8080 / 5432 / 6379 / 7474 / 7687 一律不映射公网，Sortie 通过 `http://WeKnora-app:8080` 调用。
3. 任务存储只支持 sqlite，`/data` 卷必须本地盘，不能放 NFS（`src/sortie_deck/runtime.py:81-88` 启动期强校验）。

### 3.1 镜像 / 容器 / 编排的关系（易混淆点）

"两套 compose"= 两个**编排工程**（两个目录、两份 `.env`、两份 `docker-compose.yml`），不是两个镜像。实际数量：

| 归属 | 镜像 | 容器数 | 说明 |
|---|---|---|---|
| sortie-deck 工程 | `sortie-deck:local`（1 个） | 2（api + worker） | 前后端**在同一个镜像里**：前端是构建阶段产出的静态文件，由后端进程直接吐；**没有独立的前端容器，也没有独立端口**。api 与 worker 共用该镜像，只是启动命令不同 |
| weknora 工程 | app / frontend / docreader / postgres / redis / neo4j（6 个官方镜像） | 6 | 官方 compose 默认服务；sandbox、minio、qdrant 等是 profile，不启用 |
| Caddy | `caddy:2` | 1 | 80/443 入口 |
| Ollama（云上自建 embedding） | `ollama/ollama` | 1 | 承载 bge-m3 |
| rerank（云上自建） | 自建小镜像或宿主机进程 | 1 | `local-rerank-service` |

合计约 **10 个容器 / 9–10 个镜像**。两个 compose 工程只共享一个 `external` 网络，镜像互不复用。

worker 不是独立镜像：它与 api 用同一个 `sortie-deck:local`，只是启动命令不同（`worker` vs `api`，见 `docker/entrypoint.sh:5-13`）。

**为什么 api 也用带 harness 的镜像**（本地实测结论）：`GET /api/harnesses` 在 **api 进程内**执行 probe，用的是 api 容器的 PATH。如果 api 用精简的 `runtime` target，它看不到 `claude` / `codex`，页面会把所有 harness 标成「不可用」，而 worker 侧实际是可用的。要么两边同镜像，要么把 probe 改成跨进程/声明式（未做）。

**共享 `external` 网络解决什么**：只解决跨工程容器的**名字解析**，不解决镜像复用。

- 共享后：sortie 工程里直接写 `base_url: http://WeKnora-app:8080`，容器间直连，不经宿主机。
- 不共享：只能写 `http://<宿主机IP>:8080`（容器里的 `127.0.0.1` 指向容器自身，不是宿主机，这是最常见的一类配置错误），且必须把 WeKnora 的 8080 映射到宿主机，多一跳、多暴露一个端口。
- 镜像层面两套工程完全独立：WeKnora 用官方镜像，Sortie 用自己构建的镜像，互不复用。

落到操作上就是：`docker network create sortie-net`，两套 compose 都声明 `networks: {sortie-net: {external: true}}`，各自服务挂上去。

## 4. 部署步骤

### 4.1 阶段 0：服务器基线

```bash
timedatectl set-timezone Asia/Shanghai
apt-get update && apt-get install -y docker.io docker-compose-plugin git curl ufw
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
mkdir -p /srv/weknora /srv/sortie
```

云厂商安全组同样只开 22/80/443。

### 4.2 阶段 1：起 WeKnora + 配模型

**起服务**（新库、全新密钥）：

```ini
# /srv/weknora/.env
WEKNORA_VERSION=<钉住版本，勿用 latest>
APP_PORT=127.0.0.1:8080
FRONTEND_PORT=127.0.0.1:8088
DB_PASSWORD=<强随机>
REDIS_PASSWORD=<强随机>
JWT_SECRET=<强随机>
SYSTEM_AES_KEY=<强随机，32 字节>
NEO4J_PASSWORD=<强随机>
RETRIEVE_DRIVER=postgres
NEO4J_ENABLE=true
STORAGE_TYPE=local
WEKNORA_BOOTSTRAP_SYSTEM_ADMIN_EMAIL=<管理员邮箱>
```

```bash
cd /srv/weknora && docker compose down -v && docker compose up -d
docker compose ps          # 等 postgres/app healthy
curl -s 127.0.0.1:8080/health
```

**配模型**（必做，否则建库时选不到 embedding）：

推荐用声明式 `config/builtin_models.yaml`，重启即生效、可复现，不用点 UI：

```yaml
builtin_models:
  - id: builtin-embedding-bge-m3
    type: Embedding
    source: remote
    is_default: true
    name: ${EMBEDDING_MODEL_NAME}
    parameters:
      base_url: ${EMBEDDING_BASE_URL}
      api_key: ${EMBEDDING_API_KEY}
      provider: generic
      embedding_parameters:
        dimension: 1024          # bge-m3 = 1024，非字符串字段必须写死

  - id: builtin-rerank-bge
    type: Rerank
    source: remote
    parameters:
      base_url: ${RERANK_BASE_URL}
      api_key: ${RERANK_API_KEY}
      provider: generic

  - id: builtin-qa-chat
    type: KnowledgeQA
    source: remote
    is_default: true
    name: ${LLM_MODEL_NAME}
    parameters:
      base_url: ${LLM_BASE_URL}
      api_key: ${LLM_API_KEY}
      provider: generic
```

compose 里挂载该文件（示例 `- ./config/builtin_models.yaml:/app/config/builtin_models.yaml:ro`），并把 `${...}` 变量写进 `.env`。

模型来源三选一（**需先确认，见第 6 节**）：

| 方案 | chat / KnowledgeQA | embedding | rerank | 适用 |
|---|---|---|---|---|
| A 全公网 API | 公网 OpenAI 兼容端点 | 同左（需支持 `/v1/embeddings`） | 同左（需支持 rerank，很多网关不支持） | 有公网模型账号、不想运维 |
| B 云上自建 | 公有 API 或 vLLM | Ollama 容器（bge-m3） | 宿主机/容器跑 `local-rerank-service` | 内网、可控成本（推荐） |
| C 混合 | 公有 API | 云上自建 | 云上自建 | 命中率与成本折中 |

若走 B/C，`rerank_server.py` 现成代码在 `/Library/git-root/weknora/local-rerank-service/rerank_server.py`，直接搬上云跑宿主机；WeKnora 的 compose 已给 app 配了 `host.docker.internal:host-gateway`（`docker-compose.yml:404`），Linux 上可用 `host.docker.internal:8012` 访问宿主服务。

**rerank 的三个层次别混**（embedding 同理）：

| 层次 | 是什么 | 谁提供 | 体积/位置 |
|---|---|---|---|
| 镜像 | 运行时 + 依赖库（python、FlagEmbedding、torch） | 自己构建或官方推理镜像 | 2–5 GB，构建期固定 |
| 模型权重 | `BAAI/bge-reranker-v2-m3` 参数文件 | **首次启动时从 HF 下载**（脚本已设 `HF_ENDPOINT=https://hf-mirror.com`） | 2.27 GB，挂卷缓存 |
| 服务进程 | 暴露 `POST /rerank` 的 HTTP 服务 | 必须在跑 | 常驻内存 1–2.5 GB |

结论：**权重确实可以运行时下载，且推荐这么做**（不烘进镜像，镜像小、可复用）。但"下载权重"不等于"有服务"——Ollama 只能提供 chat / embedding，不提供 `/rerank`，所以 rerank 必须有一个独立服务进程。

两种落地方式：

1. **容器化现有脚本（推荐，零改造）**：已在 `/Library/git-root/weknora/local-rerank-service/Dockerfile` 落地，`rerank_server.py` 一行未改。`HF_HOME=/hf` 挂卷缓存权重，首次启动下载 2.27 GB。接口是 Cohere 协议（`{query, documents}` → `{results:[{index, relevance_score}]}`），正是 WeKnora `cohererank` client 期望的形状。
2. **换成通用推理镜像（TEI / Infinity）**：镜像和权重都现成，但它们的 `/rerank` 返回裸数组 `[{index, score}]`、请求字段是 `texts` 而非 `documents`，与 Cohere 协议不一致，**直接替换会解析失败**，需要中间适配层。除非要上 GPU 追求吞吐，否则不值得。

纯 CPU 的注意点：

- **半精度不用改代码**：`rerank_server.py:61` 虽然写的是 `use_fp16=True`，但 FlagEmbedding 1.4.2 内部有 `if device == "cpu": self.use_fp16 = False`（`inference/reranker/encoder_only/base.py:114`），CPU 上会自动退化为 fp32。镜像里锁 `FlagEmbedding>=1.4,<2`，避免该内部行为随大版本漂移。
- **并发是串行的**：服务用锁串行推理（`rerank_server.py:120`），并发任务会排队；单次 20 条文档在 4C 上约 1–2s。这与 `TDT_HARNESS_RAG_TIMEOUT_SECONDS=15` 的预算要一起看，任务并发高时要么加副本（每副本各自加载一份 2.2GB 模型）、要么调大超时。

镜像构建（在云上执行，或本地交叉构建）：

```bash
cd /srv/rerank   # 放 rerank_server.py + Dockerfile
docker build -t sortie-rerank:1.0 .
# 云服务器为 x86 时，本机（arm64 Mac）构建需指定平台：
# docker buildx build --platform linux/amd64 -t sortie-rerank:1.0 --load .
```

compose 侧（与 WeKnora 同网络，模型权重挂卷持久化）：

```yaml
services:
  rerank:
    image: sortie-rerank:1.0
    container_name: sortie-rerank
    restart: unless-stopped
    environment:
      PORT: "8012"
      HF_ENDPOINT: https://hf-mirror.com
    volumes:
      - rerank-hf:/hf
    networks: [sortie-net]
volumes:
  rerank-hf:
```

WeKnora 侧模型配置填 `base_url: http://sortie-rerank:8012`、`provider: generic`（走 Cohere wire 协议）。首次启动后等 healthcheck 变 healthy（要等 2.27GB 下完）再建库/检索。

### 4.3 阶段 2：建 3 个空库 + 生成 API Key

先在 `kb.example.com` 注册管理员并登录（拿到 Bearer token），然后：

```bash
BASE=http://127.0.0.1:8080
TOKEN=<登录后的 Bearer token>

# 3 个库逐个创建；embedding_model_id 用 4.2 里默认 Embedding 模型的 id
for name in sortie-knowledge sortie-toolkit sortie-memory; do
  curl -s -X POST $BASE/api/v1/knowledge-bases \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"name\":\"$name\",\"type\":\"document\",\"indexing_strategy\":{\"vector\":true,\"keyword\":true,\"wiki\":false,\"graph\":false}}"
done

# 空间级 API Key（响应里带明文 api_key）
curl -s -X POST $BASE/api/v1/tenants/1/api-keys \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"sortie-deck","full_access":true}'
```

记录 3 个 `id` 和 1 个 `api_key`，填进 4.4 的 `.env`。

`indexing_strategy` 保持一致：向量 + 关键词开，wiki/图谱关（与现有用法对齐：toolkit/memory 是短文本条目，不需要图谱）。

### 4.4 阶段 3：构建并部署 Sortie Deck

镜像：**已完成并实测**，`/Library/git-root/sortie-deck-todos/Dockerfile` 追加了 `runtime-harness` target，只给 worker 用。

```dockerfile
FROM runtime AS runtime-harness
USER root
# Node 复用 web 构建阶段的 22.x 二进制，不走 apt（Debian 12 的 node 18 偏旧）
COPY --from=web /usr/local/bin/node /usr/local/bin/node
COPY --from=web /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf .../npm-cli.js /usr/local/bin/npm && ln -sf .../npx-cli.js /usr/local/bin/npx
# claude-agent-sdk 是可选依赖，基础镜像的 `pip install -e .` 不装 → probe 会 missing。
# 该 wheel 自带原生 CLI（_bundled/claude，314MB），SDK 的 _find_cli() 优先用它，
# 所以不需要再 npm 装 @anthropic-ai/claude-code（会重复占 314MB）。
RUN pip install -e ".[harness-claude]"
RUN ln -sf /usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude /usr/local/bin/claude
# codex 只能走 npm（包内 musl 静态二进制，约 +390MB）
RUN npm i -g --registry=$NPM_REGISTRY @openai/codex && codex --version
USER sortie
```

实测结果（在构建出的镜像内跑探针）：

| 项 | 结果 |
|---|---|
| 镜像体积 | 1.5 GB（曾重复装 npm claude 时为 1.98 GB） |
| `claude --version` | 2.1.316，来自 SDK 内置二进制，PATH 软链到 `/usr/local/bin/claude` |
| `codex --version` | 0.166.0 |
| `import claude_agent_sdk` | 通过 |
| probe | `claude_code`/`codex` = `auth_required`（无凭据时的正确状态）、`opencode`/`cursor` = `missing`、`mock` = `ok` |
| 挂载 `~/.codex` 后 | `codex` 变 `ok`，证明文件型凭据路径可用 |

凭据：`ANTHROPIC_API_KEY`、`OPENAI_API_KEY` / `CODEX_API_KEY` 由 compose `env_file` 在运行时注入 worker 容器，不烘焙进镜像。

**构建时必须显式指定 target**（已同步到 `docker-compose.yml`）：Docker 默认构建最后一个 stage，而最后一个是 `runtime-harness`，所以 api 必须写 `target: runtime`，否则 api 也会变成带 node + CLI 的胖镜像。

```yaml
  api:
    build: {context: ., target: runtime}
    image: sortie-deck:local
  worker:
    build: {context: ., target: runtime-harness}
    image: sortie-deck:worker
```

注意：**只有 api 与 worker 拆开时才只需要给 worker 装**；如果单容器跑（`TDT_RUN_WORKER=true` 在 api 里），这一个容器就同时是 api 和 worker，harness 二进制必须装进 api 镜像。

**环境文件双轨**（本地 newapi / 云端 DeepSeek）：

| 文件 | 用途 | 模型来源 |
|---|---|---|
| `.env` | 本机开发 | 内网 `newapi.zbjt.com:3000`（OpenAI + Anthropic 两个端点） |
| `.env.cloud` | 云端部署 | DeepSeek 官方（`api.deepseek.com/v1` + `api.deepseek.com/anthropic`） |

两份都含真实密钥，`.gitignore` 已忽略（`.env` / `.env.cloud`）。compose 通过 `SORTIE_ENV_FILE` 选择：

```bash
docker compose up -d                                  # 本地，读 .env
SORTIE_ENV_FILE=.env.cloud docker compose up -d       # 云端，读 .env.cloud
```

`/srv/sortie/.env.cloud` 关键项：

```ini
TDT_ENV=production
TDT_HOST=0.0.0.0
TDT_PORT=8787
TDT_TOKEN_SECRET=<强随机 ≥32 字节；production 用默认值会启动失败>
TDT_PUBLIC_BASE_URL=https://sortie.example.com
TDT_CORS_ORIGINS=https://sortie.example.com
TDT_STORAGE=sqlite
TDT_RUN_BACKEND=sqlite
TDT_RUN_WORKER=true
TDT_DATA_DIR=/data
TDT_ENG_CONCURRENCY=2

# 知识库（3 个 id + key 全部是新实例的）
TDT_KB_BACKEND=weknora
TDT_WEKNORA_BASE_URL=http://WeKnora-app:8080
TDT_WEKNORA_API_KEY=<4.3 生成的 key>
TDT_WEKNORA_KB_ID=<新 sortie-knowledge id>
TDT_TOOLKIT_WEKNORA_KB_ID=<新 sortie-toolkit id>
TDT_WEKNORA_MEMORY_KB_ID=<新 sortie-memory id>
TDT_WEKNORA_RETRIEVAL_MODE=hybrid
TDT_HARNESS_RAG_TIMEOUT_SECONDS=15

# 编排层模型（航线规划 / 房间 / PRD / 记忆总结）
TDT_LLM_BASE_URL=https://api.deepseek.com/v1
TDT_LLM_API_KEY=<DeepSeek key>
TDT_LLM_MODEL=deepseek-flash

# harness（Claude Code 走 Anthropic 兼容端点）
TDT_DEFAULT_HARNESS=claude_code
TDT_HARNESS_FALLBACK=true
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_API_KEY=<同一个 DeepSeek key>
ANTHROPIC_MODEL=deepseek-flash
ANTHROPIC_SMALL_FAST_MODEL=deepseek-flash
```

compose：api 与 worker 都用 `runtime-harness`（同一个 `sortie-deck:local` 镜像），共享 `sortie-data` 卷；`networks: [sortie-net]`（`external: true`）。起服务后 `curl 127.0.0.1:8787/api/health`。

### 4.5 阶段 4：Caddy 反代 + HTTPS

```caddyfile
sortie.example.com {
    reverse_proxy sortie-api:8787 {
        flush_interval -1        # SSE 实时过程不能缓冲
    }
}
kb.example.com {
    reverse_proxy WeKnora-frontend:80
}
```

### 4.6 阶段 5：账号与安全（必做）

1. **改默认账号**：`users.json` 不存在时会自动种 `admin/admin123` 和 `operator/operator123`（`src/sortie_deck/auth.py:107-120`）。首次登录立刻改密码并删除 `operator`。
2. 自助注册只能创建 `member` + `pending`，管理员审核后才可登录（`src/sortie_deck/auth.py:52`、`src/sortie_deck/api/app.py:434-450`）。
3. 密钥只放 `.env`（`chmod 600`），不进镜像、不进 git。
4. WeKnora 侧：确认匿名注册已关闭；`JWT_SECRET` / `SYSTEM_AES_KEY` 外泄等于模型凭据外泄。
5. 备份：`/data` 与 `weknora_postgres-data` 每日 `pg_dump` + 卷快照。

### 4.7 阶段 6：验证清单

| # | 检查项 | 通过标准 |
|---|---|---|
| 1 | 后端健康 | `GET /api/health` 200 |
| 2 | 前端 | `https://sortie.example.com` 能登录 |
| 3 | 模型就绪 | WeKnora 后台「模型」页能看到 Embedding/Rerank/KnowledgeQA 三条且默认标记生效 |
| 4 | 知识库连通 | Sortie 知识库页能列出 3 个新库（不再是本地兜底） |
| 5 | 上传 + 检索 | 往 `sortie-knowledge` 传 1 篇文档 → 解析完成 → `hybrid-search` 有命中 |
| 6 | 任务闭环 | 建一个小任务跑通「规划 → 执行 → 验收」，过程有 SSE 消息 |
| 7 | harness 生效 | 阶段 meta 的 `harness` 与选择一致（不是 `harness_fallback`） |
| 8 | 多用户 | 注册 → pending → 管理员放行 → 登录成功 |
| 9 | 重启存活 | `docker compose restart` 后数据与任务仍在 |

### 4.8 本地验证记录（2026-11-02，复用已运行 WeKnora）

拓扑：`sortie-deck-todos-api-1`（8788→8787）+ `sortie-deck-todos-worker-1`，用 `/tmp/sortie-test.yml` 覆盖后接入 `weknora_WeKnora-network`，知识库指向 `http://WeKnora-app:8080`；WeKnora 六个容器保持原样未重建。

| 检查项 | 结果 |
|---|---|
| 镜像构建 | `sortie-deck:local` 1.5 GB（api+worker 共用）、`sortie-rerank:1.0` 1.74 GB |
| api 健康 | `GET /api/health` 200，`run_backend=sqlite` |
| worker 领取任务 | 日志 `worker … 已启动，队列: /data/runs.sqlite`，任务在 worker 内推进 |
| 任务闭环 | mock harness：intake → eng_implement → eng_selftest → done，timeline 11 条 |
| HITL | `POST /hitl {"action":"approve"}` 后继续执行并完成 |
| 知识库连通 | `/api/knowledge/status` → `backend=weknora`、`knowledge_base_count=3`、`base_url=http://WeKnora-app:8080/api/v1` |
| 混合检索 | `/api/knowledge/retrieve` 返回 3 条命中，top1 score 0.97，总耗时 1.3s（含 rerank） |
| claude_code harness | 直接调用 provider：拉起内置 CLI 并返回 `Not logged in`（未配 key），说明二进制与 SDK 链路通 |
| codex harness | 直接调用 provider：返回 `needs_auth`（401 Unauthorized），状态映射正确 |
| rerank 镜像 | 首次加载 363s（含 2.27GB 下载），`/health` ready=true；3 docs ≈0.63s、10 docs ≈0.93s（稳态）；打分排序正确 |
| 容器内 LLM | 日志显示 `POST http://newapi.zbjt.com:3000/v1/chat/completions 200 OK` |

验证中发现并修复的两件事：

1. worker 继承 api 的 `HEALTHCHECK`（探 8787）会被判 unhealthy → `runtime-harness` 加 `HEALTHCHECK NONE`。
2. api 用精简 target 会让 `/api/harnesses` 误报全部不可用 → api 与 worker 统一 `runtime-harness`。

遗留问题（未改代码）：

- `claude_code` 运行期遇到「Not logged in」返回 `fail` 而非 `needs_auth`（probe 阶段才判凭据），凭据过期时不会转 HITL，直接阶段失败。
- 真实模型调用未验证（本机无 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`）。

## 5. 风险

| 风险 | 影响 | 处理 |
|---|---|---|
| 3 个 KB id 没回填 / 填错 | 不报错，静默降级到本地实现，看起来"能用"但没有向量检索 | 用 `/api/knowledge` 状态接口确认走的是 weknora；上传文档后看 WeKnora 侧是否有该文档 |
| 模型没配或 base_url 错 | 建库时选不到 embedding；文档解析卡在向量化；`hybrid-search` 500 | 启动后先验 4.7-3 |
| embedding 维度写错 | 入库成功但检索报维度不匹配，且换模型后必须重建库 | bge-m3 固定 `dimension: 1024` |
| 云上访问不到 `newapi.zbjt.com:3000` | chat/KnowledgeQA 全断 | 部署前 `curl` 验证，不通换公网端点 |
| `WEKNORA_VERSION=latest` | `pull` 时静默升级，行为漂移 | 钉版本号 |
| docreader 资源 | 3.8GB 镜像，解析大文档吃内存 | 内存 ≥16G，限制单文档大小 |
| SSE 被缓冲 | 前端实时过程"卡住" | Caddy `flush_interval -1` |

## 6. 待确认事项

1. **模型怎么落**：全走公网 API（要确认该端点支持 embeddings 与 rerank），还是云上自建 Ollama(bge-m3) + rerank 服务（推荐，内网可控成本）？
2. 云服务器：厂商/规格/系统/登录方式；云上到 `newapi.zbjt.com:3000` 是否可达。
3. 域名与解析权限（Caddy 自动证书需要 80/443 可达）。
4. 本期是否同时做 A 方案（worker 镜像装 Claude Code）；不做则只有 `mock` 可跑。
