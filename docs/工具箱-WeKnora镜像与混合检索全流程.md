# 工具箱 · WeKnora 镜像与混合检索全流程

> 适用范围：`dev` 分支，`toolkit.py` / `toolkit_index.py` / `loadout.py` / `weknora.py` / `graph.py` / `orchestrator.py`
> 记录时间：2026-10-26（本文件描述的是**当前已生效**的实现与配置，不是规划）
> 关联文档：`docs/工具箱-tool-skill-mcp-执行流程调研.md`、`docs/plans/2026-10-20-tool-search-meta-tool.md`

---

## 0. 一句话原理

`data/toolkit.json` 是**唯一事实源（source of truth）**，WeKnora 只保存"可检索的镜像副本"：

- **写**：条目落本地后，立刻把 `name/summary/tags/capabilities/正文` 拼成一段文本投喂到 WeKnora 专用知识库；
- **读**：查询 → WeKnora 混合检索（向量 + BM25 → RRF → rerank）→ 从命中的标题里取出 `item_id` → 回查本地条目 → 本地再做启停/类型/白名单过滤。

因此：**WeKnora 不可用不影响工具创建**；检索异常或空命中时自动退回本地打分，本地永远持有全量条目。

| 关注点 | 结论 |
|---|---|
| 本地存储 | `data/toolkit.json`，读写全在 `ToolkitStore` 内加锁 |
| 镜像载体 | WeKnora 知识库 `sortie-toolkit`（与文档库 `sortie-knowledge` 物理隔离） |
| 镜像标识 | 标题锚点 `toolkit/{kind}/{name}#{item_id}` |
| 检索后端 | `TDT_TOOLKIT_RECALL_BACKEND=weknora`（未配置或异常则本地打分） |
| 检索分数 | WeKnora rerank 复合分（0-1），与本地三层加权分同口径 |

---

## 1. 数据模型

```
ToolkitItem (pydantic)                          src/sortie_deck/toolkit.py:30-69
  ├─ kind        tool | mcp | skill
  ├─ name/summary/category/tags/capabilities    ← 参与镜像与检索
  ├─ runtime/endpoint（tool）
  ├─ transport/command/url/args/env（mcp）      ← env 永不进镜像
  ├─ body/scripts（skill）                      ← scripts 源码永不进镜像
  └─ load        always（常驻） | defer（按需检索后上浮）
```

- `load=always` 的条目走"常驻注入"，不依赖检索；`load=defer` 的条目才是检索对象。
- 本地 store 的增删改查：`list` `get` `create` `upsert_by_name` `update` `delete`（`toolkit.py:489-615`）。

---

## 2. 写路径：创建 / 修改 / 删除 / 导入

### 2.1 时序

```
界面「添加工具」抽屉                     apps/web/src/App.tsx:2270-2294
  └─ POST /api/toolkit                  src/sortie_deck/api/app.py:1387-1393
       └─ ToolkitStore.create           src/sortie_deck/toolkit.py:528-561
            ├─ 加锁：读 → append → 写 data/toolkit.json     ← 唯一事实源
            └─ _mirror_sync             src/sortie_deck/toolkit.py:464-487
                 ├─ backend != weknora → 直接 return（不镜像）
                 └─ mirror_upsert       src/sortie_deck/toolkit_index.py:122-136
                      ├─ 按 title 找到旧镜像 → DELETE（WeKnora 无更新接口）
                      └─ create_manual(status="publish")     src/sortie_deck/weknora.py:196-222
                           → WeKnora 异步：分块 → 向量化 → 关键词索引
```

### 2.2 四个触发点

| 动作 | 本地方法 | 镜像操作 |
|---|---|---|
| 新建条目 | `create()`（`toolkit.py:560`） | `mirror_upsert` |
| 导入覆盖同名 / 市场安装 | `upsert_by_name()`（`toolkit.py:581`） | `mirror_upsert` |
| 编辑（含启停） | `update()`（`toolkit.py:604`） | `mirror_upsert`（删旧建新） |
| 删除 | `delete()`（`toolkit.py:615`） | `mirror_remove` |

三类条目（tool / mcp / skill）时机完全一致，因为镜像挂在**存储层**而非某个业务分支。

### 2.3 三个必须知道的例外

