# 知识库两条链路源码解析：入库索引 与 检索生成

> 时间：2026-10-19。对照 `docs/zh/notes/rag-industry-survey.md` 中"生产级标准 Pipeline"的业界图，逐环节落到本项目真实代码。涉及两个仓库：
> - **Sortie Deck**（本项目，Python）：导入编排 + 目录管理 + graph 检索模式
> - **WeKnora 服务端**（`/Library/git-root/weknora`，Go + docreader Python）：解析、分块、双索引、混合检索、RRF、Rerank、问答管线

## 0. 职责边界一张图

```
Sortie Deck（本项目）                          WeKnora 服务端
─────────────────────                        ────────────────────────────────
POST /api/knowledge/import ──HTTP──▶  【离线】docreader 解析 → TextSplitter 分块
knowledge_gateway.import_doc                   → 嵌入 + BM25 双索引（ParadeDB）
                                               → (graph 开启时) 逐 chunk LLM 抽实体 → Neo4j
/api/knowledge/retrieve  ──HTTP──▶    【在线】hybrid-search：双路召回 + RRF + Rerank
  ├─ hybrid 模式                                或 knowledge-chat：改写 + 实体搜索 + Top-K + LLM
  └─ graph 模式（knowledge-chat SSE）
本地 catalog（data/knowledge.json）            （Sortie Deck 不做任何 RAG 本体）
```

---

## 1. 离线链路：上传 → 解析 → 分块 → 双索引

### 1.0 导入瞬间 vs 之后：同步/异步时序边界

**同步发生的事（导入请求内，秒级返回）**：

1. `api/app.py:598` 导入端点把上传文件落到临时文件 →
   `knowledge_gateway.py:156` `import_doc` →
   `weknora.py:223` `create_from_file`，向 WeKnora 发 multipart 上传
   （`POST /knowledge-bases/{kb_id}/knowledge/file`）
2. WeKnora 接收文件、登记一条 knowledge 记录后**立即返回**——此时
   `parse_status` 通常是 `processing`（矩阵测试里 .ppt/.xls 响应即可见）
3. 本地 catalog 同时镜像一条记录（`_upsert_catalog_from_remote`）：
   **body 为空、只存元信息与 parse_status——正文不在本项目，活在 WeKnora**
   （`KnowledgeDoc.body=""` 约定见 `docs/zh/modules/knowledge.md` §4）

**之后异步发生的事（WeKnora 服务端内部，本项目不参与也不感知过程）**：

文档解析 → 分块 → embedding → 写入 WeKnora 自己的双索引（详见 §1.2-1.4）；
graph 开启时再追加逐 chunk 实体抽取（§1.4 末）。

**何时可检索**：`parse_status` 变为 `completed` 后，文档的 chunk 才会被
混合检索/graph 检索命中。catalog 侧用 `refresh_parse_status()`
（`knowledge_gateway.py:215`）从 WeKnora 回读状态更新本地镜像。

**耗时差异**：小文本文件（md/txt）几秒内 completed；大 PPT/扫描件要等
OCR/模型推理，可能几十秒到几分钟；graph 抽取逐 chunk 调 LLM，再叠加
数十秒级，个别 chunk 失败不阻塞整体完成。

> 检索端注意：completed 之前的文档对检索"不存在"（无 chunk 可召回），
> 不会报错也不会部分命中——批量导入后要等状态再验证召回。

### 1.1 上传与任务入队（WeKnora）

上传接口落盘后立即投递异步任务，不阻塞请求：

`internal/application/service/knowledge_create.go:257`
```go
task := asynq.NewTask(
    types.TypeDocumentProcess,
    payloadBytes,
    documentProcessTaskOptions(s.config, asynq.MaxRetry(3))...,
)
```
- 任务队列是 **asynq**（Redis），失败自动重试 3 次；解析超时、各阶段失败都有独立的 failKnowledge* 兜底（`knowledge_process.go:270-317`）
- 本项目侧对应：`knowledge_gateway.py:140` `import_doc()` 按 file/url/body 三种来源调 `create_from_file / create_from_url / create_manual`（`weknora.py:187-238`），全文直接推给 WeKnora，**Sortie Deck 不碰解析与分块**

