# 角色 Agent 设计的业界合理性调研

- **调研主题**：多 Agent 编排系统中「角色 Agent」的设计是否合理
- **被测设计**：Sortie Deck（AI 原生办公迭代控制平面）
- **调研范围**：12 个业界对象，2024 年至今
- **调研日期**：2026-11-16
- **方法**：Deep Research（模型内部框架 → Web 检索补充 → 逐项结构化 → 横向对比）

---

## 0. 摘要

**总体判断：被测设计的核心思路是主流且合理的，与 2024–2026 年多 Agent 工程实践的收敛方向一致；但在"角色级可配置性"和"契约字段的真实生效"上存在明确偏差，属于可修正的实现缺口，不是路线错误。**

被测设计可以拆成 5 个可独立评判的决策：

| # | 设计决策 | 业界对应 | 判断 |
|---|---|---|---|
| D1 | 角色 = persona 提示词 + 职责契约 + 工具/MCP/skill 绑定 | CrewAI `role/goal/backstory/tools`；OpenAI Agents SDK `instructions/tools/handoffs`；Claude Code subagents 的 frontmatter + system prompt | ✅ 主流 |
| D2 | 角色差异在编排层烘焙进 prompt/context pack，共享同一 harness | Strands "agent = model + system prompt + tools + loop"；Anthropic 子 agent 只拿任务+资源+数据源 | ✅ 主流 |
| D3 | 每阶段新建会话，跨阶段靠文件化记忆 + 产物契约接力 | Discrete Phase Separation；Anthropic orchestrator-worker；MetaGPT "交接只认结构化文档" | ✅ 主流，且有量化支持 |
| D4 | 双层压缩：provider auto-compact + 编排层 rolling summary（60K） | Anthropic compaction / structured note-taking；JetBrains observation masking vs LLM summarization | ✅ 方向正确，策略有优化空间 |
| D5 | 权限/模型/回合数不按角色配置，按阶段/任务/全局 | Claude Code subagents 支持 per-agent `model`+`tools`；OpenAI Agents SDK 支持 per-agent `model` | ⚠️ 明确偏差 |

一句话结论：**"角色是共享引擎上的配置快照"这个抽象是业界主流；但主流框架普遍允许角色级配置 model/tools/permissions，本项目目前只做到提示词与工具绑定级，这是最值得补的一块。**

---

## 1. 合理的设计（有直接业界对应）

### 1.1 角色 = 人格 + 契约 + 绑定

业界几乎所有角色化框架都是这个三件套，差别只在载体：

- **CrewAI**：`Agent(role, goal, backstory, tools, llm)` —— "Role-Playing as Code"，其中 role/goal/backstory 就是人格，tools 是绑定，Task 的 `description/expected_output` 承担契约。
- **OpenAI Agents SDK**：`Agent(instructions, tools, model)` + `handoffs` + `sessions`，官方定义就是"LLM configured with instructions, tools, and optional runtime behavior"。
- **Claude Code Subagents**：`.claude/agents/*.md` 的 YAML frontmatter（`name`/`description`/`model`/`tools`）+ body 作为 system prompt，**独立 context window 与独立权限**。
- **MetaGPT**：Role 携带 profile/goal/constraints + SOP，交接只认结构化文档。

所以"角色 agent 不只是提示词"这个判断是对的：**人格 + 契约 + 绑定**三件套正是业界的最小共识集。

### 1.2 共享 harness、角色差异在调用前烘焙

Strands Agents 对 agent 的定义是极简的："a model, a system prompt and a list of tools run in a loop inside your own process"。这与本项目的 `HarnessStageExecutor` 完全同构 —— **provider 不认识角色，只吃 prompt + options**。

Anthropic 的多 Agent 研究系统把同一原则推到极致：子 agent 只拿到「任务描述 + 相关上下文 + 数据源列表」，**不继承他人的对话历史**。本项目用 `StageContext` 承载同样的三元组（stage/role + upstream/inputs + toolkit/repos），是同一范式的工程化。

> 结论：**不写 `.claude/agents/` 文件、改用代码拼 prompt，不是偏离，而是"编排层拥有角色定义权"的选择。** 落盘文件适合人类手工维护的固定角色（Claude Code 的产品形态），代码动态生成适合按任务组合角色（编排系统形态）。两者各有适用面。

### 1.3 每阶段新会话 + 产物交接

这是本项目最"重"的设计，但恰恰是业界 2024–2026 收敛最明显的方向：