1. **种子条目首启不镜像**：`ToolkitStore` 首次初始化直接 `_write(_SEED)`，不经过 `_mirror_sync`（`toolkit.py:431-453`），要靠全量重建补。
2. **镜像失败不阻塞创建**：`_mirror_sync` 内部吞掉异常只打 `warning`（`toolkit.py:482-487`），所以界面创建成功 ≠ 镜像成功，排障要看后端日志。
3. **同步调用有超时**：写镜像超时 10s（`_MIRROR_TIMEOUT`，`toolkit_index.py:34`），超时即失败，条目仍成功。

### 2.4 全量重建

```
POST /api/toolkit/reindex（admin）        src/sortie_deck/api/app.py:1438-1449
  └─ toolkit_index.sync_all               src/sortie_deck/toolkit_index.py:154-179
       ├─ 库内 title 前缀 toolkit/ 的条目 ⇒ {title: id}
       ├─ 本地每一条：有旧镜像先删再建（updated），无则建（created）
       └─ 库里剩的（本地已删）→ 删除（removed）
```

以本地为准"多退少补"，是镜像漂移的唯一官方修复手段。

---

## 3. 镜像原理

### 3.1 为什么需要镜像

WeKnora 是文档/chunk 级检索服务，无法直接查询结构化 JSON。镜像把条目转成一段**可被向量与 BM25 同时命中**的文本：

- 命中后靠标题里的 `#item_id` 回映射本地条目；
- 权限、启停、白名单、类型过滤**全部在本地做**，WeKnora 里残留或过期的镜像不会造成越权。

### 3.2 标题锚点

```python
# src/sortie_deck/toolkit_index.py:37-39
return f"{TITLE_PREFIX}/{item.kind}/{item.name}#{item.id}"
```

形如 `toolkit/tool/git-diff#tk_1ff00af93648`。`#` 左侧只是给人看的（kind + name），右侧才是机器用的回查 key。

### 3.3 正文构造

`item_content`（`toolkit_index.py:42-70`）拼装顺序：

```
# {kind}/{name}
{summary}

分类: {category}
标签: {tags...}
能力: {capabilities...}          ← 能力标签是召回命中率最高的一层
端点: {endpoint}                 ← 仅 tool
连接: {url or command}           ← 仅 mcp
脚本: {script paths}             ← 仅 skill

{body}                           ← ≤4000 字，超出截断并标注
```

**安全红线**：`env`（可能含 PG 密码、token）与 skill 脚本源码**永不写入镜像**（`toolkit_index.py:42-46` 注释与 `tests/test_toolkit_index.py:67-79` 用例双重保证）。

### 3.4 更新 = 删旧建新

WeKnora 没有更新 knowledge 的接口，所以每次 upsert 都是"按 title 找到旧的 → 删除 → 新建"。代价是重建索引（异步解析），收益是本地逻辑简单、不会出现新旧两份并存。

### 3.5 状态与可检索性

| 状态 | 含义 | 能否被检索 |
|---|---|---|
| `draft` | 手工知识默认状态，**不进入解析流水线** | 不能（这就是必须先修的那个坑） |
| `pending` / `processing` / `finalizing` | 分块 / 向量化 / 收尾中 | 部分可见，不稳定 |
| `completed` | 索引完成 | 能 |
| `enable_status=enabled` | 条目启用 | 能（停用则本地也会过滤） |

> **关键坑（已修）**：`create_manual` 必须显式传 `status="publish"`，否则条目永久停在 `draft`，`hybrid-search` 一条都召不回。见 `weknora.py:196-222`。

---

## 4. 读路径：四个入口，一个收敛点

```
① 手动预绑定（agent / role / stage 的 toolkit_ids）
      → 不走召回，直接压成常驻清单          orchestrator.py:660-683, 696-736
② 任务创建期 JIT 装载（TDT_LOADOUT_AUTO，默认开）
      → build_loadout(capability needs)     orchestrator.py:738-814
      → 候选唯一直选 / 多候选交规划器 LLM 挑选 → probe 探活
③ 阶段入口主动召回（TDT_LOADOUT_STAGE_RECALL，默认开）
      → run_stage_recall 多路短查询          graph.py:507-576
      → 合并取最高分 → 阈值 → 去重 → 探活 → 注入
④ 推理循环内元工具（TDT_TOOLKIT_SEARCH，默认开）
      → 模型在 StageResult.meta 声明意图      graph.py:143-253
      → toolkit_search：召回候选摘要回填
      → toolkit_pick：五道校验后解冻正文
      → skill_run：须先 pick，脚本沙箱执行
                          ↓
                 loadout.recall()             loadout.py:96-150
                   ├─ mirror_available() 为真 → weknora_recall   toolkit_index.py:182-225
                   │     hybrid_search → chunk 去重 → title 解析 item_id
                   │     → store.get 回查 → enabled / kinds / 白名单过滤 → 复合分
                   └─ 未配置 / 异常 / 空命中 → 本地 _score        loadout.py:78-93
                         能力标签 1.0 / tags 0.6 / 正文 0.25，按查询词元数归一
```

