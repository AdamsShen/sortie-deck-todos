# 多 Agent 协作记忆：业内方案调研与本项目实现对照

> 整理时间：2026-10-21。内容分三部分：① 业内多 Agent 协作记忆方案调研；② 本项目记忆体系实现梳理；③ 逐项对照与差距分析。

---

## 一、业内调研：多 Agent 协作的记忆策略

### 1.1 记忆协作拓扑（谁和谁共享）—— 3 种主流拓扑

| 拓扑 | 做法 | 代表系统 | 优劣 |
|---|---|---|---|
| **Local-Only 纯本地** | 每个 Agent 私有记忆，靠消息通信交换 | CAMEL、AutoGen、Chain-of-Agents | 隐私强、无干扰；但信息冗余、通信开销 O(N²) |
| **Shared 纯共享** | 全局记忆池，所有 Agent 读写 | Memory Sharing framework | 协作快、联合注意；但噪声堆积、无权限区分 |
| **Hybrid 混合（当前最主流）** | 本地记忆 + 协调器/黑板全局记忆 | LEGOMem、Anthropic 多智能体系统、MetaGPT | 平衡隐私/效率/协作 |

### 1.2 框架层面的具体实现

| 框架 | 记忆方案 |
|---|---|
| **MetaGPT** | 全局消息池（环境日志复制）+ **基于角色的订阅**：Agent 按角色注册订阅消息类型，只同步与职责相关的消息进私有记忆缓存，"共享记录 + 个性化视图"（arXiv:2308.00352） |
| **LangGraph** | 短期记忆 = 图 State + checkpointer（线程内）；长期记忆 = **分层命名空间 BaseStore**（如 `("agents", agent_id, "memories")`），支持向量检索，多 Agent 经命名空间隔离或共享；跨 Agent 协调靠共享 State（黑板模式） |
| **AutoGen** | 对话消息列表转发，最灵活但上下文膨胀快 |
| **CrewAI** | Crew 级共享记忆（短期/长期/实体三层），更新集中化需显式管理 |
| **事件总线异步共享**（搭叩实践） | Agent 完成任务压缩上下文后发布 `MemoryShareEvent`，其他 Agent 选择性订阅，传压缩摘要不传全量 |

### 1.3 记忆机制策略 —— 5 类

来自综述《Memory for Autonomous LLM Agents》（arXiv:2603.07670），记忆形式化为「写入—管理—读取」循环：

1. **上下文驻留式压缩**：压缩活跃上下文；风险是丢罕见关键事实。
2. **检索增强存储**（RAG-style）：写入外部向量/结构化存储，行动时检索。
3. **反思式自我改进**：把过往交互总结为规则/教训；风险是错误推断变持久规则。
4. **分层虚拟上下文**（MemGPT/Letta 分页）：core / recall / archival 三层，Agent 显式分页。
5. **策略学习式管理**：RL 训练 Agent 决定存储/检索/更新/丢弃（AgeMem 均分 41.96% 超 Mem0 的 37.14%）。

三维分类体系：**时间范围**（活跃上下文/事件记录/概括事实/可复用技能）× **表征载体**（文本/向量/可执行技能/混合）× **控制策略**（固定规则 / LLM 决策 / 学习决策）。

### 1.4 读写与一致性策略（多 Agent 特有，约 8 种）

**写入侧**：
- 角色分层摘要：协调器写高层计划，执行者写过程日志，评估者写高权重观察；
- 重要性过滤：按任务权重决定是否存储；
- 多粒度存储：全局精简摘要 + 个体详细日志并存；
- 异步事件发布（压缩后共享）。

**读取侧**：
- 角色自适应检索：协调器读全局，执行者读局部；
- 本地优先、共享兜底；
- 学习型检索决策。

**一致性侧**：
- 串行轮流 / 任务级原子更新 / 发布-订阅三种同步机制；
- 冲突时**禁止后写覆盖**，用角色权重、置信度、投票或协调器仲裁；
- 每条记忆记录 Provenance（作者 / 时间 / 置信度 / 证据链），支持仲裁与审计。

### 1.5 记忆内容类型（业界四类，CoALA 分类）

| 类型 | 内容 | 业界生产对应 |
|---|---|---|
| Working（工作记忆） | 当前上下文窗口、近期轮次、工具 schema | 各家 threads/session 自动截断、Anthropic context editing / compaction |
| Episodic（情景记忆） | 具体经历：做了什么、何时、结果 | AWS AgentCore "Session Summaries" / "Episodic" 策略 |
| Semantic（语义记忆） | 用户偏好、事实、领域规则 | AgentCore "User Preferences"/"Semantic"、Mem0 user scope |
| Procedural（程序性记忆） | 工作流、工具使用习惯、可复用技能 | LangMem 显式支持；**业内最不成熟** |

