# 本地重排序（bge-reranker）服务接入 WeKnora 流程

> 2026-10-20 落地记录。目标：Sortie Deck 任务检索时，WeKnora 混合检索（向量 + BM25 → RRF 融合）之后自动经过本地 bge-reranker-v2-m3 重排，提升 TopK 命中质量。

## 一、组件与端口

| 组件 | 地址 / 位置 | 说明 |
|---|---|---|
| 本地 rerank 服务 | `http://127.0.0.1:8012` | FastAPI，Cohere 兼容 `/rerank`，模型加载一次常驻 |
| 服务代码 | `/Library/git-root/weknora/local-rerank-service/rerank_server.py` | 单文件，Python 3.12 + FlagEmbedding |
| WeKnora 后端 | Docker 容器 `WeKnora-app`，宿主机 `127.0.0.1:8080` | hybrid-search 接口入口 |
| WeKnora 前端 | `http://127.0.0.1:8088` | 配置模型的界面 |
| 模型 | `BAAI/bge-reranker-v2-m3`（MIT 开源，568M 参数） | 半精度加载，权重缓存于 `~/.cache/huggingface` |
| 调用方 | Sortie Deck（`src/sortie_deck/weknora.py`） | 用 API Key 认证，非账号密码 |

## 二、为什么选 bge-reranker-v2-m3

- **开源免费**：MIT 协议，可本地自建，无 API 调用费（对比：SiliconFlow 托管版免费额度、腾讯 LKEAP 按量计费）。
- **资源匹配**：Apple M3 / 16GB 实测峰值内存 2.25GB，无需 GPU。
- **效果**：多语言含中文、8192 长上下文；相关文档得分 0.81+，无关文档 ≈0，区分度可用于阈值过滤。

## 三、服务实现要点（rerank_server.py）

- **协议**：实现 Cohere wire 格式（WeKnora 「Custom (OpenAI-compatible)」provider 的默认 rerank 协议）：
  - `POST /rerank`：请求 `{model, query, documents: [str], top_n?, return_documents?}` → 响应 `{results: [{index, relevance_score, document?}]}`，按分数降序。
  - `GET /health`：存活探针。
- **模型加载**：启动时加载一次（走缓存约 3.6s），推理用线程锁串行化（FlagReranker 非线程安全，本地单用户吞吐足够）。
- **分数标度**：`compute_score(normalize=True)` 输出 0~1 概率，对应 WeKnora 的 `probability` 标度（**勿选 logit**，否则租户阈值失效）。
- **镜像加速**：`HF_ENDPOINT=https://hf-mirror.com`（在 import 前设置）。

## 四、WeKnora 侧配置（界面操作，零代码改动）

1. 登录 `http://127.0.0.1:8088`，账号 `admin@sortie.local`（密码见 `docs/zh/notes/weknora-graphrag-plan.md`）。
2. **设置 → 模型 → Rerank 标签页** → 添加模型：
   - 厂商：**Custom (OpenAI-compatible)**
   - BaseURL：`http://host.docker.internal:8012`
     （Docker 容器内不能用 localhost——容器内 localhost 指向容器自身；已实测容器可达宿主机）
   - 模型名：`bge-reranker-v2-m3`
   - API Key：占位值 `local`（本地服务不校验）
   - score_scale：保持默认 `probability`
3. **无需其他配置**：租户检索配置默认 `RerankModelSourceAuto`，自动选用租户下第一个 Rerank 模型。

### 归属关系（重要）

模型与检索配置均为**租户级隔离**。Sortie Deck 的 API Key（名为 `sortie-deck`，full_access）属于租户 10000「admin's Workspace」（即 `admin@sortie.local` 的空间），所以 rerank 必须配在该账号下；其他租户（如 10001 adams）配了也无效。

## 五、端到端检索链路

### 写入链路

```text
用户/Agent 导入知识（正文/文件/URL）
  → KnowledgeGateway.import_doc()                src/sortie_deck/knowledge_gateway.py:156
  → WeKnoraClient.create_manual/from_file/url    src/sortie_deck/weknora.py:192-243
  → WeKnora docreader 解析 → 切片 → embedding → pgvector + 关键词索引
  → 本地 catalog 只存元数据镜像（正文留在 WeKnora）
```

### 检索链路（重排介入点）

```text
Mission 检索 → KnowledgeGateway.retrieve()       src/sortie_deck/knowledge_gateway.py:308
  ├─ 任务命名空间：本地 catalog 关键词匹配 ini:<id>，+10 分置顶（不走重排）
  └─ 全局池（hybrid 模式）→ POST /api/v1/knowledge-bases/{id}/hybrid-search
       WeKnora 内部（internal/application/service/…）：
       向量检索 + BM25 → 加权 RRF 融合(0.7/0.3, k=60)
       → 【重排】HTTP → host.docker.internal:8012/rerank → bge 打分
       → 复合评分（0.6*模型分 + 0.3*检索分 + 0.1*来源权重）
       → 阈值过滤（默认 0.2，全拒时降级重试）→ MMR 去重 → TopK
  → RetrieveHit 列表进入 Agent 上下文
```