### 4.1 各入口的职责边界

| 入口 | 触发时机 | 结果落点 |
|---|---|---|
| ① 手动绑定 | 任务创建时 | `toolkit_briefs[role:*]` / `toolkit_briefs[stage:*]` → 拼进 persona |
| ② JIT | 任务创建时（`loadout_auto`） | 并入 `stage_toolkits` 与 `toolkit_briefs`，落 `ini.meta.loadout_plans` 审计 |
| ③ 阶段召回 | 每个阶段节点执行前 | mcp → `stage_toolkits`；skill → `toolkit_floats`；tool → `toolkit_ids`（`graph.py:564-569`） |
| ④ 元工具 | 阶段执行中模型主动发起 | brief 增量 + 事件流 + `toolkit_pick_budget` 预算 |

### 4.2 阶段召回为什么拆多路短查询

`_score` 按查询词元数归一（分数 = 命中权重 / 词元数），长查询会把每条得分稀释到 ~0.08，被阈值全部卡死（`graph.py:518-524`）。因此拆成聚焦短查询（角色+阶段 / 阶段标签 / brief 摘要各一路），多路命中的同一条目取最高分。

### 4.3 元工具的预算与校验

| 项 | 值 | 位置 |
|---|---|---|
| 单次搜索候选数 | `SEARCH_TOP_K = 5` | `loadout.py:156` |
| 单阶段 pick 次数 | `PICK_BUDGET_PER_STAGE = 8` | `loadout.py:157` |
| skill 正文上浮上限 | `SKILL_BODY_FLOAT_LIMIT = 5000` 字 | `loadout.py:162` |
| pick 校验链 | 存在 → 启用 → 白名单 → 预算 → 探活 | `graph.py:155-156, 203-228` |

pick 失败不会抛异常，而是把原因回传给模型，允许换条目或换词重搜（闭环）。

---

## 5. WeKnora 在本项目的四个用途

四者共用一个 `WeKnoraClient`（`weknora.py:47-381`），靠**不同知识库 + 不同标题前缀**做逻辑隔离。

| 用途 | 目标库 | 标识 | 开关 |
|---|---|---|---|
| 企业知识库（文档 RAG） | `sortie-knowledge` | 文档标题 / `kb_mission_index` | `TDT_KB_BACKEND=weknora` |
| 任务产物回写 | `sortie-knowledge` | `index_mission_artifact`（`knowledge_gateway.py:432`） | 同上 |
| **工具箱镜像** | `sortie-toolkit` | 标题前缀 `toolkit/` | `TDT_TOOLKIT_RECALL_BACKEND=weknora` |
| 记忆条目镜像 | 记忆库 | `mem:<id>`（`memory_mirror.py:45`） | `TDT_MEMORY_RETRIEVAL=weknora` |

检索侧入口也分开：

- 文档：`KnowledgeGateway.retrieve`（`knowledge_gateway.py:308-358`），mission 命名空间优先，graph 模式失败降级 hybrid；
- 工具：`toolkit_index.weknora_recall`（`toolkit_index.py:182-225`），固定 `retrieval_mode=hybrid`。

**为什么工具箱单独开库**：`sortie-knowledge` 开启图谱抽取（`graph=true`），工具条目是短文本，混进去只会浪费抽取开销并污染实体图。

---

