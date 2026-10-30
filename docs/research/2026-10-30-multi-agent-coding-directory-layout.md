# 多 Agent 协作项目「编码任务」落盘目录调研

> 调研日期：2026-10-30
> 触发问题：`data/missions/<id>/` 下的 `artifacts / chat / logs / memory / workspace` 五件套，在业界是不是主流？还有哪些常落地的文件夹？
> 结论口径：只讨论**运行期/任务级数据目录**（agent 干活时写出来的东西），不讨论框架源码目录与仓库内的 `src/` 组织。

## 0. TL;DR

1. **没有统一标准，但语义高度收敛**：业界目录名不同，实际只分 6 类语义 —— 输入、工作区、交付物、轨迹日志、会话消息、记忆/状态。业界的差异 90% 是用词差异，10% 是真设计差异。
2. **五件套（artifacts / chat / logs / memory / workspace）覆盖了其中 5 类，属主流形态**，尤其与「runtime session 目录」类产品（OpenHands 旧版 runtime、各类沙箱开箱布局）同构。
3. 业界最普遍、本项目目前**没有显式落地**的两类是：`input/`（只读输入的快照）与 `state/`（可独立重放的运行状态）。是否要补，取决于"任务目录能否拷走后离线复现"这一目标。
4. 三个更强的共性约定（比目录名更重要）：**一任务一根目录**、**交付物与过程物料分家**、**生命周期分级回收**。

## 1. 业界实例对照