- **Discrete Phase Separation**：每个阶段（research/planning/execution）在独立会话运行，**只有蒸馏后的 artifact 跨边界，不传完整历史**。理由是一旦研究、计划、实现挤在同一个窗口，三者互相争夺注意力，输出全面劣化。
- **Anthropic 多 Agent 研究**：主 agent 把计划写入 Memory（因为 200K 窗口会截断），子 agent 各自压缩后回传。
- **MetaGPT**：交接只认结构化文档，"不认对话"，以此抑制级联幻觉。
- **Amp**：为不同子任务设计专用 sub-agent（Finder/Oracle/Librarian/Kraken），各自独立 context window，只回传精华。
- **量化支持**：有工程总结指出，**传递完整对话转录的交接比传递状态摘要的任务完成率低约 34%**，因为下一个 agent 会继承近因偏差和自相矛盾。

> 结论：**"每阶段新会话 + 产物契约 + 文件记忆 bootstrap"是经过验证的主流做法，比"一个长会话贯穿全流程"更稳。**

### 1.4 双层压缩

- **provider 内 auto-compact**：对应 Anthropic 2025-09 工程博客推荐的 compaction —— 摘要后重启干净窗口；Claude Code 的 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 就是这一层的产品化。
- **编排层 rolling summary（60K 预算）**：对应 structured note-taking —— 把要点写到上下文窗口之外，下次按需取回。
- **按需挂 MCP（strict_mcp_config）**：对应 MCP 官方 client best practices 的 progressive tool discovery —— 不把全部工具 schema 灌进上下文（官方示例中 33 个工具就占 8000+ token）；也对应 `tool_search` 元工具模式。

> 结论：**两层分工（会话内交给 provider、跨阶段交给编排层）是合理切分，避免了重复压缩。**

---

## 2. 横向对比表

| 对象 | 角色载体 | 角色级差异 | 会话粒度 | 跨阶段状态 | 压缩策略 | 与本项目关系 |
|---|---|---|---|---|---|---|
| LangGraph | 代码/节点 | prompt、tools、model | thread 级 | 共享 state + checkpointer | 自建 | 同源（graph/checkpointer） |
| CrewAI | 代码 | role/goal/backstory/tools/llm | Crew 内顺序 | Task 输出交接 | 无内置 | 高度相似 |
| AutoGen | 代码 | system_message/tools/model | Team 共享对话 | 共享消息历史 | 弱 | 差异最大（共享 vs 隔离） |
| OpenAI Agents SDK | 代码 | instructions/tools/model/handoffs | turn + Session | handoff + session | 自建 | 高 |
| MetaGPT | 代码 | profile/订阅/动作/SOP | 每 Role 独立 | 共享消息池 + 文档 | 结构化产物天然压缩 | 很高 |
| Claude Code Subagents | **文件**（frontmatter+body） | system prompt/tools/model/permissions | 每 subagent 独立窗口 | 只回传结果 | 自身 auto-compact | 概念同构，载体不同 |
| Strands | 代码 | system prompt/tools/model | 进程内 loop | AgentCore Memory | 云服务层 | 中 |
| Anthropic 研究系统 | 代码编排 | 任务/工具/轨迹 | 每子 agent 独立窗口 | 主 agent Memory + 共享文件 | 并行压缩 | 很高 |
| Devin/Factory/Amp | 产品内部 | 专用 sub-agent（模型+工具集） | 每任务/每子任务 | 环境持久化 + 精华回传 | 子 agent 隔离 | 高（部分不公开） |
| Letta (MemGPT) | 代码（core memory block） | 人设 block/记忆工具 | runtime 持久化 | 三层记忆 | agent 主动管理 | 中 |
| 反方（Cognition） | — | 反对按角色拆分 | 单会话 | 状态摘要交接 | 单会话压缩 | 反向参照 |
| 上下文工程实践 | — | — | compaction 可重启窗口 | note-taking / 子 agent | compaction + masking + tool search | 高（本项目已部分采纳） |

---

## 3. 偏差与风险（需要正视）

### 3.1 角色级 model/tools/permissions 缺失（最主要的偏差）

**业界事实**：Claude Code subagents 的 frontmatter 支持 `model` 和 `tools`；OpenAI Agents SDK 的 `Agent` 直接带 `model`；Amp 的 Finder 用小而快的模型 + 精简工具集，Oracle 用强推理模型。

**本项目现状**：`model`、`permission_mode`、`max_turns`、`disallowed_tools` 全部是**阶段级/任务级/全局**，没有 per-role 配置（`docs/zh/modules/executors.md` 与代码一致）。persona front-matter 虽然声明了 `tools_allowed`，但**运行时并未消费**。

**影响**：
- 成本优化做不了 —— 无法让 QA/评审用强模型、代码检索用弱模型；
- 权限最小化做不了 —— 无法给 QA 角色"只读 + 跑测试"、给 deploy 角色"只读 + 发布"，只能按阶段粗粒度限制；
- 契约与执行不一致 —— `tools_allowed` 声明了却不生效，属于"文档/契约撒谎"。