## 6. 配置项总表

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TDT_TOOLKIT_RECALL_BACKEND` | `local` | `local` / `weknora`；`weknora` 才写镜像、才走混合检索 |
| `TDT_TOOLKIT_WEKNORA_KB_ID` | 空 | 工具箱专用库 id；为空则始终降级 local |
| `TDT_WEKNORA_BASE_URL` | 空 | 复用企业知识库的连接配置 |
| `TDT_WEKNORA_API_KEY` | 空 | 同上 |
| `TDT_LOADOUT_AUTO` | `true` | 任务创建期 JIT 装载（模式 1） |
| `TDT_LOADOUT_STAGE_RECALL` | `true` | 阶段入口主动召回（模式 1.5） |
| `TDT_LOADOUT_RECALL_MIN_SCORE` | `0.2` | 召回最低分阈值（按本地打分口径标定） |
| `TDT_TOOLKIT_SEARCH` | `true` | 推理循环内元工具（模式 2） |

当前本地 `.env` 的生效值：

```35:39:.env
TDT_TOOLKIT_RECALL_BACKEND=weknora
TDT_TOOLKIT_WEKNORA_KB_ID=1174d22c-f792-42f1-ba3c-61ce075e391c
```

> 改动这两项后**必须重启后端**：`settings` 只在进程启动时读取一次。

---

## 7. 失败与降级矩阵

| 场景 | 行为 | 位置 |
|---|---|---|
| WeKnora 不可达 / 超时 / 非 2xx | 捕获异常 → 本地三层打分，打 `warning` | `loadout.py:135-138` |
| KB id 未配置 / backend=local | 不镜像、纯本地打分 | `toolkit_index.py:73-78` |
| 镜像写入失败 | 仅 `warning`，条目创建照常成功 | `toolkit.py:482-487` |
| 镜像刚建、解析未完成 | 库里可见但检索召回不到（`finalizing` → `completed` 约 1-3 分钟） | WeKnora 侧异步 |
| 混合检索命中为空 | **降级本地打分**（镜像延迟时兜底） | `loadout.py:131-134` |
| 条目停用 / 白名单外 | 镜像保留，检索侧本地过滤 | `toolkit_index.py:214-219` |
| mcp 探活失败 | pick / 阶段召回直接丢弃并记日志 | `graph.py:557-563` |
| 条目已删但镜像残留 | 检索时 `store.get` 抛 `KeyError` → 跳过；可用 reindex 清理 | `toolkit_index.py:210-213` |

---

## 8. 排障手册

### 8.1 现象：界面新建工具，WeKnora 里没有

按顺序查：

1. `.env` 是否配了 `TDT_TOOLKIT_RECALL_BACKEND=weknora` 与 `TDT_TOOLKIT_WEKNORA_KB_ID`；
2. 后端是否**重启过**（settings 有缓存）；
3. 后端日志是否有 `toolkit mirror sync failed`（镜像失败只 warning，界面不会提示）；
4. 库里是否有该 title：`toolkit/{kind}/{name}#{item_id}`。

### 8.2 现象：镜像在库里，但检索召回不到

1. 查 `parse_status`：`draft` 说明写入时没传 `status=publish`（已修，`weknora.py:196-222`）；
2. `pending/processing/finalizing` 说明还在解析，等 1-3 分钟；
3. `enable_status` 是否为 `enabled`。

### 8.3 现象：同一工具在结果里出现两次

`hybrid-search` 是 **chunk 级命中**，一条知识可能命中多个 chunk。已按 `item_id` 去重并保留最高分（`toolkit_index.py:202-220`）。若仍重复，检查是否库里存在两条同 `#item_id` 的镜像（历史残留），用 reindex 清理。

### 8.4 现象：无关查询也能召回一堆工具

WeKnora 的 rerank 复合分**不等于相关性概率**，无关查询也会给 0.4~0.85：

| 查询 | 实测返回 |
|---|---|
| `查看工作区 diff` | git-diff 0.995 / 0.989 |
| `做一顿红烧肉` | slowlog-triage 0.847、api-tester 0.712、find-skill 0.700 |
| `今天天气怎么样` | find-skill 0.461、ui-v4-skill 0.447 |

因此 `LOADOUT_RECALL_MIN_SCORE=0.2` 在 WeKnora 路径下几乎不生效（该阈值是按本地 0.25/0.6/1.0 口径标的）。**尚未修复**，见第 9 节。

### 8.5 现象：镜像条数比本地多

通常是"条目删除发生在 backend 仍为 local 的进程里"，`mirror_remove` 没执行 → 产生孤儿镜像。处置：

```bash
# 触发全量校正（以本地为准，多退少补）
curl -X POST http://127.0.0.1:8787/api/toolkit/reindex -H "Authorization: Bearer <admin-token>"
```

### 8.6 现象：跑完单测后知识库里多出 `git-diff` / `slowlog-triage`

测试污染真实 KB。已在两处设防：

- `tests/conftest.py:15-17` 强制 `TDT_KB_BACKEND=local` + `TDT_TOOLKIT_RECALL_BACKEND=local`；
- `tests/test_toolkit_index.py:53-59` 的 `weknora_on` 夹具默认注入空 `FakeClient`，用例需要真实命中时自行覆盖。

---

## 9. 已知问题

