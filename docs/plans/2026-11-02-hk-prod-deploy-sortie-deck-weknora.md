# HK 生产机部署方案：Sortie Deck + WeKnora + Ollama/rerank

- 状态：执行中（2026-11-02）
- 目标机：`i-j6cazclc2tnx3fdg38g8`（`hk-4-cloud-datawarehouse-172-16-3-1.yuliyuli.prod`，cn-hongkong，私网 172.16.3.1，EIP 47.238.69.212）
- 原则：容器化部署；**完全不动**已在运行的 `posthog-hobby`（36 容器，占用 80/443/8443/5432）

## 1. 目标与约束

| 项 | 取值 |
|---|---|
| 系统 | Ubuntu 24.04.4 LTS，x86_64 |
| 规格 | 16 vCPU / 30 GB（已用 16 GB，可用约 13 GB）/ 无 swap |
| 磁盘 | `/` 63G（剩 27G，不用）；`/data` 252G（剩 233G，Docker Root 也在此） |
| Docker | 29.1.3 + Compose 2.40.3，root 执行 |
| 入口 | 公网 EIP `47.238.69.212`，仅 HTTP（无域名，不做 HTTPS） |
| 部署目录 | `/data/sortie-deploy/{sortie,weknora,ollama,rerank,caddy}` |
| 网络 | 新建 `sortie-net`（bridge），所有新容器接入 |

## 2. 端口规划

| 端口 | 用途 | 归属 |
|---|---|---|
| 8080 | Sortie Deck（Caddy → `sortie-api:8787`） | 新增 |
| 8088 | WeKnora UI（直连 `WeKnora-frontend:80`） | 新增 |
| 80 / 443 / 8443 / 443udp | PostHog | **不动** |
| 5432 | PostHog DB | **不动** |
| 8081 | PostHog temporal-ui | **不动** |

> 8443 已被 PostHog 的 Caddy 占用且安全组仅放行 Cloudflare 网段，因此本项目不做 HTTPS；纯 IP 访问也无法申请公共证书。

## 3. 安全组变更

安全组 `sg-j6c4nzdysk1vsauj9bxc` 现有入站：80/443/10086 对 `0.0.0.0/0`，8443 仅 Cloudflare 网段，5432 白名单。需新增两条：

```bash
aliyun ecs AuthorizeSecurityGroup --RegionId cn-hongkong \
  --SecurityGroupId sg-j6c4nzdysk1vsauj9bxc \
  --IpProtocol tcp --PortRange 8080/8080 --SourceCidrIp 0.0.0.0/0 \
  --Description "sortie-deck web"

aliyun ecs AuthorizeSecurityGroup --RegionId cn-hongkong \
  --SecurityGroupId sg-j6c4nzdysk1vsauj9bxc \
  --IpProtocol tcp --PortRange 8088/8088 --SourceCidrIp 0.0.0.0/0 \
  --Description "weknora ui"
```

另一个安全组 `sg-j6c3zuf5yw0fr2b10vz5` 的 `1/65535` 规则来源是 APIG 安全组与内网段，**不是公网**，无需改动。

## 4. 目标拓扑

```
公网 47.238.69.212
  ├─ :8080 → Caddy(独立) → sortie-api:8787     (SSE flush_interval -1)
  └─ :8088 → WeKnora-frontend:80              (直连)

docker network: sortie-net
  ├─ sortie-api / sortie-worker      (同一镜像 sortie-deck:local)
  ├─ WeKnora-app / postgres / redis / neo4j / docreader
  ├─ sortie-ollama                   (embedding: bge-m3, 1024 维)
  └─ sortie-rerank                   (rerank: bge-reranker-v2-m3, Cohere 协议)
```

## 5. 模型分工