| 系统 | 任务级目录形态 | 关键特征 | 来源 |
|---|---|---|---|
| MetaGPT | `workspace/<task>/` → `docs/` `resources/` `src/` `tests/` `requirements.txt` | 目录直接是**产出软件工程结构**，不是元数据容器 | [MetaGPT 使用说明（二手）](https://blog.csdn.net/weixin_27021789/article/details/160905588) |
| SWE-agent | `trajectories/<user>/<exp>/` → `preds.json`、`run_batch.*.log`、`<instance>/` → `<id>.traj` `<id>.config.yaml` `<id>.{debug,info,warning}.log` | **轨迹与配置同目录**，一份 config 即可复现该次运行；日志按级别分文件 | [SWE-agent Output files](https://www.swe-agent.com/latest/usage/trajectories/) |
| OpenHands SDK | `workspace/conversations/<conversation_id>/` → `base_state.json` + `events/event-00000-<id>.json` | **状态快照 + 事件流**两件套；事件一事件一文件，便于增量恢复 | [OpenHands 持久化](https://docs.openhands.org.cn/sdk/guides/convo-persistence) |
| Claude Code | `~/.claude/` → `projects/<project>/<session>.jsonl`、`<session>/subagents/`、`<session>/tool-results/`、`projects/<project>/memory/`、`file-history/`、`debug/`、`tasks/`、`plans/`、`history.jsonl` | 大工具输出**外溢到独立文件**避免污染主记录；`file-history` 做检查点回滚；`todos/` `logs/` 已废弃 | [Explore the .claude directory](https://code.claude.com/docs/en/claude-directory) |
| Codex CLI | `${CODEX_HOME:-~/.codex}/` → `sessions/YYYY/MM/DD/rollout-*.jsonl`、`archived_sessions/`、`session_index.jsonl`、`history.jsonl`、`state_*.sqlite` | 会话按**日期分桶**；JSONL 为事实源，SQLite 只是投影/索引 | [Codex 数据格式](https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/codex-history-ingest/references/codex-data-format.md) |
| CrewAI | 平台目录（macOS `~/Library/Application Support/CrewAI/<project>/`）→ `knowledge/` `short_term_memory/` `long_term_memory/` `entities/` `long_term_memory_storage.db` | 记忆**按类型分目录**，向量库目录 + SQLite 混合 | [CrewAI Memory](https://www.aidoczh.com/crewai/en/concepts/memory.html) |
| Google ADK | `root/users/{user_id}/sessions/{session_id}/artifacts/{artifact_path}/versions/{version}/` + `metadata.json`；本地 `.adk/artifacts`、`sessions.db` | **产物带版本目录**，artifact 是二进制/文件一等公民 | [FileArtifactService](https://adk.dev/api-reference/typescript/classes/FileArtifactService.html) |
| AgentScope | `{workdir}/` → `.mcp/` `data/`（多模态 offload） `skills/` `sessions/<id>/context.jsonl` + 工具结果文件 | 把 **MCP 配置、技能、会话**都收进工作区，做到"目录即环境" | [AgentScope 工作区](https://docs.agentscope.io/zh/versions/2.0.2/building-blocks/workspace) |
| Harbor（编码 agent 评测） | 沙箱内 `/workspace`（任务）、`/tests`（输入）、`/harbor/agent`（轨迹日志）、`/harbor/verifier`（评测输出/reward） | **输入/工作区/轨迹/评测四区物理隔离**，跨 Docker/E2B/Daytona 统一路径 | [Harbor Environments](https://deepwiki.com/laude-institute/harbor/3.4-environments) |
| WeKnora 沙箱 | `/workspace/input`（只读输入）、`/workspace`（脚本）、`/workspace/output`（可下载产物） | 三段式约定，产物目录单独可下载 | [技能目录与沙箱](https://github.com/luckydududu/WeKnora/blob/main/website-docs/03-features/22-skills-sandbox.md) |
| AWP v1.0 规范 | 包内：`workflow.awp.yaml` + `agents/{id}/` + `skills/` + `mcp/`；运行时：`workspace/MEMORY.md` + `workspace/memory/YYYY-MM-DD.md` + `data/{input,output,state}`；**明确排除** `logs/` `runs/` `workspace/` `data/state/` | 少见的"规范化"尝试：区分**可移植源包**与**运行时数据** | [AWP File Structure](https://github.com/veegee82/agent-workflow-protocol/blob/main/spec/versions/1.0/file-structure.md) |

概念层面的两个"一等公民"佐证：

- **A2A 协议**把 `artifact` 定义为任务的"实际交付物"，与围绕它的对话（message）显式区分 —— 这正是 `artifacts/` 与 `chat/` 分家的协议级依据（[A2A Artifact](https://agent2agent.info/docs/concepts/artifact/)）。
- **LangGraph** 把持久化拆成两条互不越界的平面：checkpointer（thread 内短期状态，可断点续跑）与 store（跨 thread 长期记忆，按 namespace 组织）—— 这正是 `state/checkpoints` 与 `memory/` 分家的框架级依据（[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)）。

## 2. 收敛出的目录语义表

| 语义 | 常见目录名（别名） | 是否交付物 | 典型生命周期 | 主要读者 |
|---|---|---|---|---|
| 输入 | `input/` `data/input/` `/tests` `attachments/` | 否 | 随任务删 | agent（只读） |
| 工作区 | `workspace/` `repo/` `worktrees/` `/workspace` `sandbox/` | 否 | **可回收**（push 后/TTL） | agent（可写） |
| 交付物 | `artifacts/` `outputs/` `deliverables/` `data/output/` | 是 | 永久保留、可版本化 | 用户、下游阶段 |
| 轨迹/过程 | `logs/` `trajectories/` `runs/` `traces/` `debug/` | 否 | TTL（7–30 天） | 排障、评测、训练 |
| 会话消息 | `chat/` `sessions/` `events/` `transcript*.jsonl` | 半（人可读） | 保留或 TTL | UI 回放、上下文重建 |
| 记忆/状态 | `memory/` `MEMORY.md` `state/` `checkpoints/` `*.sqlite` | 否 | 记忆长期；状态短期 | 下一轮 agent、续跑 |
| 配置/技能 | `skills/` `.agent/` `.cursor/rules/` `AGENTS.md` `CLAUDE.md` | 源资产 | 版本控制 | agent 启动期 |

## 3. 五条共性设计原则

1. **一任务一根目录**（mission/session 即隔离单元），元数据集中存（CDE 模式）。删除任务 = 删一个目录 + 删一条元数据记录。
2. **输入 / 工作区 / 产出 三段式**，可写区被夹在中间，回收时只铲中间那段（Harbor、WeKnora、本项目一致）。
3. **交付物与过程物料分家**：`artifacts/` 给人看、给下游消费、受契约校验；`logs/` 给机器看、可丢、不对外展示（A2A 的 artifact vs message 之分）。
4. **短期状态与长期记忆分家**：checkpoint/state 追求"逐节点可重放"，memory 追求"人可读可编辑"。混用会导致要么重放不了，要么记忆被状态淹没。
5. **生命周期分级**：workspace（可回收）→ logs（TTL）→ artifacts/memory/chat（长期）。业界普遍在 README/规范里显式声明这三档，而不是靠代码隐式行为。

## 4. 与本项目现状对照

现状（`src/sortie_deck/layout.py:24`）：`_MISSION_SUBDIRS = ("artifacts", "logs", "workspace", "chat", "memory")`，注释见 `layout.py:5-10`。

| 语义 | 本项目 | 业界对应 | 评价 |
|---|---|---|---|
| 交付物 | `missions/<id>/artifacts/<stage>/` | ADK artifacts / A2A artifact | ✅ 命中主流，且有 `contracts.py` 契约校验，强于多数实现 |
| 过程物料 | `missions/<id>/logs/<stage>/`（`layout.py:50-56`、`graph.py:102-107`） | SWE-agent 的 `*.log` / `trajectories/` | ✅ 命名与分流正确（回复全文/沟通记录/工具审计不进 artifacts） |
| 会话消息 | `missions/<id>/chat/<file>.jsonl` | Claude Code `*.jsonl`、Codex `rollout-*.jsonl` | ✅ JSONL 追加流是业界事实标准 |
| 记忆 | `missions/<id>/memory/`（`memory.md` + `entries/` + `context/` + `sessions/`，`mission_memory.py:83-104`） | AWP `MEMORY.md` + `memory/YYYY-MM-DD.md` | ✅ 比多数实现更分层（索引 + 全文分离） |
| 工作区 | `missions/<id>/workspace/repos/<name>/` | `/workspace` / sandbox | ✅ 可回收语义与全行业一致 |
| 输入 | 无独立目录（需求在 DB + chat） | `input/` `/tests` | ⚠️ 缺。任务目录拷走后无法还原"当时给了什么" |
| 状态 | 全局 `data/checkpoints.sqlite`，不在任务目录 | `state/`、`base_state.json + events/` | ⚠️ 缺。单任务目录不能独立重放（但集中存储更利于多副本，属合理取舍） |
| 配置/技能 | 注入到 `workspace/.cursor/rules/`、`workspace/.sortie-deck/`（`knowledge_pack.py:149`） | AgentScope `skills/`、Claude Code `projects/<p>/memory/` | ⚠️ 风险：随 `workspace/` 一起被回收，规则/知识包产物不可追溯 |
| 产物版本 | 无版本目录 | ADK `versions/{n}/` + `metadata.json` | 可选。当前按 stage 覆盖 |

## 5. 建议（按性价比排序，均非必须）

1. **不建议新增顶层目录**。五件套已对齐主流，新增 `runs/`、`sessions/` 只会与 `logs/`、`chat/` 语义重叠。
2. **补一个 `input/`（只读快照）**：进入任务时把 brief、绑定的 `context/` 文件、目标仓库 ref 落成快照。收益是可复现、可审计；成本极低。
3. **把知识包/规则注入产物落进任务目录而非 workspace**：例如 `logs/knowledge-pack/<stage>.md`，避免 workspace 回收后无法回答"当时 agent 被灌了什么规则"。
4. **`artifacts/` 增加 `manifest.json`（谁产出、时间、契约校验结果、上游产物哈希）**：比引入版本目录轻，但能支撑回放与差异定位。
5. **在文档里显式声明三档生命周期**（workspace 回收 / logs TTL / artifacts+memory 长期），当前 `docs/data-directory-lifecycle.md` 已有雏形，可与本调研合并口径。

## 6. 来源汇总

- SWE-agent Output files — https://www.swe-agent.com/latest/usage/trajectories/
- OpenHands 持久化 — https://docs.openhands.org.cn/sdk/guides/convo-persistence
- Claude Code `.claude` 目录 — https://code.claude.com/docs/en/claude-directory
- Codex CLI 数据格式 — https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/codex-history-ingest/references/codex-data-format.md
- CrewAI Memory 存储位置 — https://www.aidoczh.com/crewai/en/concepts/memory.html
- Google ADK FileArtifactService — https://adk.dev/api-reference/typescript/classes/FileArtifactService.html
- AgentScope 工作区 — https://docs.agentscope.io/zh/versions/2.0.2/building-blocks/workspace
- Harbor 沙箱标准路径 — https://deepwiki.com/laude-institute/harbor/3.4-environments
- AWP v1.0 File Structure — https://github.com/veegee82/agent-workflow-protocol/blob/main/spec/versions/1.0/file-structure.md
- A2A Artifact 概念 — https://agent2agent.info/docs/concepts/artifact/
- LangGraph Persistence — https://docs.langchain.com/oss/python/langgraph/persistence
- WeKnora 技能目录与沙箱 — https://github.com/luckydududu/WeKnora/blob/main/website-docs/03-features/22-skills-sandbox.md