关键结论（2026-10-20 实测修正）：
- **`/hybrid-search` 仅当请求体显式携带 `rerank` 对象时才走重排路径**（WeKnora handler `knowledgebase.go: req.Rerank != nil` 分支），裸请求走原始召回。
- 因此 Sortie Deck 的 `WeKnoraClient.hybrid_search()`（`src/sortie_deck/weknora.py:156`）已加 `"rerank": {"enabled": True}`——租户未配 rerank 模型时 WeKnora 自动降级为 RRF 检索序，无条件开启是安全的。
- knowledge-chat（graph/agent 管线）本次实测**未调用** rerank，其重排开关在对话配置侧，后续按需排查；Sortie Deck 默认 hybrid 模式不受影响。

### 职责划分：WeKnora 编排 vs 本地模型服务

常见疑问："rerank 不是 WeKnora 后端直接提供吗？为什么还有个 8012 服务？"

关键是分清两件事：

- **WeKnora 后端提供的是重排的「编排逻辑」**：什么时候调、怎么调、分数怎么融合（0.6/0.3/0.1 复合评分）、失败怎么降级。但它**不带任何 rerank 模型**——就像它也不带 embedding 模型一样（向量也是调本机 Ollama 的 `bge-m3` 算的）。
- **模型本身必须有"模型运行时"承载**，两种方案：

```text
方案 A（云端托管）：
  WeKnora 后端 ──HTTP──→ SiliconFlow 的 bge-reranker API（免费额度，但数据出网）

方案 B（本地自建，本项目所选）：
  WeKnora 后端 ──HTTP──→ host.docker.internal:8012 ──→ bge-reranker-v2-m3
                          （rerank_server.py：模型加载进内存常驻，暴露 HTTP 接口）
```

8012 服务是**极薄的模型服务壳**，不懂 RAG、不懂检索——纯粹"给 query + 一批文档，返回相关性分数"：

```text
POST /rerank  {"query": "...", "documents": ["...", "..."]}
  → 模型对每对 (query, document) 打相关性分（0~1）
  → 返回 {"results": [{"index": 0, "relevance_score": 0.82}, ...]}
```

一次检索中各步骤的归属：

```text
1. Sortie Deck ──→ WeKnora: POST /hybrid-search
2. WeKnora 自己完成：向量检索 + BM25 + RRF 融合        ← WeKnora 直接提供
3. WeKnora ──→ 8012: 候选发给本地 rerank 打分           ← 模型能力来自这里
4. 8012 ──→ WeKnora: 返回每条的相关性分数
5. WeKnora 自己完成：复合评分 + 阈值 + MMR → TopK       ← WeKnora 直接提供
6. WeKnora ──→ Sortie Deck: 返回最终排序结果
```

**一句话总结**：检索、融合、编排都是 WeKnora 后端直接做的；只有"打分"这一步，WeKnora 按配置的 BaseURL 去 HTTP 调模型——本地方案里是 8012，若配 SiliconFlow 则是云端 API，其余流程一模一样。

## 六、实测数据（Apple M3 / 16GB）

| 指标 | 数值 |
|---|---|
| 模型加载（缓存命中） | 3.6s |
| 服务进程 RSS | 0.24GB（推理峰值 2.25GB） |
| 热请求延迟 | 4 文档 147ms（首次请求含 Metal 编译冷启动约 5s） |
| 6 候选离线基准 | 230ms/次；50 候选 814ms |
| 排序质量 | 相关文档 0.82+，无关 ≈0，排序正确 |

## 七、运维

```bash
# 启动 / 重启
cd /Library/git-root/weknora/local-rerank-service && python3 rerank_server.py
# 后台方式
nohup python3 rerank_server.py > /tmp/rerank_server.log 2>&1 &
# 日志
tail -f /tmp/rerank_server.log
# 探活
curl http://127.0.0.1:8012/health
```

- 当前为 `nohup` 前台进程方式，**Mac 重启后需手动启动**；开机自启可后续补 launchd plist（`~/Library/LaunchAgents/com.local.reranker.plist`，待办）。
- 依赖已装：`FlagEmbedding 1.4.2 / torch 2.14.1 / transformers 5.20.2 / fastapi / uvicorn / psutil`（本机 pip 曾因 Python SSL 根证书缺失需 `--trusted-host` 绕过，建议有空跑 `/Applications/Python 3.12/Install Certificates.command` 修复）。

## 八、验证重排是否生效（2026-10-20 已全部通过 ✅）

1. 探活：`curl http://127.0.0.1:8012/health` → `{"ready": true}`。
2. 混合检索（带 `"rerank": {"enabled": true}`）：服务日志出现 `docs=43 top_score=0.9766`，相关 PRD chunk 稳居 Top1-3，无关 chunk 模型分 ≈0。
3. **GLM-5.2 生成回答**：knowledge-chat SSE 管线完整走通（query_understand → 11 个 references → thinking → answer → complete），回答带 `<kb doc=... chunk_id=.../>` 引用标记，内容与 PRD 一致。
4. Sortie Deck 侧：`WeKnoraClient.hybrid_search()` 实测返回 9 条，重排后相关文档置顶（`tests/test_weknora_gateway.py` 7 passed）。

已知观测：真实 PRD 长文本下 43 候选重排约 13s（远慢于短文本基准 814ms），因 cross-encoder 对 8192 长上下文逐对打分。如需提速可：调小候选池、或改用 llama.cpp GGUF 量化版（`--pooling rank`）。