| 用途 | 来源 | 配置 |
|---|---|---|
| 阶段执行（Claude Code harness） | DeepSeek 官方 Anthropic 端点 | `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`、`ANTHROPIC_MODEL=deepseek-flash` |
| 编排层 LLM（航线/房间/PRD/记忆） | DeepSeek 官方 OpenAI 端点 | `TDT_LLM_BASE_URL=https://api.deepseek.com/v1`、`TDT_LLM_MODEL=deepseek-flash` |
| WeKnora KnowledgeQA | 同上 | `base_url=https://api.deepseek.com/v1`、`deepseek-flash` |
| WeKnora Embedding | 自建 Ollama | `base_url=http://sortie-ollama:11434/v1`、`bge-m3`、`dimension=1024` |
| WeKnora Rerank | 自建 rerank | `base_url=http://sortie-rerank:8012`、`provider=generic`（Cohere 形状） |

DeepSeek 不提供 embedding / rerank，因此后两者必须自建；同理不部署 falkordb（WeKnora 图检索只支持 Neo4j）。

## 6. 资源上限（内存是硬约束）

| 服务 | mem_limit |
|---|---|
| sortie-api | 512m |
| sortie-worker | 2g（`replicas: 1`） |
| WeKnora-app | 1g |
| WeKnora-postgres | 1g |
| WeKnora-docreader | 1.5g |
| WeKnora-neo4j | 1g |
| WeKnora-redis | 128m |
| sortie-ollama | 2g |
| sortie-rerank | 3g |
| sortie-caddy | 128m |
| **合计** | **≈12.3g（可用 13g，需实测观察）** |

配套：`TDT_ENG_CONCURRENCY=1`、`TDT_STAGE_TIMEOUT_SECONDS=900`。

## 7. 执行步骤

### 步骤 0：推送代码（本地）

```bash
cd /Library/git-root/sortie-deck-todos && git push origin dev
```

服务器从 GitHub public 仓库 `git clone -b dev`，需要包含 `eb9f513`（`SORTIE_ENV_FILE` 支持）与 `978d977`。

### 步骤 1：目录与网络

```bash
mkdir -p /data/sortie-deploy/{sortie,weknora,ollama,rerank,caddy}
docker network create sortie-net
```

### 步骤 2：构建 Sortie 镜像

```bash
cd /data/sortie-deploy/sortie
git clone -b dev https://github.com/AdamsShen/sortie-deck-todos.git src
cd src
DOCKER_BUILDKIT=1 docker build --target runtime-harness -t sortie-deck:local .
```

预期：`naming to docker.io/library/sortie-deck:local`，镜像约 1.5GB。构建期需访问 npm/pypi（已实测可达）。

### 步骤 3：构建 rerank 镜像

上传 `local-rerank-service/{Dockerfile,rerank_server.py}` 到 `/data/sortie-deploy/rerank/`：

```bash
docker build -t sortie-rerank:1.0 .
```

### 步骤 4：Ollama + bge-m3

```bash
docker run -d --name sortie-ollama --network sortie-net --restart unless-stopped \
  --memory 2g -v /data/sortie-deploy/ollama:/root/.ollama ollama/ollama:latest
docker exec sortie-ollama ollama pull bge-m3
```

预期：`docker exec sortie-ollama ollama list` 出现 `bge-m3`（约 1.2GB）。

### 步骤 5：WeKnora 服务

上传官方 `docker-compose.yml` + `config/`，写 `/data/sortie-deploy/weknora/.env`：

```ini
WEKNORA_VERSION=<钉版本>
APP_PORT=127.0.0.1:8080
FRONTEND_PORT=0.0.0.0:8088
DB_PASSWORD=<强随机>
REDIS_PASSWORD=<强随机>
JWT_SECRET=<强随机>
SYSTEM_AES_KEY=<32 字节强随机>
NEO4J_PASSWORD=<强随机>
RETRIEVE_DRIVER=postgres
NEO4J_ENABLE=true
STORAGE_TYPE=local
WEKNORA_BOOTSTRAP_SYSTEM_ADMIN_EMAIL=<管理员邮箱>
```

compose 里给 app / frontend / neo4j 等加 `networks: [sortie-net]`（`external: true`），并给各服务加 `mem_limit`。

```bash
docker compose up -d && docker compose ps
```

### 步骤 6：WeKnora 初始化

`config/builtin_models.yaml` 声明三个默认模型（重启生效）：

