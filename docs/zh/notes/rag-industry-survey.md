# RAG 业界实现调研：知识库检索的技术选型

> 调研时间：2026-10。目的：梳理业界知识库 / RAG 的主流实现路线与检索管线，作为本项目（Sortie Deck + WeKnora）知识库模块的选型依据与演进参考。

## 1. 两条主流路线

### 路线 A：自建 Pipeline（框架 + 组件拼装）

| 框架 | 定位 | 适用场景 |
|---|---|---|
| **LangChain / LangGraph 1.0** | 生态最全，Agent 编排最强（1.0 已 GA，Agent 底层跑在 LangGraph 引擎上） | 复杂 Agent 应用 + RAG |
| **LlamaIndex** | 检索优化最深：子节点拆分、父子分块、索引优化，开箱即用 | 检索准确率优先 |
| **Haystack 2.x** | Pipeline 每步可观测、可替换，生产特性完善（缓存 / 权限 / 监控），内置 BM25 + dense 混合检索 | 企业级、可控可维护 |
| **DSPy** | 把 prompt / 检索参数当作可优化对象，程序化调优 | 精调场景 |

选型经验（社区共识）：

- 偏重**准确率**选 LlamaIndex；偏重**可控可维护**选 Haystack；要 **Agent + 工具生态**选 LangChain/LangGraph。
- 实用铁律：**先让纯 RAG 跑通，再加 Agent 外壳**，否则出问题分不清是检索的锅还是编排的锅。

### 路线 B：开箱即用的知识库平台

| 平台 | 特点 |
|---|---|
| **Dify**（~96k★） | 流程编排最完备，Agentic Workflow，中文友好 |
| **RAGFlow**（~68k★） | 深度文档理解（DeepDoc）、可视化分片、模板化切片，中文文档处理最强 |
| **FastGPT / MaxKB** | 国产低代码知识库，pgvector / Milvus 后端 |
| **WeKnora**（腾讯开源） | 企业级文档理解 + 混合检索 / GraphRAG + 原生微信生态 + 内置 MCP Server |

**本项目（Sortie Deck）选择的是路线 B 中的 WeKnora**：仓库内只做导入与目录管理，解析、分块、嵌入、混合检索全部委托 WeKnora 服务端。

## 2. 生产级检索的标准 Pipeline

2025 年后业界已收敛出相对固定的成熟范式：

```
离线：文档 → 解析(OCR/版面) → 分块 → [向量索引 + 倒排索引] 双索引
                                          │
在线：Query → 改写/HyDE → 混合检索(双路) → RRF 融合 → Rerank → Top-K → LLM
```

### 关键环节的业界共识

1. **混合检索成为默认**：单路纯向量召回对专有名词、编号、代码不敏感。标准做法是 BM25（稀疏）+ 向量（稠密）双路并行召回。
2. **RRF 融合**：两路分数量纲不同，用 Reciprocal Rank Fusion 按排名位次融合，已是标准算法；另一派用加权打分（如 `0.6×vector + 0.4×bm25`），需按场景调权。
3. **Rerank 三段式**：粗召回（双路各 50–100 条）→ cross-encoder 重排（Cohere / BGE-reranker / 腾讯 LKEAP 等）→ 留 Top 5–10 条进 prompt。
4. **查询增强**：Query 改写、HyDE（假设文档嵌入）、多轮迭代检索。
5. **分块进化**：固定切块 → heading / 语义感知切分 → 父子分块（child 用于检索、parent 用于生成上下文；WeKnora 的 `enable_parent_child` 即此机制）。
6. **向量库选型**：pgvector（中小规模够用、省运维）→ Milvus / Qdrant（大规模）→ ES / OpenSearch（已有关键词基础设施时复用）。

### Naive RAG 的典型短板（上述增强的动因）

- 单路向量召回不敏感 → 混合检索弥补；
- 相似度高 ≠ 答案相关 → Rerank；
- 固定切块破坏语义（表格、标题层级）→ 感知式 / 父子分块；
- 多跳问题一次检索拿不齐证据 → GraphRAG / Agentic RAG。

