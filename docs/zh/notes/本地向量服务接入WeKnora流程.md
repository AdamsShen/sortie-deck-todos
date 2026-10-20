# 本地向量（embedding）服务接入 WeKnora 流程

> 2026-10-20 落地梳理（配置于 2026-10-19 初始化时完成）。目标：说明知识库向量化能力由本机 Ollama 的 `bge-m3` 提供，WeKnora 如何调用它完成「文档入库向量化」与「查询向量化」，以及与 rerank 方案的异同。

## 一、组件与端口

| 组件 | 地址 / 位置 | 说明 |
|---|---|---|
| Ollama 服务（宿主机） | `http://127.0.0.1:11434` | 承载向量模型进程，已加载 `bge-m3:latest` |
| 向量模型 | `bge-m3`（BAAI，MIT 开源） | 1024 维，多语言（含中文），中英混排检索效果好 |
| WeKnora 后端 | Docker 容器 `WeKnora-app`，宿主机 `127.0.0.1:8080` | 向量化的「编排方」，自身不算向量 |
| 向量库 | `WeKnora-postgres`（ParadeDB，pgvector） | 存 chunk 向量 + BM25 关键词索引 |
| WeKnora 配置 | `.env:356` `OLLAMA_BASE_URL=http://host.docker.internal:11434` | 容器内访问宿主机 Ollama 的地址 |
| 模型配置（DB） | `models` 表，租户 10000：name=`bge-m3`，base_url=`http://host.docker.internal:11434/v1` | 走 Ollama 的 OpenAI 兼容门面（`/v1/embeddings`） |
| 调用方 | Sortie Deck（间接） | 不直接调 embedding，向量对其透明 |

## 二、职责划分：与 rerank 方案同构

与《本地重排序服务接入 WeKnora 流程》完全同构——**WeKnora 提供编排，模型跑在本地服务里**：

```text
方案（本项目所选）：本地 Ollama
  WeKnora 后端 ──HTTP──→ host.docker.internal:11434/v1/embeddings ──→ bge-m3 算向量
```

- WeKnora 自己**不算向量**：它负责何时算（入库/查询）、批量多少、维度校验、失败处理。
- Ollama 暴露 **OpenAI 兼容门面**（`/v1/embeddings`），所以在 WeKnora 里按「Custom (OpenAI-compatible)」provider 配置即可，与 vLLM / SiliconFlow 等远程 embedding 接法完全一致（`.env:380-386` 注释亦说明：`source: remote` + API 地址，或 Ollama 专用 `source: local` 两种接法，本项目用前者）。
- 对比备选：SiliconFlow `BAAI/bge-m3`（免费额度，数据出网）、TEI 自建。选 Ollama 因为零成本、数据不出域、且本机已在跑。

## 三、WeKnora 配置（已完成，记录备查）

1. `.env` 配置（`/Library/git-root/weknora/.env`）：
   - `OLLAMA_BASE_URL=http://host.docker.internal:11434`（容器 → 宿主机 Ollama）
   - `OLLAMA_OPTIONAL=true`（Ollama 不可用仅告警不阻断启动）
2. 界面配置（设置 → 模型 → Embedding）：
   - 厂商：Custom (OpenAI-compatible)；模型名：`bge-m3`
   - BaseURL：`http://host.docker.internal:11434/v1`（注意带 `/v1`，OpenAI 兼容门面前缀）
   - 维度：1024（须与建库时一致，**中途换模型/维度会导致旧向量失效**）
3. **知识库绑定**：每个知识库（`kb.EmbeddingModelID`）在创建时锁定一个 embedding 模型，检索与入库都用它。

## 四、端到端链路（向量化的两个时机）

### ① 入库向量化（文档解析后批量）

```text
导入知识（Sortie Deck → WeKnora /knowledge/file|url|manual）
  → docreader 解析 → 切片（chunk）
  → internal/application/service/chunk.go:771  取知识库绑定的 embedding 模型
  → chunk.go:817  engine.BatchIndex：批量向量化 + 写入向量库（pgvector）+ BM25 索引
```

### ② 查询向量化（检索时实时）

```text
检索请求（Sortie Deck → WeKnora /hybrid-search）
  → internal/application/service/knowledgebase_search.go:554  embeddingModel.Embed(query)
  → 向量检索（cosine）+ BM25 关键词检索 → RRF 融合 → 【rerank】→ TopK
```

关键点：
- **query 和 chunk 必须用同一个模型向量化**（WeKnora 用知识库绑定关系保证），否则向量空间不一致、检索完全失效。
- 批量 embedding 大小可调（`.env:357`）；并发池大小可调（`.env:832`，遇 429 限流时调小）。
- WeKnora 查询路径带 `WithEmbedQuery(ctx)` 标记，便于 bge 类模型区分「查询/文档」两种编码模式。

## 五、Sortie Deck 侧的感知

```text
1:3:src/sortie_deck/weknora.py
Sortie Deck does not re-implement RAG. Search always uses WeKnora's hybrid-search.
```

- Sortie Deck **不接触向量**：导入只传文本/文件，检索只拿结果块（`WeKnoraHit.content/score`）。
- 换 embedding 模型（如未来换更大的模型）对 Sortie Deck 代码零影响——只需在 WeKnora 侧重配 + 重建知识库索引（或新建库）。

## 六、与 rerank 方案的对照

| 维度 | 向量（embedding） | 重排（rerank） |
|---|---|---|
| 模型 | bge-m3（Ollama，11434） | bge-reranker-v2-m3（自建 FastAPI，8012） |
| 协议 | OpenAI 兼容 `/v1/embeddings` | Cohere 兼容 `/rerank` |
| 调用时机 | 入库批量 + 查询实时（每路一次） | 混合检索 RRF 融合后一次 |
| 配置位置 | 设置 → 模型 → Embedding + 知识库绑定 | 设置 → 模型 → Rerank（租户自动选用） |
| 不可用影响 | **入库失败**、检索退化为纯关键词 | 自动降级为 RRF 检索序，检索不中断 |

注意不对称性：embedding 挂掉影响比 rerank 严重（新文档无法入库、查询无法向量化）；Ollama 常驻本机风险低，但 Mac 重启后同样需要确认 Ollama 已启动。

## 七、运维与验证

```bash
# Ollama 状态与模型清单
curl -s http://127.0.0.1:11434/api/tags | python3 -m json.tool
# 手动验证 embedding（应返回 1024 维向量）
curl -s http://127.0.0.1:11434/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"bge-m3","input":"连通性测试"}' | python3 -c "import json,sys; print('dim =', len(json.load(sys.stdin)['data'][0]['embedding']))"
# 容器内连通性（模拟 WeKnora 视角）
docker exec WeKnora-app sh -c "curl -s --max-time 5 http://host.docker.internal:11434/api/tags"
```

当前实测状态：Ollama 运行中，已加载 `bge-m3:latest`；`models` 表租户 10000 下 `bge-m3`（source=remote，base_url `.../v1`）配置生效；15 篇文档已全部向量化入库。
