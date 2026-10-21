# 技术方案：记忆检索切换为 WeKnora 混合检索 + rerank + graph

> 日期：2026-10-21
> 状态：已实施（33 个记忆/harness 测试全绿；启用方式：`TDT_MEMORY_RETRIEVAL=weknora` + WeKnora 连接配置，存量条目执行 `scripts/rebuild_memory_mirror.py` 回填）
> 需求：记忆检索不再用「候选池逐条关键词重合度打分」，改用 WeKnora 的混合检索 + rerank + graph 模式。

---

## 一、需求背景与现状

现状：`memory.py` 的 `context_candidates()` 把四层作用域候选池逐条做关键词重合度打分（`_score_entry` + `_recency_bonus`），无语义检索能力。

现有 WeKnora 集成（`knowledge_gateway.py` / `weknora.py`）已具备：

| 能力 | 代码 | 说明 |
|---|---|---|
| 混合检索 + rerank | `WeKnoraClient.hybrid_search`（`weknora.py:156`） | 向量 + BM25，请求显式带 `rerank:{enabled:true}`；租户未配 rerank 模型时 WeKnora 自动降级 RRF |
| Graph 检索 | `WeKnoraClient.knowledge_chat_references`（`weknora.py:272`） | GraphRAG：查询实体识别 → 图谱一跳扩展 → 关联 chunk；`TDT_WEKNORA_RETRIEVAL_MODE=graph` 时启用，失败降级 hybrid |
| 任务命名空间隔离 | `knowledge_gateway.retrieve` | mission 命名空间优先 + 全局池 |

业界对齐：Mem0（语义 + BM25 + 实体三路）、Zep（embedding + BM25 + 图遍历 RRF 融合）——WeKnora hybrid+rerank+graph 与这两条路线同构。

## 二、设计

### 2.1 核心思路：镜像 + 回映射，本地 Store 仍是数据源

WeKnora 是文档/chunk 级检索，记忆条目是结构化数据。采用项目已验证的 `kb_mission_index` 模式：

```
写入侧：记忆条目 create/update/delete → best-effort 镜像到 WeKnora（folder=memory/，title 带 mem_<id> 标识）
读取侧：query → WeKnora hybrid_search / graph → hit.title 解析出 mem_<id> → 回映射本地 MemoryEntry
        → 本地过滤（作用域 / 治理态 / stage 标签 / user 隐私）→ 截断 limit
```

**关键原则**：WeKnora 只负责「召回 + 语义排序」；作用域、治理态（superseded 过滤）、stage 隔离、user 隐私这些**结构化语义在本地过滤**——WeKnora 不理解这些业务语义，召回多取一些（limit*3），本地过滤后截断。

### 2.2 检索流程（新 `MemoryStore.context_candidates` 内部路径）

```
TDT_MEMORY_RETRIEVAL=weknora 且 WeKnora 可用：
  1. query（title+brief 或 stage 上下文）→ WeKnora 记忆 KB
  2. retrieval_mode=graph 时走 knowledge_chat_references（图谱实体扩展），失败降级 hybrid_search
  3. hits → 解析 mem_<id> → 批量 get 本地条目
  4. 本地过滤：scope 匹配 + status==active + stage_applies + user 隐私
  5. 不足 limit 时用本地打分（现有关键词+衰减）补足
  6. mission 桶仍走 merge_stage_conclusions 分支仲裁（结构化逻辑不变）
TDT_MEMORY_RETRIEVAL=local（默认）或 WeKnora 不可用：
  现有路径不变（关键词打分 + 衰减），零回归
```

### 2.3 镜像同步策略（best-effort，静默降级）

- **创建/更新**：条目落本地后异步镜像 `create_manual(title=f"mem:{id}", content=正文)` → `move_to_folder("memory/")`；
- **删除/失效**：本地 delete 或标记 superseded 时尝试删除远端文档（失败忽略——远端残留条目在读取侧会被本地过滤兜住）；
- **批量回填**：新增 `rebuild_memory_index()` 类方法（或 CLI 命令），把存量 active 条目一次性灌入 WeKnora；
- 记忆写入路径保持「绝不阻塞」：镜像失败只打 warning 日志。

### 2.4 作用域在 WeKnora 侧的组织

单一记忆 KB + folder 划分（便于按作用域定向检索，也支持跨域联合查询）：

```
memory/workspace/     全局约定
memory/user/<uid>/    个人
memory/mission/<id>/  任务
memory/agent/<id>/    角色
```

读取侧回映射后按本地条目的 scope 字段做最终过滤（folder 只是召回辅助，不作为权限依据）。

## 三、改动清单

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/settings.py` | 新增 `memory_retrieval: str = "local"`（local\|weknora）、`weknora_memory_kb_id: str \| None = None`（缺省回落主 `weknora_kb_id`） |
| `src/sortie_deck/memory.py` | `context_candidates` 增加 WeKnora 路径与本地兜底补足；新增 `MemoryMirror`（镜像写：create/update/delete 钩子） |
| `src/sortie_deck/weknora.py` | 无改动（hybrid_search / graph 能力已有） |
| `src/sortie_deck/api/app.py` | 记忆 create/update/delete 后触发镜像（best-effort） |
| `src/sortie_deck/cli.py`（或 scripts/） | 存量条目回填命令 |
| `tests/test_memory_governance.py` | WeKnora 路径 mock 测试（hit→回映射→过滤→降级） |
| `docs/记忆体系整体策略.md` | R1/R2 读取时机更新；配置总表补充 |

## 四、备选方案

- **方案 B：不镜像，检索时全量把 active 条目喂给 WeKnora**——免同步但每次检索都是写入+检索两跳，延迟和成本高，否；
- **方案 C：彻底删掉本地打分**——WeKnora 不可用时记忆检索瘫痪，违背「记忆是增强不是依赖」原则，否（保留为兜底）。

## 五、风险

- WeKnora 侧解析延迟：`create_manual` 后立即检索可能召回不到最新条目（解析异步）——本地打分补足路径兜住新增条目；
- hit→条目回映射依赖 title 约定 `mem:<id>`——镜像时 title 即带，稳定；
- graph 模式依赖租户图谱能力配置——失败自动降级 hybrid，再降级本地打分，三级兜底。