## 3. 前沿方向（2024–2025）

- **GraphRAG**：抽取实体关系建知识图谱（如 Neo4j），解决多跳推理；代价是 token 消耗高、动态图更新难。
- **Agentic RAG**：LLM 自主决定"要不要检索、检索什么、够不够"（Self-RAG / Adaptive-RAG）；RAG 逐渐演化为 Agent 系统的子模块。
- **多模态 RAG**：图表、图片进入向量空间（WeKnora v0.8.3 的多模态 Embedding 即此方向）。

行业趋势：基础性创新放缓，重心转向已有技术的组合优化与生产化；框架同质化明显，头部收敛为少数几个。

## 4. 对照本项目（Sortie Deck）

### 现状

- 本项目对 WeKnora **只有薄封装**（`src/sortie_deck/weknora.py` HTTP client + `src/sortie_deck/knowledge_gateway.py` 编排），分块 / 嵌入 / 混合检索全部由 WeKnora 服务端完成——WeKnora 默认 `auto` 自适应分块、chunk 512 字符、overlap 80、分隔符 `["\n\n", "\n", "。"]`。
- 恰好覆盖了标准 Pipeline 的核心共识：`hybrid-search`（BM25 + 向量双路）、服务端 Rerank、auto / 父子分块、可选 GraphRAG。
- 本项目自有的增量设计是**任务命名空间隔离**：每需求产物打 `ini:<id>` 标签、回写 `missions/<id>/` 目录、检索时命名空间内 +10 分加权置顶（`knowledge_gateway.py` `retrieve` / `index_mission_artifact`）。

### 与业界做法的差距与改进方向

1. **任务隔离实现偏轻量**：命名空间检索用的是本地 catalog 关键词打分（非混合检索）。业界对应"scoping / 权限过滤检索"。若接入 WeKnora，更彻底的做法是：
   - 用 WeKnora **多 KB**（对应 roadmap P2 的多 `kb_id`）或 **folder 过滤**做任务隔离，让命名空间检索也吃到混合检索红利；
2. **当前实际未启用 WeKnora**：`.env` 中 `TDT_KB_BACKEND=weknora` 等配置处于注释状态，实际跑的是本地 JSON catalog 关键词降级路径（`knowledge_pack.py` 的 `score_doc`）。落地第一步是启用 WeKnora 后端并验证检索链路；
3. **缺评估**：业界共识是上线前做召回率评估（WeKnora 自带 Recall / BLEU / ROUGE 端到端评估），建议为知识库检索建立回归评估。

## 5. 参考来源

- [Haystack vs LangChain vs LlamaIndex: Picking a RAG Framework in 2026](https://dreaming.press/posts/haystack-vs-langchain-vs-llamaindex.html)
- [LangChain vs LlamaIndex vs Haystack（aidoptio 中立对比）](https://aidoptio.com/blog/langchain-vs-llamaindex-vs-haystack)
- [RAG 优化策略 2025：GraphRAG / Agentic RAG / Hybrid Search](https://synthimind.com/blog/rag-optimization-strategies-2025/)
- [Hybrid Retrieval for Enterprise RAG: BM25, Vectors, or Both](https://ragaboutit.com/hybrid-retrieval-for-enterprise-rag-when-to-use-bm25-vectors-or-both/)
- [RAG Matured from Demo to Production Pattern（Harbor 2025 年度回顾）](https://harborsoftware.com/2025/12/19/year-in-ai-what-actually-shipped-2025/)
- [RAG 开源框架调研笔记（含 Dify / RAGFlow / WeKnora 对比表）](https://devpress.csdn.net/v1/article/detail/155273943)
- [WeKnora 官方文档：配置详解 / 分块机制](https://weknora.weixin.qq.com/docs)
- [Tencent/WeKnora GitHub](https://github.com/Tencent/WeKnora)
