# 方案：toolkit_search 元工具（模式 2 · 推理循环内按需检索）

- 日期：2026-10-20
- 状态：草案，待确认后实施
- 关联：`docs/zh/modules/toolkit.md`（工具箱模块）、`src/sortie_deck/loadout.py`（模式 1 JIT 装载）

## 1. 需求背景

工具箱目前给 Agent 供能有三条路径：

1. **Agent 级静态绑定**：`PublishedAgent.toolkit_ids`，角色工坊 UI 勾选；
2. **槽位/阶段级绑定**：`CreateInitiativeRequest.role_toolkits / stage_toolkits`，运行时按
   `stage_toolkits > RoleAgent.toolkit_ids > meta.role_toolkits` 优先级解析（`orchestrator.py:616-624`）；
3. **模式 1 · JIT 动态装载**（`loadout.py`）：stage 能力需求 → 代码层倒排召回 → 候选唯一直选 /
   多候选交装载规划器 LLM 挑选 → probe 探活 → 挂载进阶段上下文。手动预绑定始终优先。

模式 1 是**预检索**（retrieval as pipeline step）：发生在编排层装配上下文时，one-shot、开环。
它有两个结构性短板：

- **召回失败即静默缺失**：需要的工具没被召回，模型根本不知道它存在，无法自救；
- **检索时机过早**：阶段开始时无法预知执行中途会出现什么长尾需求（例：eng 阶段跑一半发现
  需要查数据库）。

本方案引入**模式 2 · 元工具**（retrieval as agent action）：给执行中的 Agent 暴露一个
`toolkit_search` 元工具，让模型在推理循环内主动搜索工具箱、按需上浮具体条目，与模式 1 互为兜底。

## 2. 业内调研（真实检索结果）