云厂商托管服务（AWS AgentCore Memory GA、Azure Foundry Memory Preview、Vertex Memory Bank Preview）已收敛到 **Extract → Consolidate → Retrieve** 异步流水线；专用记忆层两条路线：Mem0（扁平存储 + 四层作用域 API + 实体链接 + 三路混合检索 + rerank）与 Zep/Graphiti（bi-temporal 时序知识图谱，旧事实失效不删除）。

---

## 二、本项目实现：三层记忆体系

### 2.1 体系总览

| 层 | 代码 | 粒度 | 存储位置 |
|---|---|---|---|
| 条目记忆（共享记忆库） | `src/sortie_deck/memory.py` | 单条事实/偏好/战例/教训，4 作用域 | `data/memory.json`（索引 `data/memory/MEMORY_INDEX.md` 为派生物） |
| 任务文件记忆 | `src/sortie_deck/mission_memory.py` | 滚动总结 / 会话转录 / 绑定文件 | `TDT_DATA_DIR/missions/<id>/memory/` 明文文件 |
| 共享后端 mem0（可选） | `memory.py:375` `Mem0Memory` | 向量检索 | REST 远端，失败静默降级 |

- 条目记忆 4 作用域：`workspace`（全局约定，仅 admin 写）/ `user`（本人）/ `mission`（单任务）/ `agent`（指定角色），见 `memory.py:12-13`。
- 条目类型：`preference` / `lesson` / `episode` / `fact`（`memory.py:13`）；带 `stage:<名称>` 标签的条目只注入对应 stage，子房间结论不跨分支泄漏（`memory.py:27-36`）。
- mem0 经 `TDT_MEMORY_BACKEND=mem0` 启用（默认 `local` 关闭，`settings.py:52-56`）：仅 `mission` 作用域写入时镜像到远端（`memory.py:476-480`），检索命中以 `Shared memory (mem0)` 段并入上下文块（`memory.py:493-519`），按 `user_id = "<prefix>:<initiative_id>"` 任务级隔离。

### 2.2 加载路径（读，3 个注入点）

1. **任务创建时（Plan-time 绑定）** `orchestrator.py:956-972`：用 `title + brief` 关键词打分取 top-8，简介行写 `ini.meta["memory_brief"]`，绑定 ids 写 `ini.meta["memory_ids"]`。注意：此处调 `context_candidates()` 纯本地，**不查 mem0**。
2. **每个 stage 执行前（Run-time）** `graph.py:1120-1129` → `mission_memory_brief()`（`orchestrator.py:96-124`）：四层作用域合并 + stage 过滤 + 相关性打分，产出 `StageContext.memory_brief`。
3. **harness prompt 拼装** `executor.py:287-329`：注入三段——`Rolling mission memory`（滚动总结全文）、`Bound file`（绑定文件，单文件截 6000 字符）、`Layered memory brief`（条目简介块）。全部纯 prompt 拼接，`provider.send` 实际只用 `prompt`。

简介块由 `render_block()`（`memory.py:343-351`）渲染：每条一行 `- [kind|id] summary`，总量硬截 6000 字符；Agent 需要全文时自己按 id 读 `memory/entries/<id>.md`（Claude Code MEMORY.md 模式）。

### 2.3 生成路径（写）

1. **人类写入**：记忆库页 `POST /api/memory`，落库后异步 LLM 生成 ≤60 字简介（`api/app.py:876-907`），状态机 `pending → done/failed`，失败降级 content 截断。
2. **任务启动落记忆包** `orchestrator.py:1244-1257` → `write_memory_pack`（`mission_memory.py:302-351`）：写 `MEMORY_INDEX.md`（简介索引）+ `entries/<id>.md`（单条全文）+ `usage.log`（取用记录）进任务目录。
3. **每个 stage 执行后自动生成** `executor.py:184-198`：
   - `record_turn` → 会话转录追加 `sessions/<sid>.jsonl`（prompt/reply 各截 2000 字符）；
   - `append_memory` → 滚动总结 `memory.md` 追加流水（回复前 600 字符）；
   - `maybe_summarize` → 超 `TDT_MISSION_CONTEXT_TOKEN_LIMIT`（默认 60000 token，`settings.py:49`）时 LLM 压缩写回（`mission_memory.py:236-279`），失败降级"保头 + 保最近 4 条"。

### 2.4 使用方式

- **被动注入**：上述 3 个注入点全部 prompt 工程拼接；
- **主动取用**：Agent 在 workspace 读 `MEMORY_INDEX.md` 决定加载哪条，再读 `entries/<id>.md` 全文，取用后 `echo <id> >> memory/usage.log`（best-effort）。

---

## 三、逐项对照与差距分析

### 3.1 已对齐项