| 问题 | 影响 | 状态 |
|---|---|---|
| WeKnora rerank 复合分与相关性不成比例 | 无关查询仍能通过 0.2 阈值，占用 top-5 候选 | **待定**：拟加 `TDT_TOOLKIT_WEKNORA_MIN_SCORE`（0.8~0.85）或相对阈值（top1 的 85%） |
| 镜像写入是请求路径内同步调用 | 建库慢时创建接口最多被拖 10s | 可接受；后续可改后台任务 |
| 镜像 title 含 `name`，重命名会产生新旧两条 | 旧 title 由删旧建新兜住，理论无残留 | 已覆盖 |

---

## 10. 验证命令

```bash
# 1) 配置是否生效（新进程读取 .env，不改动运行中的服务）
.venv/bin/python -c "
from sortie_deck.settings import settings
from sortie_deck.toolkit_index import mirror_available
print(settings.toolkit_recall_backend, settings.toolkit_weknora_kb_id, mirror_available())
"

# 2) 本地条目 vs 镜像条数
.venv/bin/python -c "
from sortie_deck.settings import settings
from sortie_deck.toolkit import ToolkitStore
from sortie_deck.toolkit_index import build_client, _kb_id
print('本地', len(ToolkitStore(settings.toolkit_db).list()))
rows = build_client().list_knowledge(_kb_id())
print('镜像', len(rows), {r.parse_status for r in rows})
"

# 3) 召回效果（混合检索 + rerank）
.venv/bin/python -c "
from sortie_deck.settings import settings
from sortie_deck.toolkit import ToolkitStore
from sortie_deck.toolkit_index import weknora_recall
store = ToolkitStore(settings.toolkit_db)
print([(i.name, round(s,3)) for i,s in weknora_recall(store, '查看工作区 diff', limit=3)])
"

# 4) 全量重建
curl -X POST http://127.0.0.1:8787/api/toolkit/reindex -H "Authorization: Bearer <admin-token>"
```

---

## 11. 代码索引

| 主题 | 位置 |
|---|---|
| 条目模型与存储 | `src/sortie_deck/toolkit.py:30-69`、`489-615` |
| 镜像同步钩子 | `src/sortie_deck/toolkit.py:464-487` |
| 镜像构造 / upsert / remove / rebuild | `src/sortie_deck/toolkit_index.py:37-179` |
| 混合检索召回与回映射 | `src/sortie_deck/toolkit_index.py:182-225` |
| 本地三层打分 | `src/sortie_deck/loadout.py:78-93` |
| 召回收敛与降级 | `src/sortie_deck/loadout.py:96-150` |
| 候选摘要 / 解冻 / pick 校验 | `src/sortie_deck/loadout.py:166-230` |
| WeKnora HTTP 客户端 | `src/sortie_deck/weknora.py:47-381` |
| hybrid-search / create_manual | `src/sortie_deck/weknora.py:156-222` |
| 阶段入口主动召回 | `src/sortie_deck/graph.py:507-576` |
| in-loop 元工具 | `src/sortie_deck/graph.py:143-253` |
| 阶段 brief 注入 | `src/sortie_deck/graph.py:1193-1211` |
| 任务创建期 JIT | `src/sortie_deck/orchestrator.py:738-814` |
| 文档 RAG 检索 | `src/sortie_deck/knowledge_gateway.py:308-358` |
| 管理端点（reindex） | `src/sortie_deck/api/app.py:1438-1449` |
| 配置项 | `src/sortie_deck/settings.py:121-145`、`187-198` |

---

## 12. 2026-10-26 本次变更

| 文件 | 改动 | 原因 |
|---|---|---|
| `.env:35-39` | 开启 `TDT_TOOLKIT_RECALL_BACKEND=weknora` 并指向 `sortie-toolkit` | 镜像与混合检索此前完全未启用 |
| `src/sortie_deck/weknora.py:196-222` | `create_manual` 增加 `status="publish"` | 默认 `draft` 不入解析流水线，镜像永远召回不到 |
| `src/sortie_deck/toolkit_index.py:202-220` | 命中按 `item_id` 去重 | chunk 级命中导致同一工具重复占位 |
| `src/sortie_deck/loadout.py:111-134` | 空命中降级本地打分 | 镜像解析延迟时召回归零 |
| `tests/conftest.py:15-17` | 单测强制本地后端 | 避免测试打真实 KB |
| `tests/test_toolkit_index.py:53-59, 196-207` | 夹具注入空替身 + 空命中兜底用例 | 测试隔离与回归覆盖 |