### 1.2 解析（docreader，独立 gRPC 服务）

- 解析器在 `docreader/parser/`（Python）：`pdf_parser.py`（含 OCR/版面分支）、`docx_parser.py`、`excel_parser.py`、`html_parser.py`、`image_parser.py` 等，另有进程内 Office 解析引擎 `third_party/anydoc-go`
- app 通过 gRPC 调 docreader（`DOCREADER_ADDR=docreader:50051`），图片走多模态 VLM 生成 caption/OCR 文本
- 解析产物是带结构的 Markdown 文本 + 图片资源

### 1.3 分块（docreader/splitter/splitter.py:35）

核心类 `TextSplitter`，策略比"固定窗口"复杂：

```python
separators:      ["\n", "。", " "]          # 分隔符优先级降序
protected_regex: [$$..$$公式, ![图片], [链接], 表格头/表体, ```代码块]
```
- `split_text()` 返回 `(start, end, text)` 三元组，**带原文偏移**（供检索命中回溯原文位置）
- 受保护片段（表格/代码/公式）整体不切断，超长再二次分段
- 标题钩子 `HeaderTracker` 把所属标题面包屑带入 chunk（上下文保留）

分块配置来自知识库级 `ChunkingConfig`（`internal/types/knowledgebase.go:277`）：`chunk_size`（100-10000）、`chunk_overlap`、`separators`，另支持**父子分块**（`:291-296`，parent 4096 / child 384，child 参与检索、parent 供生成上下文）与 `TokenLimit` 按 token 截断。本项目实例用默认 `auto / 512 / 80`。

### 1.4 双索引（Postgres/ParadeDB，一个库两条索引）

chunk 落库后双写两套索引（`internal/application/repository/retriever/postgres/repository.go`）：

| 路 | 索引 | 代码位置 |
|---|---|---|
| 关键词（稀疏） | ParadeDB **BM25** 倒排 | `repository.go:241` `paradedb.score(id) as score` |
| 向量（稠密） | **pgvector** HNSW | `repository.go:410` `embedding::halfvec(%d) <=> $1::halfvec(%d)` |

- embedding 在入库阶段批量生成后写入同一张表（半精度 halfvec 省一半内存）
- 同一库双索引是 `hybrid-search` 开箱即用的原因，无需独立向量库服务
- **GraphRAG 双写**（本项目已开启）：解析完成后进入增强扇出，`extract.go:119` 把有正文的文本 chunk 逐个投递 `TypeChunkExtract`（独立 `QueueGraph` 队列），每 chunk 一次 LLM 调用抽取实体/关系，`apoc.merge.node` 幂等写 Neo4j（节点标签 `ENTITY<kb_id>`，属性含 chunks 回链）。本项目实例已建 193 个实体；长 chunk 偶发超 8192 token 上限导致该 chunk 抽取失败（重试 3 次后放弃，不影响其他 chunk）

### 1.5 离线链路小结（本项目现状）

```
项目 /api/knowledge/import → gateway.import_doc → WeKnora create_manual/file
  → asynq TypeDocumentProcess (retry 3)
  → docreader 解析 (PDF-OCR/Office/HTML/图片 VLM)
  → TextSplitter 分块 (auto/512/80，表格代码公式保护，标题面包屑)
  → pgvector 向量 + ParadeDB BM25 双索引
  → [graph] QueueGraph 逐 chunk LLM 抽实体关系 → Neo4j