| 来源 | 做法 | 可借鉴点 |
|------|------|----------|
| Claude Code Tool Search（默认开启） | 启动只注入工具**裸名** + server 一句话说明，description/schema 全部 defer（详见 2.1）；`ENABLE_TOOL_SEARCH=auto\|auto:N\|true\|false`；常驻用 `alwaysLoad: true`；描述截断约 2KB、输出上限约 25k token | 元工具 + 分层披露 + 延迟解冻 + 自适应阈值的完整工程化范式 |
| OpenAI Agents JS（[tools 文档](https://openai.github.io/openai-agents-js/guides/tools)） | 工具标 `deferLoading: true`，配 `toolSearchTool()`；`toolNamespace` 把相关工具分组共享一个域描述 | 分组索引（namespace）降低描述噪音 |
| RAG-MCP（[arXiv 2505.03275](https://arxiv.org/abs/2505.03275)） | 任务文本 embedding 检索 top-k 工具注入；200 工具全量注入准确率 13.6% → 检索 top-10 后 43%+ | 量化证明"少而准优于全量"；本项目工具数上百时可升级向量索引 |
| MCP Gateway 模式（[Arcade](https://www.arcade.dev/blog/mcp-gateway-pattern)、[DevToCash](https://devtocash.com/blog/2026-08-17-mcp-gateway-devops-ai-agents)） | 一个入口聚合多 server；per-agent allowlist、统一认证、审计日志、密钥托管；"授权集中在一处" | 执行层治理原则；本项目 `graph.py` 的 `mcp_calls` 集中执行已对齐，可补审计聚合 |

与既有约定（CLAUDE.md）的兼容点：编排层集中授权、HITL 确认门、最小权限 allowlist 均已存在，
本方案只增加"检索入口"，不改变授权模型。冲突点：无。

### 2.1 重点参照：Claude Code 实现"模式 2"的原理

Claude Code 是模式 2 目前最完整的公开实现，其核心是**分层披露（tiered disclosure）**——
不是"什么都不注入"，而是把工具信息切成成本差 2-3 个数量级的三层：

| 层 | 内容 | 单位成本 | 何时进上下文 |
|----|------|----------|--------------|
| Tier 0 常驻 | 工具**裸名**（无描述、无 schema）+ server 一句话说明 | 每个名字 ~3-10 token | 启动即进，每轮在场 |
| Tier 1 | 工具 description（~20-50 token） | 搜索命中才展开 | 模型调用 tool_search 后 |
| Tier 2 | inputSchema（~200-2000 token） | 点名才展开 | 模型选定条目后 |

官方表述（[Claude Code MCP 文档](https://code.claude.com/docs/en/mcp)）：
"Only tool **names** and server **instructions** load at session start."
社区实测（[Repowise](https://www.repowise.dev/blog/mcp/what-model-context-protocol-actually-sends-to-claude-code)）：
单个 server 启动占用 ≈ 30 token 的工具名列表 + 279 token 的 server 说明。
同规模全量 schema 注入需要数十万 token——**裸名目录比完整 schema 便宜 100-300 倍**。

**为什么必须留一层"裸名"常驻？** 若启动时注入为零，模型面对任务根本不知道环境里有某个
能力域，永远不会发起搜索，元工具模式即失效。裸名是最低成本的"存在性声明"：
`mcp__pg__query_slow_log` 一个名字就足以让模型联想到"应搜索 postgres 日志工具"。
同时名字不承担匹配职责（匹配在索引层做，那里有全量描述），所以可以省到只剩名字。

**两个变体：**

1. **Claude Code 客户端式**（MCP 工具搜索）：Tier 0 裸名常驻；tool_search 在客户端执行，
   检索索引持有全量 description；命中的 schema 以 tool_reference 解冻进上下文。
2. **Anthropic API 服务端式**（[Tool search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)）：
   所有工具标 `defer_loading` 发给 API，**完全不进模型上下文**（也不计 input token）；
   模型只看到一个 `tool_search_tool_regex`；搜索由 API 服务端执行（匹配范围含工具名、
   描述、参数名与参数描述），返回 `tool_reference` 块（默认 top-5）自动展开为完整定义。
   这是真正 O(1) 启动的形态。

**自适应阈值（auto 模式）**：`ENABLE_TOOL_SEARCH=auto` 的行为不是教条地"永远 defer"——
小工具库直接全量加载，直到工具定义占满上下文约 5% 才开始 defer 其余部分（[实测](https://prompt-architects.com/blog/235-using-mcp-inside-claude-code)）。
即：便宜就直给（省一次搜索往返），贵了才收进索引层。

**对本方案的启示**：Tier 0 常驻的应是与本阶段已绑定能力相关的**条目名清单**（便宜且提供
"知道去搜"的线索），而 description/schema 走搜索后解冻；P0 同时实现 auto 式预算判断——
阶段绑定工具数少时维持现状直注，超预算才启用元工具路径。

## 3. 技术设计

### 3.1 三层结构

```
常驻层（进入 persona/toolkit_brief，每轮在上下文）
  ① 各分类一行摘要（现有 TOOLKIT_CATEGORIES + featured 条目清单）
  ② toolkit_search 元工具说明："需要工具箱能力时调用，返回候选条目摘要"

索引层（进程内，不占模型上下文）
  ③ ToolkitItem 元数据：name/summary/tags/capabilities/category/kind
     → 关键词倒排（复用 loadout.recall 的 _score）起步；可选加 embedding 字段升级向量检索

实体层（磁盘/远端，按需上浮）
  ④ tool：endpoint/command 细节          → 上浮为文本简述注入
  ⑤ mcp：transport/command/url/env      → 上浮后经 McpToolInvoker 可真实调用
  ⑥ skill：body 正文 + scripts          → 上浮为正文注入；脚本只执行不读入
```

### 3.2 端到端流程

```
阶段执行开始
  → 编排层按既有链路注入 toolkit_brief（模式 1 / 手动绑定结果）
  → persona 附加 toolkit_search 元工具说明

阶段执行中，模型判断缺能力
  → 产出结构化意图：StageResult.meta["toolkit_search"] = {"query": "...", "kinds": [...]}
    （沿用 mcp_calls 的"声明意图、编排层执行"模式，授权不进插件）
  → 编排层调用 loadout.recall(query, kinds=...) → 候选摘要（id/kind/name/summary，≤5 条）
  → 结果回填阶段上下文（toolkit_brief 增量追加），告知模型可点名

模型点名某条目（StageResult.meta["toolkit_pick"] = {"item_id": "..."}）
  → 校验：条目存在、enabled、通过 org_policy.allowlist
  → probe_item 探活（CLI 在不在 PATH / skill 是否 executable）
  → 上浮：
      tool  → brief 追加 endpoint 与用途说明
      mcp   → brief 追加连接信息；后续 mcp_calls 可真实调用（走既有 confirm 门）
      skill → brief 追加 body 正文；scripts 注入执行沙箱（只执行，不进上下文）
  → 失败/降级时记录原因回给模型，模型可换条目或换词重搜（闭环）

阶段结束
  → 上浮的条目记入 run loadout 记录（loadout.py 的 LoadoutPlan/LoadoutChoice 复用）
```

### 3.3 上下文与预算控制

| 项 | 预算 | 来源参照 |
|----|------|----------|
| 每次搜索返回候选 | ≤5 条，摘要每条 ≤200 字 | RAG-MCP top-k |
| 单条目上浮正文 | skill body ≤5k 字符，超长截断并标注 | Claude Code ~2KB 描述截断 |
| 单阶段累计上浮条目 | ≤8 条，超出提示模型收敛 | Claude Code `auto:N` |
| mcp 工具单次输出 | 复用 executor 现有截断 | Claude Code 25k 上限 |

### 3.4 安全与治理（不新增授权面）

- 检索结果**永远过滤** `enabled=false` 与 org_policy `allowed_toolkit_ids` 白名单外条目；
- `read_only=false` 的条目上浮时标注"需确认"，真实调用仍走 `graph.py` 既有 confirm 门；
- 元工具意图（search/pick）全部进 events 与 run 记录，供审计回放。

### 3.5 MCP server 生命周期：集中池化与空闲回收

**问题**：平台是多用户多任务的，多个任务可能同时需要同一个 MCP server。现状
（`mcp_runtime.py` + `graph.py:442-487`）是"每次 mcp_calls 临时 spawn、用完 terminate"——
同一 server 被反复冷启动（npx 类可达数秒），且 stdio 进程一对一，无法跨任务复用；
反之若全部常驻，几百个 server 的进程开销不可接受。

**业界参照**：

- **IBM ContextForge MCP Gateway** 的 Session Pool（[Tuning Guide](https://ibm.github.io/mcp-context-forge/latest/manage/tuning/)）：
  `MCP_SESSION_POOL_TTL=300s`（空闲强制关闭）、`MCP_SESSION_POOL_MAX_PER_KEY=10`
  （每 key 池上限）——即"空闲 TTL + 每 key 上限"的池化，官方称 10-20x 延迟改善；
  并明确警告：**仅当上游 server 无 per-user 状态时才可安全共享**。
- **mcp-proxy 类桥接**（[mcp-proxy](https://aicoolies.com/tools/mcp-proxy)）：把 stdio server
  桥成 HTTP/SSE 端点，一个后端进程服务多客户端，是 stdio 跨客户端复用的标准解法。

**关键约束**：MCP stdio transport 是**一对一**的——一个进程的 stdin/stdout 只能挂一个 client，
跨任务直接复用同一进程需要额外的桥接组件。本方案采用**每任务独立实例**（进程池），
不引入桥接：隔离最彻底、实现最简单。

**设计**：`mcp_runtime` 从"临时 invoker"演进为平台级 `McpSessionPool`，按两类分别处理：

```
http / sse 类 server
  → 连接复用，几乎零进程成本，直接多任务共享

stdio 类 server（每任务独立实例，进程池）
  → 实例键 = (item_id, config_hash, task_id)
  → 任务内首次使用懒 spawn，之后任务内所有调用复用同一实例（消灭反复冷启动）
  → 不同任务即使指向同一 server，也各持独立实例——隔离域按任务划分，
    stateful server（浏览器会话、登录态等）同样安全，无跨任务状态泄露
  → 回收条件 = 无在途请求 AND（空闲超过 TTL（默认 300s）OR 任务已结束）
  → 全局实例硬上限 + LRU 驱逐（容量优先于空闲）
```

**代价与控制**：进程数峰值 ≈ 并发任务数 × 各任务涉及的 stdio server 数，由全局 LRU 上限
与空闲 TTL 兜底；`stateful: true` 标记保留，用于提示"该实例不可复用/不可预热"，在每任务
实例模型下跨任务泄露风险天然为零。

**回收细节**（对应"心跳"思路的工程化）：

| 项 | 设计 | 原因 |
|----|------|------|
| 回收判据 | 无在途请求 + 空闲 TTL（默认 300s）或任务结束 | 纯心跳计数会误杀 2 分钟级长调用；任务结束立即回收不占空闲窗口 |
| TTL 取值 | ≫ 冷启动成本 | 避免"关了又开"抖动；ContextForge 取 300s |
| 容量上限 | 全局实例数上限，LRU 驱逐 | 几百个 server 场景下必须有硬顶 |
| 健康检查 | 借出前 ping / 进程存活检查，失败标记并重建 | server 半路挂掉要显式降级而非静默失败 |
| 预热（可选） | featured 高频 server 任务开始时预拉 | 对冲首调冷启动延迟 |

**与 harness 的关系**：claudecode / opencode 等 harness **不各自拉起 MCP**，统一经平台端点
接入（池归平台，审计/白名单/密钥全部集中，与 3.4 治理一致）；确需本地运行的 server 才由
harness 自持配置（隔离路径）。

## 4. 文件与改动范围

| 文件 | 改动 |
|------|------|
| `src/sortie_deck/loadout.py` | `recall()` 增加 `query` 自由文本入口与候选摘要序列化；新增 pick/unfreeze 辅助函数 |
| `src/sortie_deck/graph.py` | 节点内处理 `StageResult.meta["toolkit_search"] / ["toolkit_pick"]`；增量更新 `toolkit_briefs`；探活降级原因回传 |
| `src/sortie_deck/mcp_runtime.py` | P1：`McpSessionManager` 演进为 `McpSessionPool`（stdio 每任务独立实例，键=item_id+config_hash+task_id；任务内缓存复用；空闲 TTL 回收 + 全局 LRU 上限） |
| `src/sortie_deck/models.py` | `StageResult.meta` 约定文档化（字段复用 dict，不改结构）；`ToolkitItem` 可选加 `stateful: bool` 与 `embedding` 字段（P2） |
| `src/sortie_deck/orchestrator.py` | `_apply_dynamic_loadout` 与 in-loop 装载共用 pick/unfreeze 逻辑，抽公共函数 |
| `src/sortie_deck/org_policy.py` | `filter_toolkit_ids` 复用于检索结果过滤（已有，无改动或仅测试） |
| `tests/` | recall 自由文本用例、pick 权限/停用拒绝用例、探活降级用例、预算上限用例 |
| `apps/web/src`（P2） | 任务详情面板展示 in-loop 装载记录（复用 loadout 展示位） |

预计规模：后端 ~300 行 + 测试 ~200 行，P0 一个工作日内。

## 5. 分期

- **P0**：toolkit_search / pick 意图链路 + 倒排检索 + 上浮注入 + 白名单过滤 + 单测；
  含 auto 式预算判断：阶段绑定条目全量注入的 token 估算 < 上下文 5% 时直注（现状行为），
  超预算时 Tier 0 只注条目名清单、正文走搜索解冻（参照 Claude Code auto 模式）；
- **P1**：上浮条目落 run 记录与前端展示；skill scripts 执行沙箱接入；输出预算硬截断；
  **MCP 会话池**（3.5）：stdio 每任务独立实例（任务内缓存复用）+ 空闲 TTL 回收 + 全局 LRU 上限；
- **P2**：`ToolkitItem.embedding` 向量索引（工具数 >100 后启用）；UI 装载记录面板。

## 6. 备选方案

1. **只强化模式 1**（扩 capabilities 标签、规划器预判更多需求）：实现最省，但无法覆盖阶段中途
   的长尾需求，召回失败仍不可自救。→ 作为保底，不解决根因。
2. **全量 defer、取消手动绑定优先**（Claude Code 式彻底动态）：上下文最省，但编排式多 Agent 需要
   确定性的能力边界（审计/合规），手动预绑定保留主导地位更稳。→ 不采纳为默认。
3. **直接上向量检索**：工具数百规模前收益有限，且引入 embedding 依赖；P2 再评估。→ 延后。
4. **stdio→HTTP 桥跨任务共享**（mcp-proxy 式，一个 server 进程服务所有任务）：进程数最省，
   但需引入并维护桥组件，且 ContextForge 明确警告仅无 per-user 状态的 server 可安全共享
   （stateful server 会跨任务泄露）。→ 本期不采纳；server 数量大、以 stateless 工具为主时
   可作为后续演进。

## 7. 测试要点

- 搜索：自由文本命中（含中文分词）、kinds 过滤、停用/白名单外条目不可见；
- 上浮：tool/mcp/skill 三类各验证注入格式；探活失败降级原因回传；
- 闭环：pick 失败后允许重搜；预算超限行为；
- 回归：手动绑定优先级不被 in-loop 装载覆盖；`TDT_LOADOUT_AUTO` 关闭时 in-loop 检索仍可用
  （独立开关 `TDT_TOOLKIT_SEARCH`）。
- 会话池（P1）：同任务内同 server 复用不重复 spawn；不同任务同 server 各自独立实例互不可见；
  在途请求期间不回收；TTL 到期或任务结束后回收、再借出能重建；LRU 驱逐顺序正确；
  server 崩溃后借出触发重建。
