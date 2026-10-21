# 记忆（Memory）

## 1. 概述

记忆库是跨任务、可持续沉淀的 Agent 上下文来源：人类把**偏好 / 教训 / 战例 / 事实**
以一行约定的形式写入，系统在任务生成与执行两个时机按相关性注入 Agent 提示词。

与「任务绑定文件记忆」（滚动总结 / 会话转录，见
[mission-memory.md](mission-memory.md)）是两套体系：前者是**人类主动沉淀、跨任务共享**，
后者是**单任务执行过程中自动产生**。

| 项 | 值 |
|----|-----|
| 代码 | `memory.py`（存储与注入）、`api/app.py` `/api/memory` 系列 |
| 存储 | `data/memory.json`（entries 为唯一数据源） |
| 索引 | `data/memory/MEMORY_INDEX.md`（派生物，可随时重建） |
| 前端 | 记忆库页（仅管理员入口） |
| 配置 | `TDT_MEMORY_BACKEND`（local / mem0） |

## 2. 作用域模型

### 2.1 设计原则（业内共识）

参考 Cursor（User Rules / Project Rules）、Claude Code
（`~/.claude/CLAUDE.md` 层叠）、Mem0（user / agent / run 三元组）、
Letta（core blocks / archival 分层）：

1. 记忆按**主体**挂载：用户 → 项目（任务）→ Agent，团队全局独立一层；
2. 注入时**层叠合并**：全局打底 → 用户层 → 任务 / Agent 层；
3. 权限分层：团队全局仅管理员可写，个人层本人可写。

### 2.2 作用域定义

| 作用域 | 界面名称 | 生效范围 | 写入权限 | scope_id |
|--------|----------|----------|----------|----------|
| workspace | 全局约定 | 所有任务的所有 stage | 仅 admin | `global` |
| user | 我的 · 所有任务 | 本人创建的全部任务 | 本人 / admin | 创建者 user id |
| mission | 我的 · 指定任务 | 单个任务 | 本人 / admin | `ini_` 任务 id |
| agent | 我的 · 指定 Agent | 某个已发布角色的 stage | 本人 / admin | `agt_` 角色 id |

> 「全局约定」不区分任务，对全员、全部任务生效；任务级与 Agent 级属于
> 「我的」下的定向共享，由写入者选择目标。

### 2.3 记忆类型

`preference`（偏好）/ `lesson`（教训）/ `episode`（战例）/ `fact`（事实）。
带 `stage:<名称>` 标签的条目只注入对应 stage（子房间结论即此机制）。

## 3. 数据模型与记忆索引

```
MemoryEntry
├── id            mem_ 前缀
├── scope / scope_id
├── kind          preference | lesson | episode | fact
├── content       完整内容（一行约定；未来可升级为长文档）
├── summary       一句话简介（≤30字，写入时 LLM 生成，失败降级为 content 截断）
├── tags          标签（stage:<名称> 有语义）
├── author_id / author_name
└── created_at / updated_at
```

**记忆索引**：`data/memory/MEMORY_INDEX.md`，每条记忆一行的简介清单
（id + 作用域 + 类型 + summary）。每次写入 / 删除后由后端从 entries
重新拼装生成，**纯文件操作不调 LLM**；索引是派生物，不作为数据源。

> 现状标注：`summary` 字段与索引文件已实现；`context_block` 已支持
> brief 相关性打分 + 新近度衰减排序。`memory_search` / `memory_read`
> 内置工具为**已确认待实现**项（阶段二）。

## 4. 注入架构（双触发）

### 4.1 触发时机一：任务生成时（Plan-time）

- 以任务 brief + title 对 workspace + user 域记忆**检索打分**，取 top-K
  （≤8 条，简介行合计 ≤1.5KB）；
- 仅注入**简介行**（id + scope + kind + summary），不注全文；
- 绑定结果写入 `ini.meta["memory_ids"]`，任务详情页「记忆」面板可见。

### 4.2 触发时机二：任务执行时（Run-time）

- Agent 可调用 `memory_search(query)` 工具：返回匹配记忆的索引行，覆盖
  全部可见域（全局约定 / 本人 / 本任务 / 本角色）；
- 需要某条记忆时 `memory_read(id)` 加载全文；
- **护栏**：每 stage 加载次数与单条全文长度有上限，防止上下文堆积；
- 实际读取记录写入任务 meta，任务详情页展示「执行中读取的记忆」。

### 4.3 注入点（已有实现）