```yaml
builtin_models:
  - id: builtin-embedding-bge-m3
    type: Embedding
    source: remote
    is_default: true
    name: bge-m3
    parameters:
      base_url: http://sortie-ollama:11434/v1
      provider: generic
      embedding_parameters:
        dimension: 1024
  - id: builtin-rerank-bge
    type: Rerank
    source: remote
    name: bge-reranker-v2-m3
    parameters:
      base_url: http://sortie-rerank:8012
      provider: generic
  - id: builtin-qa-deepseek
    type: KnowledgeQA
    source: remote
    is_default: true
    name: deepseek-flash
    parameters:
      base_url: https://api.deepseek.com/v1
      api_key: <DeepSeek key>
      provider: generic
```

建 3 个空库 + 生成 API Key：

```bash
BASE=http://127.0.0.1:8080; TOKEN=<登录后 Bearer>
for n in sortie-knowledge sortie-toolkit sortie-memory; do
  curl -s -X POST $BASE/api/v1/knowledge-bases -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$n\",\"type\":\"document\",\"indexing_strategy\":{\"vector\":true,\"keyword\":true,\"wiki\":false,\"graph\":false}}"
done
curl -s -X POST $BASE/api/v1/tenants/1/api-keys -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"sortie-deck","full_access":true}'
```

### 步骤 7：启动 Sortie Deck

`.env`（由 `.env.cloud` 改造）关键项：

```ini
TDT_ENV=production
TDT_HOST=0.0.0.0
TDT_RUN_BACKEND=sqlite
TDT_ENG_CONCURRENCY=1
TDT_STAGE_TIMEOUT_SECONDS=900
TDT_WEKNORA_BASE_URL=http://WeKnora-app:8080
TDT_WEKNORA_API_KEY=<步骤 6 的 key>
TDT_WEKNORA_KB_ID=<新 sortie-knowledge id>
TDT_TOOLKIT_WEKNORA_KB_ID=<新 sortie-toolkit id>
TDT_WEKNORA_MEMORY_KB_ID=<新 sortie-memory id>
TDT_LLM_BASE_URL=https://api.deepseek.com/v1
TDT_LLM_API_KEY=<DeepSeek key>
TDT_LLM_MODEL=deepseek-flash
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_API_KEY=<DeepSeek key>
ANTHROPIC_MODEL=deepseek-flash
```

```bash
cd /data/sortie-deploy/sortie/src
docker compose up -d --build
curl -s 127.0.0.1:8787/api/health
```

### 步骤 8：独立 Caddy

`/data/sortie-deploy/caddy/Caddyfile`：

```
:8080 {
    encode gzip
    reverse_proxy sortie-api:8787 {
        flush_interval -1
    }
}
```

```bash
docker run -d --name sortie-caddy --network sortie-net --restart unless-stopped \
  --memory 128m -p 8080:8080 \
  -v /data/sortie-deploy/caddy/Caddyfile:/etc/caddy/Caddyfile:ro \
  -v /data/sortie-deploy/caddy/data:/data caddy:2
```

## 8. 验收清单

| # | 检查 | 标准 |
|---|---|---|
| 1 | 容器状态 | 全部 `Up`，无反复重启 |
| 2 | Sortie 健康 | `curl 127.0.0.1:8787/api/health` → 200 |
| 3 | harness | `/api/harnesses` → `claude_code ok` |
| 4 | 外网入口 | `http://47.238.69.212:8080` 能登录（admin/admin123 后立刻改密） |
| 5 | 知识库 UI | `http://47.238.69.212:8088` 可访问 |
| 6 | 知识库连通 | Sortie 知识库页列出 3 个新库，`/api/knowledge/status` 的 `backend=weknora` |
| 7 | 检索 | 传 1 篇文档 → 解析完成 → `hybrid-search` 有命中且带 rerank 分数 |
| 8 | 任务闭环 | 小任务跑通 intake → eng_implement，工作区出现目标文件 |
| 9 | 内存 | `docker stats` 无容器贴近 limit，`free -g` 可用 > 1G |
| 10 | 重启存活 | `docker compose restart` 后数据仍在 |