### 3.2 压缩策略偏"摘要"，缺少"观察掩码"这条对照路线

JetBrains 2025 在 SWE-bench Verified 上对比了两种策略：**observation masking**（把旧的文件内容/命令输出替换为占位符，保留推理与动作）与 **LLM summarization**（用另一个模型压缩历史）。结论是 masking 往往更省更稳，因为它不丢"做了什么"，只丢"看到了什么"。

本项目跨阶段用的是纯 LLM 摘要 + 截断降级，**早期精确路径、命令、错误码容易在摘要中丢失**。项目文档自己也承认"总结质量受截断影响"。

### 3.3 历史会话"只写不读"

`memory/sessions/<sid>.jsonl` 记录完整转录，但 `ContextPack.session_log` 未进入 prompt（`docs/zh/modules/mission-memory.md` 明确写了"当前只写不读"）。长任务里"三阶段前那个决策是怎么定的"只能靠 memory.md 的摘要，**无法回查原文**。Letta 的 recall memory 与 Anthropic 的共享文件系统都在解决同一问题。

### 3.4 阶段内自审多轮共用会话，存在 context rot 风险

自审 3–5 轮是续接同一会话（`executor.py` 的 `send()` 注释："第 2..N 轮必须看到首轮实现上下文"）。这保证了连续性，但 Anthropic 的实践是**让子 agent 在干净上下文里用结构化结论复核**，避免评审者被实现过程"带偏"。当前做法在长阶段上可能让自审退化为自我确认。

### 3.5 记忆写入质量依赖角色分层，执行角色易退化为流水账

`build_stage_summary` 只对协调角色（product/ops）走 LLM 提炼，执行角色固定截取前 600 字流水。这符合"协调者写决策、执行者写增量"的业界思路，但**执行角色占阶段数最多**，长期任务里 memory.md 仍会以流水账为主。

### 3.6 共享 harness 全任务锁定，牺牲了按阶段换模型的弹性

任务锁定一种 harness（`harness_locked`）换来会话/权限/工具行为的一致性，这是合理权衡。但业界（如 OpenAI 的 runtime 选择、Amp 的专用 sub-agent）允许**按子任务选不同能力档位**。本项目的补偿手段是阶段级 `max_turns`/`disallowed_tools`，粒度够用但不精细。

---

## 4. 改进建议（按优先级）

### P0 —— 契约一致性（低成本、高收益）

1. **让 `tools_allowed` 真正生效，或从契约里删掉**。当前声明未消费，属于隐性技术债；下游若照着它做权限设计会踩空。
2. **补角色级 `model` / `effort` 配置**。在 `PublishedAgent` / `RoleAgent` 上加可选 `model` 与 `tools` 白名单，由 `_build_context_pack` 透传；对齐 Claude Code subagents 与 OpenAI Agents SDK 的既有做法。默认空 = 沿用现状，零回归。

### P1 —— 上下文与记忆

3. **压缩策略引入 observation masking**：对工具输出（文件内容、命令 stdout、日志）优先用占位掩码 + 保留动作与结论，只在超阈值时才走 LLM 摘要。可先在 `mission_memory` 的降级分支做 A/B。
4. **把 session_log 接入 bootstrap 检索**：按当前 stage/role 关键词召回相关历史轮次（而非固定取最近 10 轮），解决"只写不读"。
5. **角色级权限模板**：给 qa / deploy 这类"验证型/发布型"角色提供只读基线，避免仅靠阶段名判断。

### P2 —— 结构优化

6. **自审镜头拆干净子会话**（可选）：第 3 轮"测试缺口"这类只读视角，用独立上下文 + 传入 `diff.patch` + 清单，减少被实现过程带偏。
7. **memory 写入分级**：执行角色的流水账也做"每 N 条合并一次"的轻量归纳，避免 memory.md 长期退化为日志。
8. **记录压缩决策**：把每次 auto-compact / rolling summary 的触发原因、丢弃内容类型写入阶段 meta，便于事后归因（当前只记录 token 数）。

---

## 5. 结论

| 问题 | 回答 |
|---|---|
| 角色 agent 是否合理 | ✅ 合理。"人格 + 契约 + 工具绑定"是 CrewAI / OpenAI Agents SDK / Claude Code subagents 的共同最小集 |
| 共享 harness 靠代码拼 prompt 是否合理 | ✅ 合理，与 Strands 的 agent 定义同构；不落 `.claude/` 文件是"编排层拥有角色定义权"的选择，不是缺陷 |
| 每阶段新会话是否合理 | ✅ 合理且是主流，有 Discrete Phase Separation / Anthropic / MetaGPT 三方印证，且有"摘要交接优于裸转录"的量化支持 |
| 双层压缩是否合理 | ✅ 分工正确；策略上建议补 observation masking 与历史检索 |
| 最需要改的 | ⚠️ 角色级 model/tools/permissions 缺失；`tools_allowed` 声明未生效 |