| 位置 | 行为 |
|------|------|
| `orchestrator.py` `create_initiative` | 生成 memory_brief 存入 `ini.meta` |
| `graph.py` stage 节点 | 经 `mission_memory_brief` 重算注入 `StageContext.memory_brief` |
| `harness_providers/executor.py` | `Layered memory brief` 段拼入 stage prompt |
| 房间讨论 `orchestrator.py` | 讨论回复注入发言者 user 域记忆 |

> 现状标注：当前 `context_block` 为「检索打分（关键词 + 新近度）排序 +
> 6000 字符硬截断」；4.1 的检索绑定已实现（Plan-time 绑定写
> `ini.meta["memory_ids"]`），4.2 的工具检索为**已确认待实现**项
> （`memory_search` / `memory_read` 工具 + 前端记忆面板）。

## 5. 使用方式（记忆库页）

入口：顶部导航「记忆」，**仅管理员可见**。

- **浏览**：卡片网格（方案三设计），类型色条 + 衬线引语排版；
  支持范围分段（全部 / 全局约定 / 我的）、类型筛选、关键词搜索；
- **写入**：「＋ 新建记忆」弹窗 → 作用域（全局约定 / 我的）→ 类型 → 内容；
  选「我的」可进一步指定生效范围（所有任务 / 指定任务 / 指定 Agent）；
- **删除**：hover 卡片删除，底部 toast 提供撤销（撤销 = 原内容重新写入）；
- **验收提示**：卡片元信息显示作者、时间、作用域目标，便于核对记忆
  注入到了正确的上下文。

## 6. API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/memory?scope=&scope_id=&q=` | 列表（user 域仅本人可见） |
| POST | `/api/memory` | 写入（workspace 仅 admin；summary 由后端生成） |
| PUT | `/api/memory/{id}` | 修改（权限同写入） |
| DELETE | `/api/memory/{id}` | 删除（workspace 仅 admin；user 域仅本人 / admin） |

## 7. 共享后端

`TDT_MEMORY_BACKEND=mem0` 时本地存储包装为 `CompositeMemory`：mission
范围写入镜像到 mem0（`TDT_MEM0_BASE_URL` / `TDT_MEM0_API_KEY` /
`TDT_MEM0_USER_PREFIX`），检索命中并入上下文块，按
`user_id = "<prefix>:<initiative_id>"` 任务级隔离。远端失败静默降级本地。

## 8. 实施计划

| 阶段 | 内容 | 状态 |
|------|------|------|
| 已完成 | 作用域模型（全局约定 / 我的三种目标）+ 权限校验 | ✅ |
| 已完成 | 记忆库看板 UI（卡片网格 / 弹窗写入 / 撤销删除 / 仅管理员） | ✅ |
| 已完成 | `summary` 字段（写入时 LLM 生成 + 截断降级）与索引文件重建 | ✅ |
| 已完成 | `context_block` 检索打分（brief 相关性排序）+ 绑定 ids 写入任务 meta | ✅ |
| 已完成 | 记忆治理阶段一：角色分层摘要 / 反思沉淀（程序性记忆）/ 新近度衰减 / 冲突检测失效（mem0 决策回读 + 本地规则兜底）/ 并行分支仲裁 | ✅ 2026-10-21 |
| 已完成 | 条目记忆检索切换 WeKnora（混合检索 + rerank + graph，`TDT_MEMORY_RETRIEVAL=weknora`，镜像回映射 + 本地可见性过滤 + 三级降级） | ✅ 2026-10-21 |
| 已完成 | `memory_search` / `memory_read` 内置元工具（in-loop 声明-兑现，预算 + 隐私护栏，读取记 usage.log） | ✅ 2026-10-21 |
| 已完成 | Plan-time 绑定并入 mem0 命中（`CompositeMemory.context_candidates` 内容互含回映射前置） | ✅ 2026-10-21 |
| 已完成 | 质量评估脚本 `scripts/memory_eval.py`（local 基线命中率约 38%，weknora 对比待服务联调） | ✅ 2026-10-21 |
| 远期 | 记忆升级为长文档时，索引展开价值最大化；TTL 清理 | ⏳ |

> 治理机制全链路说明见 `docs/记忆体系整体策略.md`（写入/管理/读取时机）。

## 9. 依赖

- Orchestrator（任务创建注入点、任务 meta）
- Auth（user id 与 admin 权限）
- Toolkit（`memory_search` / `memory_read` 工具注册）
- LlmClient（写入时简介生成，需降级兜底）