## 9. 回滚

```bash
cd /data/sortie-deploy/sortie/src && docker compose down      # 保留 sortie-data 卷
cd /data/sortie-deploy/weknora && docker compose down         # 保留 WeKnora 卷
docker rm -f sortie-caddy sortie-ollama sortie-rerank
```

`posthog-hobby` 全程不触碰；如需彻底清理，再对上述 compose 加 `-v`（会删数据）。

## 9.1 执行结果（2026-11-02 完成）

| 项 | 结果 |
|---|---|
| 安全组 | 已放行 TCP 8080 / 8088（`0.0.0.0/0`） |
| 镜像 | `sortie-deck:local` 2.27GB、`sortie-rerank:1.0` 均已在服务器构建完成 |
| Sortie | `sortie-api` / `sortie-worker` / `sortie-caddy` 全部 Up，`/api/health` 200 |
| WeKnora | app / frontend / docreader / postgres / redis / neo4j 全部 Up（app healthy） |
| 模型 | 3 个内置模型注册成功：`bge-m3`(Embedding)、`bge-reranker-v2-m3`(Rerank)、`deepseek-flash`(KnowledgeQA) |
| 知识库 | 3 个空库建成并绑定 embedding 模型：`ab50e0df…`(knowledge)、`c4b9a41d…`(toolkit)、`7f0c289c…`(memory) |
| 检索链路 | 样本文档解析 `completed`，`/api/knowledge/retrieve` 命中 1 条、score 0.7295（含 rerank） |
| 任务链路 | mock 任务由 worker 领取并跑完 intake，进入 HITL |
| harness | `/api/harnesses` → `claude_code ok` |
| 外网 | `http://47.238.69.212:8080` 200、`http://47.238.69.212:8088` 200 |
| 内存 | 29G 中已用 19G，可用 10G；各容器均未贴 limit |

执行中遇到并解决的四个问题：

1. **8080 抢占**：WeKnora 的 `APP_PORT` 原为 `127.0.0.1:8080`，与 Caddy 冲突 → 改为 `127.0.0.1:8090`。
2. **neo4j 未启动**：官方 compose 把 neo4j 放在 `neo4j` profile，`NEO4J_ENABLE=true` 时 app 会一直等它 → 用 `docker compose --profile neo4j up -d`。
3. **rerank 权重目录权限**：bind mount `/data/sortie-deploy/rerank/hf` 由 root 创建，容器用户 uid 10001 无法写 → `chown -R 10001:10001`。
4. **SSRF 拦截自建模型**：WeKnora 的模型 base_url 校验拒绝私网 IP → `.env` 增加 `SSRF_WHITELIST_EXTRA=sortie-ollama,sortie-rerank,api.deepseek.com`。
5. **知识库缺 embedding_model_id**：首次建库未指定模型，解析报 `model ID cannot be empty` → 删库重建并带 `embedding_model_id=builtin-embedding-bge-m3`。

## 10. 风险

| 风险 | 影响 | 处置 |
|---|---|---|
| 内存只剩 13G | 与 PostHog 抢内存可能 OOM | 已设 mem_limit；先按 `ENG_CONCURRENCY=1` 跑，观察 `docker stats` |
| 8080/8088 暴露公网无鉴权 | Sortie 有登录，WeKnora 有 Basic-less 登录 + 注册开放风险 | WeKnora 首次登录后立即设强密码并确认注册策略 |
| 自签/无 HTTPS | 账号密码明文传输 | 仅限内网/可信网络使用；对外必须加 TLS 反代 |
| Ollama/rerank 冷启动 | 首次请求超时 | rerank healthcheck 判 ready；RAG 超时按实测调整 |
| 构建期依赖外网 | npm/pypi 拉取失败 | 失败时加 `--build-arg NPM_REGISTRY=https://registry.npmmirror.com` |
| 生产模式密钥 | `TDT_ENV=production` 用默认 token secret 会启动失败 | 已生成随机 `TDT_TOKEN_SECRET` |