| 业界策略 | 本项目实现 | 位置 |
|---|---|---|
| Hybrid 拓扑（本地私有 + 共享记忆池） | agent 作用域条目 + mission 作用域 + workspace 全局 | `memory.py:12-13` |
| 黑板/共享状态 | LangGraph State + artifact index 注入 | `executor.py:326-329` |
| MetaGPT 角色订阅式隔离 | `stage:<名称>` 标签只注入所属 stage | `memory.py:27-36` |
| 结果摘要化传递 | 子房间结论以简介行注入（不传全量） | `memory.py:343-351` |
| Provenance 记录 | author / created_at / usage.log | `mission_memory.py:96-98` |
| 短期记忆压缩（compaction） | 滚动总结 + LLM 超限压缩写回 | `mission_memory.py:236-279` |
| 作用域分层 API | 四层作用域（对齐 Mem0 四层作用域设计） | `memory.py:12-13` |
| 事件式异步摘要生成 | 记忆简介异步 LLM 生成不阻塞写入 | `api/app.py:876-907` |

### 3.2 差距项（改进方向，按性价比排序）

| # | 业界策略 | 本项目现状 | 建议 |
|---|---|---|---|
| 1 | **程序性记忆**（四类之一，AgentCore/LangMem 单列） | `kind=lesson` 有字段但无反思生成管线 | 每阶段结束后按角色反思沉淀可复用经验，跨任务同角色检索 |
| 2 | **角色分层写入摘要** | `append_memory` 无条件追加回复头部，流水账（`executor.py:194-197`） | 按角色区分写入粒度：协调者写决策/结论/风险，执行者只写增量 |
| 3 | **记忆治理：衰减/失效/TTL** | `_score_entry` 仅关键词重合度（`memory.py:107-112`），无时间衰减 | 条目级失效标记（Zep bi-temporal 思路，失效不删除）+ 新近度加权 |
| 4 | **冲突检测更新**（Azure Foundry Consolidation） | 无 | 写入时 LLM 三选一：新增 / 更新覆盖 / 丢弃冗余，时间戳保留演变轨迹 |
| 5 | **并行分支结论合并仲裁** | 子房间并行 stage 靠 stage 隔离规避冲突，无合并仲裁 | 多分支结论汇入同一 stage 时按权重/置信度合并 |
| 6 | **memory_search / memory_read 工具** | 未实现（`docs/zh/modules/memory.md` 计划中），Agent 只能靠文件系统读 entries | 注册内置工具 + 加载护栏 |
| 7 | **Plan-time 绑定含 mem0** | `create_initiative` 的 `context_candidates` 纯本地（`memory.py:308`） | 检索绑定统一走 `context_block` 或 candidates 增加 mem0 分支 |
| 8 | **反思式记忆质量评估** | 无 | 用 LoCoMo/LongMemEval 思路做注入有效性抽检 |

> **落地状态（2026-10-21）**：#1-#5 已实施（记忆治理阶段一）；条目记忆检索已切换 WeKnora 混合检索 + rerank + graph（超出原差距清单范围，见 `docs/plans/2026-10-21-memory-weknora-retrieval.md`）；#6 记忆元工具（memory_search/memory_read）、#7 mem0 命中前置、#8 质量评估脚本（local 基线命中率 38%）已实施（阶段二）。全链路策略见 `docs/记忆体系整体策略.md`。

### 3.3 附带发现

- `harness_providers/opencode.py:37` 复用 `settings.mem0_timeout` 做自己的 HTTP 超时，配置耦合，建议拆独立配置项。

---

## 四、参考来源

- 多智能体记忆设计综述（TechRxiv, ShanglinWu/LLM-MAS_Memory_Survey）：https://blog.csdn.net/qq_58602552/article/details/160121353
- Memory for Autonomous LLM Agents（arXiv:2603.07670）：https://www.alphaxiv.org/zh/abs/2603.07670v1
- MetaGPT（arXiv:2308.00352）：https://arxiv.org/pdf/2308.00352v3
- LangGraph 记忆文档：https://langgraph.com.cn/concepts/memory.1.html
- 搭叩事件总线共享实践（InfoQ）：https://www.infoq.cn/article/ufSVUGyl6fvVMqx67yCc
- 多 Agent 记忆架构作用域分析：https://dialoguedb.com/blog/multi-agent-memory-architecture
- Mem0《State of AI Agent Memory 2026》：https://mem0.ai/blog/state-of-ai-agent-memory-2026
- StudyTrails《Agent Memory Systems》：https://www.studytrails.com/agentic-ai-book/chapter-3-agent-memory/
- 本项目文档：`docs/zh/modules/memory.md`、`docs/zh/modules/mission-memory.md`、`docs/zh/principles/mission-memory.md`