**一句话**：这套设计不是"自造概念"，而是把业界（尤其是 Anthropic 与 MetaGPT 路线）的"隔离 + 契约 + 摘要"范式做成了可运行的编排系统；下一步的价值不在改范式，而在把"角色"从提示词级提升到**配置级**（model/tools/permissions），并让契约字段真正闭环。

---

## 6. 来源

### 框架
- LangGraph Persistence: https://docs.langchain.com/oss/javascript/langgraph/durable-execution
- LangGraph 生产实践: https://prodinit.com/blog/langgraph-production-guide
- CrewAI 架构演进: https://www.crewai.com/blog/how-crewai-is-evolving-beyond-orchestration-to-create-the-most-powerful-agentic-ai-platform
- CrewAI role 抽象: https://con-fucius.github.io/ai-ml-research-hub/frameworks/crewai
- AutoGen / Magentic-One: https://aiwiki.ai/wiki/autogen
- AutoGen HITL 上下文丢失 issue: https://github.com/microsoft/autogen/issues/7036
- OpenAI Agents SDK Agents: https://openai.github.io/openai-agents-python/agents/
- OpenAI Agents SDK 概览: https://lmversity.com/learn/agent-frameworks/openai-agents-sdk
- MetaGPT 解析: https://pyshine.com/metagpt/
- MetaGPT 论文解读: https://lacuna.tiptreesystems.com/work/metagpt-meta-programming-for-a-multi-agent-collaborative/version/art_7be03e3210dbec641688b76b190e9381

### Harness 与产品
- Claude Code Subagents（官方博客）: https://claude.com/fr/blog/steering-claude-code-skills-hooks-rules-subagents-and-more
- Claude Code Sub-agents 文档: https://code.claude.com/docs/ko/sub-agents
- Claude Code subagents 指南: https://agentstacklist.com/guides/claude-code-subagents
- Anthropic 多 Agent 研究系统: https://blog.ivheli.com/anthropic/zh/blog/17-%E5%A4%9AAgent%E7%A0%94%E7%A9%B6%E7%B3%BB%E7%BB%9F%E6%9E%84%E5%BB%BA
- Anthropic 实战经验: https://www.cnblogs.com/informatics/p/19625900
- 子 Agent 上下文隔离: https://guanweilu.cn/article/587.html
- AWS Strands 发布: https://aws.amazon.com/blogs/opensource/introducing-strands-agents-an-open-source-ai-agents-sdk
- Strands + AgentCore Memory: https://docs.amazonaws.cn/bedrock-agentcore/latest/devguide/strands-sdk-memory.html
- Amp Code 设计哲学: https://ain3xt.com/cn/posts/20251225-amp-code-contrarian-design
- Factory vs Devin: https://continuumcode.ai/guides/factory-ai-vs-devin
- Devin 生产实践: https://www.sitepoint.com/devin-ai-engineers-production-realities/

### 记忆与上下文工程
- Letta (MemGPT) 三层记忆: https://aiwiki.ai/wiki/letta
- Letta 深度介绍: http://quidproquo.cc/posts/ai/2026-08-22-letta-memgpt-agent-memory
- Anthropic 上下文工程: https://www.sefism.com/resources/effective-context-engineering-for-ai-agents
- MCP Client Best Practices: https://modelcontextprotocol.io/docs/latest/develop/clients/client-best-practices
- 上下文窗口与会话生命周期（JetBrains 对比）: https://zylos.ai/research/2026-03-31-context-window-management-session-lifecycle-long-running-agents
- Meta-tool 渐进披露: https://blog.synapticlabs.ai/bounded-context-packs-meta-tool-pattern

### 反方与阶段分离
- Single vs Multi-Agent 综述: https://neuralmasteryai.com/docs/agents/single-vs-multi-agent
- Cognition "Don't Build Multi-Agents" 讨论: https://toolhalla.ai/blog/single-agent-vs-multi-agent-2026
- 状态摘要交接优于裸转录: https://tryinterlock.com/blog/2026-agent-handoffs-state-summaries-beat-raw-transcripts-34.php
- Discrete Phase Separation: https://agentpatterns.ai/patterns/agent-design/discrete-phase-separation
- 每步新会话实践: https://github.com/MarkTripoli/skills/blob/main/docs/context-management.md
- 阶段产物交接实践: https://lobehub.com/es/skills/liminal-ai-liminal-spec-ls-research

> 说明：Devin / Factory / Amp 的内部会话架构未公开，相关结论依据官方文档、工程博客与第三方分析推断，已在对比表中标注"部分不公开"。所有来源均为 2024 年至今的公开材料。