```

---

## 2. 在线链路：Query → 混合检索 → RRF → Rerank → Top-K → LLM

### 2.1 入口：本项目两种模式（`knowledge_gateway.py:267` `retrieve()`）

```
retrieve(query, initiative_id?)
├─ ① 任务命名空间（本地 catalog 关键词打分，+10 加权置顶）
├─ ② 全局池
│    ├─ hybrid 模式：WeKnoraClient.hybrid_search() → POST /hybrid-search   (~0.3s)
│    └─ graph  模式：_retrieve_graph() → /sessions + /knowledge-chat SSE    (~10-40s)
│         失败/空结果 → 自动降级 hybrid-search（检索永不中断）
└─ 合并去重（remote_id 去重）→ RetrieveHit[]
```

### 2.2 hybrid-search 服务端内幕（无 LLM 参与的一段）

`internal/application/service/knowledgebase_search.go:125` `HybridSearch`：

1. **多库鉴权 + embedding 一致性校验**（跨不同 embedding 空间的多库检索直接报错，`:200`）
2. **过量召回**：`matchCount = max(matchCount×5, 默认TopK) × 库数`，上限 `maxRetrievalPoolSize`（`:233`）——粗召回池远大于最终 Top-K
3. **查询向量化一次**：`GetQueryEmbedding` 只算一次，随 params 传给各 store（`:246`，避免 N 路 store 重复调 embedding API）
4. **双路并行召回** → 融合

**RRF 融合**（`knowledgebase_search_fusion.go:156` `fuseWithRRF`）：

```
rrfScore = vectorWeight/(k+vectorRank) + keywordWeight/(k+keywordRank)
score    = rrfScore / maxRRF        # 除以 (w_v+w_k)/(k+1) 归一化到 [0,1]
```
- rank 取该 chunk 在任意一路列表中的**最好名次**（`bestRanks`），不是拼接位次
- 归一化是 WeKnora 的改进：裸 RRF 峰值 ~0.016，会让 hybrid 命中在任何单路结果面前吃亏，导致 rerank/MMR 无分数可用（注释原文）
- k、双路权重来自租户级 `RetrievalConfig`，可调

**Rerank**（`knowledgebase_search_rerank.go:66` `HybridSearchWithRerank`）：
- 未配 rerank 模型 → `RerankOutcomeDisabled`，直接返回融合结果（**本项目现状**：rerank 模型未配置，此阶段跳过）
- 配置后：召回池扩到 `max(matchCount, rerank.top_k)` → cross-encoder 对 query×chunk 打分 → 按阈值过滤 → 取 topK，附 `RerankDiagnostics`（模型/阈值/候选数）

### 2.3 knowledge-chat（graph 模式走的全管线，含 LLM）

管线是**插件事件链**，事件定义 `internal/types/chat_manage.go:366-378`，装配 `session_knowledge_qa.go:217-229`：

```
LOAD_HISTORY → MEMORY_RECALL → QUERY_UNDERSTAND → CHUNK_SEARCH_PARALLEL
→ CHUNK_RERANK → [WEB_FETCH] → CHUNK_MERGE → FILTER_TOP_K
→ INTO_CHAT_MESSAGE → CHAT_COMPLETION_STREAM
```

逐环节对照业界图：

| 业界图环节 | WeKnora 实现 | 现状 |
|---|---|---|
| Query 改写 | `chat_pipeline/query_understand.go`：LLM 重写查询写入 `RewriteQuery`（`:64` 默认透传、`:123` 标注 `query_rewrite` 的 LLM 调用、`:398` 覆写），后续检索用改写后的查询（`merge_history.go:52`） | ✅ 生效（graph 模式） |
| HyDE | 无实现 | ❌ 未做 |
| 混合检索（双路） | `CHUNK_SEARCH_PARALLEL`：并行双路召回（同 §2.2 的融合逻辑） | ✅ |
| **图谱扩展**（GraphRAG 独有） | `QUERY_UNDERSTAND` 内 `extract_entity.go` 用 LLM 从查询抽实体 → `ENTITY_SEARCH` 事件 `search_entity.go` 查 Neo4j（`n.name CONTAINS`，一跳邻居+关系），补回关联 chunk 进候选池 | ✅（Neo4j 已建 193 实体） |
| Rerank | `CHUNK_RERANK` | ⚠️ 未配模型，跳过 |
| RRF 融合 | `CHUNK_MERGE` | ✅ |
| Top-K | `FILTER_TOP_K`（`chat_pipeline/filter_top_k.go`） | ✅ |
| LLM 生成 | `INTO_CHAT_MESSAGE`（组装上下文+引用标记）→ `CHAT_COMPLETION_STREAM`（SSE 流式） | ✅ |

SSE 事件流（`response_type`）：`agent_query / thinking / tool_call / references / answer / complete`。**本项目 `knowledge_chat_references()`（`weknora.py:244`）拿到 `references` 事件（含 `knowledge_references` chunk 列表，混合检索+图谱扩展的合并结果）后立即断开连接**——不等 LLM 生成正文，把 40s 的调用降为纯检索用途。

### 2.4 在线链路小结（本项目两种模式实测）

| | hybrid（`/hybrid-search`） | graph（`/knowledge-chat`） |
|---|---|---|
| 改写/HyDE | 无 | LLM 查询改写 ✅ / HyDE 无 |
| 双路召回 + RRF | ✅ | ✅ |
| 图谱扩展 | 无 | ✅（实体抽取→Neo4j 一跳） |
| Rerank | 跳过（未配模型） | 跳过（未配模型） |
| LLM 参与 | 无 | 查询改写 + 实体抽取 + 答案生成 |
| 实测延迟 | ~0.3s | ~10-40s |

---

## 3. 未启用/可补的环节（对照业界图的差距）

1. **Rerank 未配置**：在 WeKnora「设置 → 模型」加一个 rerank 模型（如 SiliconFlow 的 bge-reranker / 腾讯 LKEAP），`CHUNK_RERANK` 即自动生效，hybrid 与 graph 两条链路同时受益，无需改本项目代码
2. **HyDE 未实现**：WeKnora 无此环节；如需要可在本项目 `retrieve()` 前置一步（用网关 chat 模型生成假设文档再作为 query 送 hybrid-search），代价是每次检索多一次 LLM 调用
3. **查询改写只在 graph 模式有**：hybrid 模式的 query 原样直送；多轮任务场景可在 `_retrieve_mission`/全局池前做轻量改写
4. **任务命名空间检索未走混合检索**：`_retrieve_mission`（`knowledge_gateway.py:326`）仍是本地 catalog 关键词打分；改进方向见 `rag-industry-survey.md` §4（WeKnora folder 过滤或多 KB）

## 4. 参考代码索引

| 环节 | 文件 |
|---|---|
| 导入编排（项目侧） | `src/sortie_deck/knowledge_gateway.py:140` import_doc；`src/sortie_deck/weknora.py:187-238` |
| 入队/重试 | WeKnora `internal/application/service/knowledge_create.go:257` |
| 解析器 | WeKnora `docreader/parser/*.py`、`third_party/anydoc-go` |
| 分块器 | WeKnora `docreader/splitter/splitter.py:35`；配置 `internal/types/knowledgebase.go:277-300` |
| 双索引 | WeKnora `internal/application/repository/retriever/postgres/repository.go:241,410` |
| 图谱抽取 | WeKnora `internal/application/service/extract.go:119`；Neo4j 写入 `.../retriever/neo4j/repository.go` |
| 混合检索 | WeKnora `internal/application/service/knowledgebase_search.go:125` |
| RRF | WeKnora `internal/application/service/knowledgebase_search_fusion.go:156` |
| Rerank | WeKnora `internal/application/service/knowledgebase_search_rerank.go:66` |
| 问答管线装配 | WeKnora `internal/application/service/session_knowledge_qa.go:217`；事件表 `internal/types/chat_manage.go:366` |
| 查询改写/实体抽取 | WeKnora `chat_pipeline/query_understand.go`、`chat_pipeline/extract_entity.go`、`chat_pipeline/search_entity.go` |
| graph 检索模式（项目侧） | `src/sortie_deck/weknora.py:244` knowledge_chat_references；`src/sortie_deck/knowledge_gateway.py:335` _retrieve_graph |
