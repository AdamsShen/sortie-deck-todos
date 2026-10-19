# 知识库

## 1. 概述

企业级知识库采用腾讯开源 **[WeKnora](https://github.com/Tencent/WeKnora)**。  
Sortie Deck **只做导入与目录管理**；**检索依赖 WeKnora 自身**（hybrid-search），不在本仓库重做向量/RAG。

编码工具（Cursor）通过 Skill 拉取单次任务相关片段并写入 `.cursor/rules`。

| 项 | 值 |
|----|-----|
| 代码 | `knowledge.py`（本地目录镜像）、`weknora.py`、`knowledge_gateway.py`、`knowledge_pack.py` |
| 本地目录 | `data/knowledge.json`（catalog，非全文检索引擎） |
| 后端 | `TDT_KB_BACKEND=local\|weknora` |
| 检索模式 | `TDT_WEKNORA_RETRIEVAL_MODE=hybrid\|graph`（见 §2.1） |
| API | `/api/knowledge` · `/status` · `/folders` · `/import` · `/sync` · `/retrieve` |
| CLI | `tdt knowledge {status,sync,import,pack}` |
| Skill | `packages/skills/knowledge-for-cursor` |

## 2.1 检索模式（hybrid vs graph）

WeKnora 的混合检索（`hybrid-search`）与 GraphRAG 是两条链路：

| | `hybrid`（默认） | `graph` |
|---|---|---|
| 接口 | `POST /knowledge-bases/{kb}/hybrid-search` | `POST /sessions` + `POST /knowledge-chat/{sid}`（SSE） |
| 召回 | BM25 + 向量双路 | 混合检索 + 查询实体抽取 → Neo4j 图谱一跳扩展 → 关联 chunk 补充召回 |
| 延迟 | ~0.3s | ~10-40s（管线内含 LLM 调用；拿到 references 即断开） |
| 依赖 | 无 | WeKnora 服务端 `NEO4J_ENABLE=true` + 知识库 `GraphEnabled` + `ExtractConfig.Enabled` |
| 失败行为 | — | 自动降级到 hybrid-search（`_retrieve_graph` 返回空即回退） |

配置（`settings.py`）：`weknora_retrieval_mode`、`weknora_chat_timeout`（SSE 流超时，默认 90s）。
`/api/knowledge/status` 返回 `retrieval_mode` 与 `graph_enabled`（读知识库 `capabilities.graph`）。
实现：`WeKnoraClient.knowledge_chat_references()`（解析 SSE references 事件转 `WeKnoraHit`）、
`KnowledgeGateway._retrieve_graph()`（会话按次创建/销毁，失败告警降级）。

部署与运维细节（模型配置、SSRF 白名单、系统代理坑、图谱抽取调优）见
`docs/zh/notes/weknora-graphrag-plan.md`。

## 2. 职责边界

| Sortie Deck | WeKnora |
|-------|---------|
| 导入：Markdown / URL / 文件 | 解析、切分、嵌入、索引 |
| 目录：catalog 列表、folder 镜像、`sync` | 混合检索 hybrid-search |
| 任务 pack → Cursor | FAQ / Wiki / Agent 问答（可选直连 WeKnora） |

## 3. 配置

```bash
TDT_KB_BACKEND=weknora
TDT_WEKNORA_BASE_URL=http://127.0.0.1:8080
TDT_WEKNORA_API_KEY=...
TDT_WEKNORA_KB_ID=<knowledge_base_id>
```

未配置 WeKnora 时回退本地 catalog 关键词（仅开发）。

## 4. 模型

`KnowledgeDoc`：title / summary / tags / category / `folder_path` / `remote_id` / `backend`。  
正文可空（存 WeKnora）；Sortie Deck catalog 只留元数据。

## 5. API

| 方法 | 路径 | 行为 |
|------|------|------|
| GET | `/api/knowledge` | 本地 catalog |
| GET | `/api/knowledge/status` | backend + WeKnora 探活 |
| GET | `/api/knowledge/folders` | 目录树（代理 WeKnora） |
| POST | `/api/knowledge/import` | 导入 file/url/body → WeKnora + catalog |
| POST | `/api/knowledge/sync` | 从 WeKnora 刷新 catalog |
| POST | `/api/knowledge/retrieve` | `{query,limit}` → WeKnora hybrid-search |

## 6. Cursor pack

`load_pack_for_cursor(..., gateway=)` → 检索结果写入：

- `.cursor/rules/sortie-knowledge.mdc`
- `.sortie-deck/knowledge-pack.md`

`cursor_cli` 出击前自动 preload。

## 6.1 任务命名空间（每需求隔离）

每个需求拥有独立的 RAG 命名空间：本任务的知识优先命中，且不污染全局库。

- **回写**：阶段完成后，orchestrator 将关键产物（`prd.md` / `tech_design.md` /
  `design.md` / `handoff.md` / `integrate.md`）经
  `KnowledgeGateway.index_mission_artifact(...)` 写入任务命名空间 ——
  best-effort、按 `(stage, kind)` 幂等，进度记录在 `Initiative.meta["kb_indexed"]`。
- **local**：catalog 条目打 `ini:<initiative_id>` 标签。
- **WeKnora**：手动文档移入 `<prefix>/<initiative_id>` 目录
  （`TDT_KB_MISSION_FOLDER_PREFIX`，默认 `missions`）；目录移动失败仅影响展示、
  不阻塞。
- **检索**：`retrieve(query, initiative_id=...)` 先查任务命名空间（加权置顶），
  不足时从全局池补充（WeKnora hybrid-search 或本地关键词排序）。harness 提示
  （`_rag_brief`）始终携带任务 id。
- **开关**：`TDT_KB_MISSION_INDEX=true`（默认开启）控制回写；
  `TDT_KB_MISSION_INDEX_MAX_CHARS` 限制单产物正文长度。

Skill / MCP / 工具**不做**任务隔离 —— 来自全局工具库、按阶段 JIT 加载（见
`toolkit.md`）。

## 7. 开发计划

### P0

| 任务 | 验收 |
|------|------|
| WeKnora client + gateway | status / import / sync / retrieve |
| Cursor pack 走 retrieve | `tdt knowledge pack` |
| 文档 | 本文 |

### P1

| 任务 | 验收 |
|------|------|
| Web 导入 / 同步按钮 | 工作台操作 |
| 文件夹移动 UI | 目录管理 |

### P2

| 任务 | 验收 |
|------|------|
| 多 KB / 租户 | 配置多 `kb_id` |
| Claude Code 同构 pack | 完成 |
