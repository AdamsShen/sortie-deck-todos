# 角色 Agent 设计问答与业界调研

> 本文档整理自一次围绕 Sortie Deck「角色 Agent」实现机制的问答，并附上 2024–2026 年业界的横向调研摘要，用于判断该设计是否合理。
>
> - 代码基线：`dev` 分支（2026-11-16）
> - 调研范围：LangGraph / CrewAI / AutoGen / OpenAI Agents SDK / MetaGPT / Claude Code Subagents / Strands / Anthropic 多 Agent 研究系统 / Devin·Factory·Amp / Letta / 反方观点 / 上下文工程
> - 详细调研材料：`docs/research/2026-11-16-role-agent-design/`（`outline.yaml`、`fields.yaml`、`results/`、`report.md`）

---

## 一、问答记录

### Q1：为什么称为"角色 Agent"？只是提示词、绑定的 MCP、skill 不同吗？Claude Code 怎么区分不同角色？

**结论：不只是提示词。角色具备构成一个独立 agent 的三要素；但 Claude Code 本身并不区分角色，差异是调用它之前由编排层烘焙进去的。**

#### 角色 agent 的三要素

1. **独立人格**：六段式 persona（角色 / 背景 / 技能 / 工作流 / 示例 / 约束）。运行时只消费编译后的 `persona` 字符串。
2. **独立职责契约**：persona 文件 front-matter 里的 `inputs / outputs / handoff_targets / on_fail`。

```1:9:/Library/git-root/sortie-deck-todos/packages/templates/personas/eng.md
---
role_key: eng
title: Software Engineer
inputs: [prd.md, acceptance.json, design.md, tech_design.md]
outputs: [tech_design.md, implementation.md, tasks.json, diff.patch]
handoff_targets: [qa, eng_web, eng_backend]
timeout_seconds: 900
on_fail: escalate
---
```

3. **独立绑定与上下文**：`role_toolkits`（MCP/skill）、`role_repos`（仓库）、独立产物目录与记忆。

发布后的角色被固化为 `PublishedAgent`，任务通过 `resolve_slot_map` 点名：

```44:64:/Library/git-root/sortie-deck-todos/src/sortie_deck/agents.py
class PublishedAgent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agt_"))
    slug: str
    title: str
    slot: AgentSlot
    summary: str = ""
    # 编译后的 system prompt：运行时唯一来源（graph / executor / plugins 只读这个字段）
    persona: str
    # 结构化人设（角色/背景/技能/工作流/示例/约束），工坊编辑用；旧数据可为空
    persona_spec: PersonaSpec | None = None
    tags: list[str] = Field(default_factory=list)
    toolkit_ids: list[str] = Field(default_factory=list)
    visibility: Visibility = "public"
    ...
```

#### 角色之间的真实差异

| 维度 | 是否按角色不同 | 说明 |
|---|---|---|
| persona 提示词 | 是 | 写入 `StageContext.persona` |
| MCP / skill 绑定 | 是 | `toolkit_ids` → `mcp_servers` + `toolkit_brief` |
| 仓库绑定 | 是 | `role_repos` → `StageContext.repos` |
| 交接目标 | 是 | `handoff_targets` / `next_targets` |
| 权限、禁用工具 | 否，阶段级 | `permissions` / `disallowed_tools` 由阶段规则决定 |
| 回合数、超时 | 否，阶段级 | `max_turns` / `stage_timeout` |
| 模型参数 | 否，无 per-role 配置 | 当前没有按角色配 model |
| harness 提供商 | 否，任务级且启动后锁定 | 全任务共用一种 harness |

#### Claude Code 如何区分角色

**它不区分。** 所有角色共用同一个 provider 与 agent loop，角色差异在构造 `StageContext` 时被消化成两部分：

- persona 作为 prompt 的第一段；
- MCP / 权限作为 SDK options。

```2142:2152:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/executor.py
sections: list[str] = [
    ctx.persona or f"You are the {ctx.role} agent in Sortie Deck.",
    (
        "You are running inside a Sortie Deck mission. Complete THIS stage and "
        "produce the stage artifact. Do not ask the user questions — decide and act."
    ),
    f"Stage: `{ctx.stage}` — Role: `{getattr(ctx.role, 'value', ctx.role)}`",
    location_line,
    f"Human instruction: {ctx.human_instruction or 'none'}",
    f"Mission brief:\n{ctx.brief}",
]
```

`StageContext` 的构造位置（角色差异在这里被固化）：

```2045:2080:/Library/git-root/sortie-deck-todos/src/sortie_deck/graph.py
ctx = StageContext(
    initiative_id=state["initiative_id"],
    actor_user_id=state.get("actor_user_id") or "",
    thread_id=state["thread_id"],
    stage=stage,
    role=role,
    brief=state["brief"],
    human_instruction=state.get("human_instruction"),
    artifact_dir=artifact_dir,
    workspace_dir=state.get("workspace_dir"),
    upstream_artifacts=upstream_content,
    upstream_paths=upstream_abs,
    persona=persona,
    permissions={
        "qa_attempt": state.get("qa_attempt", 1),
        **_coding_perm_from_instruction(state.get("human_instruction")),
    },
    toolkit_ids=toolkit_ids,
    toolkit_brief=toolkit_brief,
    inputs=_mission_inputs(app_cfg, state["initiative_id"]),
    repos=[RepoConfig.model_validate(r) for r in stage_repos],
    memory_brief=memory_brief,
    harness=str(state.get("harness") or ""),
    harness_locked=bool(state.get("harness_locked")),
    handoff_targets=contract_targets,
    next_targets=next_targets,
    ...
    disallowed_tools=_resolve_stage_disallowed_tools(stage, app_cfg),
)
```

---

### Q2：人格/契约/绑定是写进 `.claude/` 目录让 Claude 自己加载，还是代码拼进新会话？

**结论：主要是代码拼进新开的 Claude 会话，不是写进 `.claude/` 目录。** 代码库中没有任何把 persona 写成 `CLAUDE.md` 或 `.claude/skills` 的逻辑。

#### 人格：拼进 `query()` 的 prompt 第一段

```259:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/claude_code.py
async for message in query(prompt=prompt, options=options):
```

需要澄清一个措辞：文档里称 persona 为"编译后的 system prompt"，但实际运行时它是**首条用户消息的内容**，不是通过 Claude Code 的 memory 文件机制加载的。旧 CLI 路径更直白，直接作为命令行参数：

```822:836:/Library/git-root/sortie-deck-todos/src/sortie_deck/plugins/coding.py
prompt = (
    f"{ctx.persona}\n\n"
    f"You are running inside an isolated Sortie Deck worktree. You MAY create/edit files here.\n"
    f"Workspace (cwd): {workspace}\n"
    f"Write the implementation into this directory (e.g. index.html / src/).\n"
    ...
)
```

#### 职责契约：一半拼进 prompt，一半由编排层兑现

- persona 的 YAML front-matter 在加载时就被剥离，**不进 prompt**；只有正文进入：

```43:50:/Library/git-root/sortie-deck-todos/src/sortie_deck/templates.py
def load_persona(role: str) -> str:
    """Persona **body** for ``role`` (front-matter contract stripped).

    The YAML contract block is metadata, not prompt material — see
    :func:`load_agent_contract` for the parsed version.
    """
    _, body = load_persona_document(role)
    return body or f"You are the {role} role agent."
```

- `handoff_targets` 变成控制协议文字进 prompt，模型只在回复里声明 ```` ```control {"handoff": ...} ````，由编排层查白名单后跳转；
- `outputs` 由 `contracts.py` 在编排侧校验产物，prompt 里只给一段 `## Result` 契约说明。

#### 绑定与上下文：走 SDK options + prompt

- **MCP**：`ClaudeAgentOptions.mcp_servers` + `strict_mcp_config`，SDK 直接连接，不落盘到 `.claude.json`；
- **skill**：正文通过 `toolkit_brief` 拼进 prompt，脚本通过模型回复里的 `toolkit_intent` 围栏交给编排层执行；
- **仓库**：clone 进 worktree，cwd 指向它，仓库说明拼进 `repo_section`。

#### Claude Code 自身仍会加载的那部分

- `setting_sources` 默认 `""` → 按 CLI 默认加载 user/project/local settings。如果绑定的仓库**自带** `CLAUDE.md` 或 `.claude/`，Claude 仍会读——那是项目文件自带的，不是 Sortie Deck 写的。
- 严格 MCP 默认开启，切断 `~/.claude.json` 全局 mcpServers 的自动注入。
- prompt 明确禁止翻这些目录："Do not read or grep the orchestrator source tree, `~/.claude`… Do not load skills unrelated to this stage"。

```323:337:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/claude_code.py
def _setting_sources(raw: str | None) -> list[str] | None:
    """把配置值解析成 SDK 的 ``setting_sources``。

    - ``""``（默认）→ ``None``：按 CLI 默认加载 user/project/local；
    - ``"none"`` → ``[]``：SDK 隔离模式，本机 settings / 插件 hooks 都不加载；
    - 其余按逗号分隔取 ``user`` / ``project`` / ``local``。
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text in {"none", "off", "isolated"}:
        return []
    allowed = {"user", "project", "local"}
    picked = [part.strip() for part in text.split(",") if part.strip() in allowed]
    return picked or None
```

---

### Q3：`mcp_servers` + `strict_mcp_config=True` 是在 Claude Code 启动会话时设置的配置吗？

**结论：不是。`start_session()` 只造了一个轻量会话对象，不做任何 Claude 进程或 MCP 初始化；真正的配置在每一轮 `send()` 调用 `query()` 时构造并传入。**

```133:140:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/claude_code.py
async def start_session(
    self, *, initiative_id: str, stage: str, meta: dict[str, Any] | None = None
) -> HarnessSession:
    return HarnessSession(
        id=f"hs_cc_{utc_now().strftime('%Y%m%d%H%M%S%f')}",
        provider=self.name,
        meta={"initiative_id": initiative_id, "stage": stage, **(meta or {})},
    )
```

配置在 `_send_via_sdk` 里现造现传：

```217:247:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/claude_code.py
pack = context_pack or {}
strict = bool(settings.harness_mcp_strict)
...
options = ClaudeAgentOptions(
    cwd=cwd or None,
    permission_mode=permission_mode,
    ...
    # strict 模式下这就是本阶段 MCP 的全部；空 dict = 本阶段不挂任何 MCP
    mcp_servers=dict(pack.get("mcp_servers") or {}),
    strict_mcp_config=strict,
    resume=session.native_id or None,
    ...
)
```

由此推出的要点：

- **会话延续与 MCP 配置是两条独立的线**：续跑靠 `resume=session.native_id`，MCP 靠每轮重传的 `mcp_servers`。
- **同阶段多轮复用同一份配置**：`pack` 在阶段开始时构建一次，自审、subroom 收尾等后续轮次都传同一个 pack。
- **中途新增的 MCP 要等下一阶段**：本轮 `query()` 的 options 已定死，所以文档明确"阶段中途 pick 的 MCP 从下一阶段生效"。

---

### Q4：一个任务用一个会话吗？不断 resume 吗？人格/契约/绑定怎么持续带入？何时压缩？哪些提示词会变化？

**结论：不是全任务一个会话，而是每个阶段新建一个会话；resume 只发生在单个阶段内的多轮之间。跨阶段延续靠文件化任务记忆，不靠会话上下文。**

#### 会话粒度

`start_session()` 在 `_run_harness()` 里调用，即每个阶段各调一次：

```1237:1241:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/executor.py
session = await provider.start_session(
    initiative_id=ctx.initiative_id,
    stage=ctx.stage,
    meta={"role": str(getattr(ctx.role, "value", ctx.role))},
)
```

会话模型本身也写明 "Sessions are disposable; memory persists"：

```56:64:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/base.py
class HarnessSession(BaseModel):
    """One provider-side conversation. Sessions are disposable; memory persists."""

    id: str = Field(default_factory=lambda: f"hs_{utc_now().strftime('%Y%m%d%H%M%S%f')}")
    provider: str = ""
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    # provider-native session id (claude session_id / opencode session id / ...)
    native_id: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
```

`resume` 只用于阶段内后续轮次，共三类：

- 主体首轮 →（有 `subroom` 意图）→ 收尾轮；
- 自审多镜头第 2..N 轮；
- escalation 后带人类答复重入。

```1761:1771:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/executor.py
async def send(prompt: str) -> Any:
    # 续接同一 provider 会话：第 2..N 轮必须看到首轮实现上下文
    return await _send_turn(
        provider,
        session,
        prompt,
        cwd=cwd,
        pack=review_pack,
        extra_dirs=extra_dirs,
        on_activity=_activity_callback(ctx),
    )
```

#### 三样东西怎么持续带入

| 项 | 阶段内（同一会话多轮） | 跨阶段（新会话） |
|---|---|---|
| 独立人格 persona | 会话历史里 | 从 `role_personas[role]` 重取，按角色不同 |
| 职责契约 | 会话历史 + MCP options | front-matter 每次重新解析注入 |
| 绑定与上下文 | MCP options 随每轮重传 | `bootstrap_context()` 从文件重建 + `role_toolkits` 重新绑定 |

阶段收尾时的落盘与摘要：

```1409:1428:/Library/git-root/sortie-deck-todos/src/sortie_deck/harness_providers/executor.py
mm = get_mission_memory()
mm.record_turn(
    ctx.initiative_id,
    session.native_id or session.id,
    stage=ctx.stage,
    role=str(getattr(ctx.role, "value", ctx.role)),
    prompt=prompt,
    reply=reply.text,
    provider=harness,
)
# 角色分层摘要：协调角色 LLM 提炼决策/结论/风险，执行角色降级为增量流水
stage_summary = await build_stage_summary(
    stage=ctx.stage,
    role=str(getattr(ctx.role, "value", ctx.role)),
    reply_text=reply.text,
    harness=harness,
)
mm.append_memory(ctx.initiative_id, stage_summary)
await mm.maybe_summarize(ctx.initiative_id)
```

下个阶段新会话启动时，`bootstrap_context()` 读回 `memory.md` + 绑定文件 + 产物索引 + 最近 10 轮转录：

```205:236:/Library/git-root/sortie-deck-todos/src/sortie_deck/mission_memory.py
async def bootstrap_context(
    self,
    initiative: Initiative,
    *,
    artifact_paths: list[str] | None = None,
    budget: int | None = None,
) -> ContextPack:
    """Everything a fresh session needs: summary + bound files + artifacts."""
    from sortie_deck.settings import settings

    limit = int(budget or settings.mission_context_token_limit)
    pack = ContextPack(
        initiative_id=initiative.id,
        memory_md=self.load_memory_md(initiative.id),
        context_files=self.list_context_files(initiative.id),
        artifact_index=list(artifact_paths or []),
        session_log=self.load_session_log(initiative.id, limit_turns=10),
    )
    pack.token_estimate = estimate_tokens(pack.render())
    # Over budget → compact the rolling summary once, then rebuild.
    if pack.token_estimate > limit and self._summary_beyond_files(pack, limit):
        summarized = await self.summarize(initiative)
        ...
```

#### 上下文压缩：两层，时机与策略不同

**第一层：会话内 auto-compact（provider 侧）**
通过 `ClaudeAgentOptions.env` 注入 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`（默认 200000 token），由 Claude Code 自己决定何时压缩；压缩事件回写到 `StageResult.meta["context"]["compact_boundary"]`。

```213:216:/Library/git-root/sortie-deck-todos/src/sortie_deck/settings.py
# 会话内自动压缩窗口（token）。Claude Code 通过
# CLAUDE_CODE_AUTO_COMPACT_WINDOW 控制，官方有效范围 100K–1M；
# 0 表示不注入（沿用模型默认阈值，约 200K/967K 才触发）。
harness_auto_compact_window: int = 200000
```

**第二层：任务记忆压缩（编排层，跨阶段）**
预算 `TDT_MISSION_CONTEXT_TOKEN_LIMIT=60000`。触发时机有两个：每阶段结束时 `maybe_summarize()` 检查 `memory.md` 单文件；新会话 bootstrap 时整个 pack 超预算再压一次。

策略是"LLM 摘要 + 降级裁剪"：

```255:285:/Library/git-root/sortie-deck-todos/src/sortie_deck/mission_memory.py
async def summarize_from_text(self, initiative_id: str, memory_md: str) -> str | None:
    """LLM-summarise ``memory_md`` and write the summary back. Degrades to trim."""
    from sortie_deck.llm import LlmClient
    from sortie_deck.settings import settings

    ...
    summary = await client.chat_text(
        system=(
            "你是任务记忆压缩器。把给定滚动记忆压缩为要点式 markdown 摘要，"
            "保留：目标、已做决策、关键结论、未完成事项、风险。不新增内容。"
        ),
        user=memory_md,
        temperature=0.1,
    )
    ...
    if not summary.strip():
        # Degraded compaction: keep the head plus the most recent entries.
        entries = memory_md.split("\n## ")
        head = entries[0].strip()
        keep = entries[-4:] if len(entries) > 5 else entries[1:]
        summary = "\n## ".join([head, *keep])[
            : settings.mission_context_token_limit * _CHARS_PER_TOKEN
        ]
```

另外摘要写入按角色分层：协调角色（product/ops）走 LLM 提炼决策/结论/风险，执行角色只保留前 600 字增量流水。

#### 哪些提示词随阶段变化

`_build_prompt()` 每次都重新拼，以下段落是阶段/角色相关的动态部分：

- persona 本体（来自当前阶段角色）
- `Stage / Role`、`Human instruction`、`Mission brief`
- 工具禁用策略、`intake` 专属规格
- 上游产物摘要 + 路径（每阶段上游不同）
- 滚动记忆、绑定文件、输入索引、RAG brief、`memory_brief`
- `toolkit_brief`（随 `role_toolkits` / `stage_toolkits` 变化）
- 清单段（技术方案产清单、编码/自测执行清单）
- QA 结论块 / 用例边界 / 浏览器 runner（仅 QA 阶段）；返工边界（返工阶段）
- `repo_section`、产物索引、越界硬限制 `_SCOPE_GUARD`

不变的骨架：控制平面协议（handoff/escalate）、`## Result` 输出契约、"完成本阶段、别问用户"的总指令。

---

### Q5：`memory.md` 保存在哪里？

**结论：`<TDT_DATA_DIR>/missions/<initiative_id>/memory/memory.md`，默认在仓库的 `data/` 下。**

```291:297:/Library/git-root/sortie-deck-todos/src/sortie_deck/mission_memory.py
def get_mission_memory() -> MissionMemoryStore:
    global _STORE
    if _STORE is None:
        from sortie_deck.settings import settings

        _STORE = MissionMemoryStore(settings.missions_dir)
    return _STORE
```

```83:96:/Library/git-root/sortie-deck-todos/src/sortie_deck/mission_memory.py
def mission_dir(self, initiative_id: str) -> Path:
    return self.root / initiative_id

def memory_dir(self, initiative_id: str) -> Path:
    return self.mission_dir(initiative_id) / "memory"

...
def memory_file(self, initiative_id: str) -> Path:
    return self.memory_dir(initiative_id) / "memory.md"
```

`root` = `settings.missions_dir` = `data_dir / "missions"`；`data_dir` 默认 `REPO_ROOT / "data"`，可用 `TDT_DATA_DIR` 覆盖。

同一目录下的其他内容：

```text
data/missions/<initiative_id>/memory/
├── memory.md              # 滚动总结（append 追加 / 压缩时整篇重写）
├── sessions/<sid>.jsonl   # 每阶段会话转录（只写不读）
├── context/<name>.md      # 需求绑定文件（brief.md / plan.md / harness.md）
├── entries/               # 单条记忆全文
├── MEMORY_INDEX.md        # 单条记忆索引
└── usage.log              # 记忆取用记录
```

**一个注意点**：`get_mission_memory()` 是进程级单例，只读 `settings.missions_dir`（进程的 `TDT_DATA_DIR`），不使用 `StageContext.data_dir`。多实例或单测排查"记忆写哪去了"时，先确认这个环境变量。

---

## 二、业界调研摘要

> 完整调研见 `docs/research/2026-11-16-role-agent-design/report.md`

### 2.1 总体结论

**被测设计的核心思路是主流且合理的，与 2024–2026 年多 Agent 工程实践的收敛方向一致；但在"角色级可配置性"和"契约字段真实生效"上存在明确偏差，属于可修正的实现缺口，不是路线错误。**

### 2.2 五个设计决策对照

| # | 设计决策 | 业界对应 | 判断 |
|---|---|---|---|
| D1 | 角色 = 人格 + 契约 + 工具绑定 | CrewAI `role/goal/backstory/tools`；OpenAI Agents SDK `instructions/tools/handoffs`；Claude Code subagents 的 frontmatter + body | ✅ 主流 |
| D2 | 角色差异在编排层烘焙进 prompt/context pack，共享同一 harness | Strands "agent = model + system prompt + tools + loop"；Anthropic 子 agent 只拿任务+资源+数据源 | ✅ 主流 |
| D3 | 每阶段新建会话，跨阶段靠文件记忆 + 产物契约 | Discrete Phase Separation；Anthropic orchestrator-worker；MetaGPT"交接只认结构化文档" | ✅ 主流，有量化支持 |
| D4 | 双层压缩：provider auto-compact + 编排层 rolling summary（60K） | Anthropic compaction / note-taking；JetBrains observation masking vs LLM summarization | ✅ 方向正确 |
| D5 | 权限/模型/回合数不按角色配置 | Claude Code subagents 支持 per-agent `model`+`tools`；Amp 用不同模型档位做不同 sub-agent | ⚠️ 明确偏差 |

### 2.3 关键印证

1. **"角色=人格+契约+绑定"是业界最小共识集**。CrewAI 的 "Role-Playing as Code"、OpenAI Agents SDK 的 `instructions+tools+handoffs`、Claude Code subagents 的 frontmatter+system prompt，都是同一抽象的不同载体。
2. **"每阶段新会话"有直接对应实践**。Discrete Phase Separation 明确要求"每阶段独立会话，只有蒸馏后的 artifact 跨边界，不传完整历史"；Anthropic 子 agent 不继承他人对话、只回传压缩结果；MetaGPT 交接只认文档。另有工程总结指出，**传递完整转录的交接比传递状态摘要的任务完成率低约 34%**。
3. **"不写 `.claude/agents/` 文件"不是偏离**。落盘文件适合人类维护的固定角色（Claude Code 产品形态）；代码动态生成适合按任务组合角色（编排系统形态）。两者各有适用面。
4. **双层压缩分工正确**。会话内交给 provider、跨阶段交给编排层，避免重复压缩；按需挂 MCP 对应 MCP 官方 client best practices 的 progressive tool discovery（33 个工具就能占 8000+ token）。

### 2.4 偏差与风险

| 风险 | 说明 |
|---|---|
| **角色级 model/tools/permissions 缺失** | 无法做"QA 用强模型、检索用弱模型"的成本优化，也无法做角色级权限最小化 |
| **`tools_allowed` 声明未消费** | persona front-matter 定义了但运行时不用，属于"契约撒谎"，下游照它设计会踩空 |
| **压缩偏纯摘要** | 缺 observation masking 路线，早期精确路径、命令、错误码容易在摘要中丢失 |
| **历史会话只写不读** | `sessions/*.jsonl` 不进 prompt，长任务无法回查早期决策原文 |
| **阶段内自审共用会话** | 3–5 轮续接同一会话，存在被实现过程带偏、退化为自我确认的风险 |
| **执行角色记忆易流水账** | 只有协调角色走 LLM 提炼，执行角色固定截取 600 字 |
| **全任务锁定一种 harness** | 换来一致性，但牺牲了按阶段换能力档位的弹性 |

### 2.5 改进建议

**P0（契约一致性，低成本高收益）**

1. 让 `tools_allowed` 真正生效，或从契约中删除。
2. 补角色级 `model` / `tools` 白名单配置，由 `_build_context_pack` 透传；默认空 = 沿用现状，零回归。

**P1（上下文与记忆）**

3. 压缩策略引入 observation masking：工具输出优先用占位掩码，保留动作与结论，超阈值才走 LLM 摘要。
4. 把 `session_log` 接入 bootstrap 检索，按 stage/role 关键词召回相关历史轮次。
5. 给 qa / deploy 这类角色提供只读基线权限模板。

**P2（结构优化）**

6. 只读型自审镜头（如"测试缺口"）拆到干净子会话。
7. 执行角色流水账做"每 N 条合并一次"的轻量归纳。
8. 记录每次压缩的触发原因与丢弃内容类型，便于事后归因。

### 2.6 主要来源

- Anthropic《How we built our multi-agent research system》：https://www.cnblogs.com/informatics/p/19625900
- Anthropic《Effective Context Engineering for AI Agents》：https://www.sefism.com/resources/effective-context-engineering-for-ai-agents
- Claude Code Subagents 官方文档：https://code.claude.com/docs/ko/sub-agents
- Claude Blog《Steering Claude Code: skills, hooks, rules, subagents》：https://claude.com/fr/blog/steering-claude-code-skills-hooks-rules-subagents-and-more
- OpenAI Agents SDK：https://openai.github.io/openai-agents-python/agents/
- CrewAI 架构：https://www.crewai.com/blog/how-crewai-is-evolving-beyond-orchestration-to-create-the-most-powerful-agentic-ai-platform
- MetaGPT 解析：https://pyshine.com/metagpt/
- LangGraph Persistence：https://docs.langchain.com/oss/javascript/langgraph/durable-execution
- AutoGen / Magentic-One：https://aiwiki.ai/wiki/autogen
- AWS Strands Agents：https://aws.amazon.com/blogs/opensource/introducing-strands-agents-an-open-source-ai-agents-sdk
- Amp Code 设计哲学：https://ain3xt.com/cn/posts/20251225-amp-code-contrarian-design
- Letta (MemGPT)：https://aiwiki.ai/wiki/letta
- MCP Client Best Practices：https://modelcontextprotocol.io/docs/latest/develop/clients/client-best-practices
- Discrete Phase Separation：https://agentpatterns.ai/patterns/agent-design/discrete-phase-separation
- Cognition "Don't Build Multi-Agents" 讨论：https://toolhalla.ai/blog/single-agent-vs-multi-agent-2026
- 状态摘要交接优于裸转录：https://tryinterlock.com/blog/2026-agent-handoffs-state-summaries-beat-raw-transcripts-34.php

---

## 三、代码定位速查

| 主题 | 位置 |
|---|---|
| 角色模型定义 | `src/sortie_deck/agents.py:44-64` |
| 角色点名解析 | `src/sortie_deck/agents.py:370-380` |
| persona 文件与契约 | `packages/templates/personas/*.md:1-9` |
| front-matter 剥离 | `src/sortie_deck/templates.py:43-80` |
| StageContext 构造 | `src/sortie_deck/graph.py:2045-2080` |
| 每阶段新建会话 | `src/sortie_deck/harness_providers/executor.py:1237-1241` |
| prompt 拼装 | `src/sortie_deck/harness_providers/executor.py:2142-2261` |
| context pack 构建 | `src/sortie_deck/harness_providers/executor.py:1954-2005` |
| MCP 按阶段解析 | `src/sortie_deck/harness_providers/executor.py:2040-2058` |
| SDK options（MCP/resume） | `src/sortie_deck/harness_providers/claude_code.py:217-253` |
| session 续跑 | `src/sortie_deck/harness_providers/claude_code.py:239` |
| setting_sources | `src/sortie_deck/harness_providers/claude_code.py:323-337` |
| 旧 CLI 路径 | `src/sortie_deck/plugins/coding.py:822-868` |
| 会话对象模型 | `src/sortie_deck/harness_providers/base.py:56-64` |
| 记忆 bootstrap | `src/sortie_deck/mission_memory.py:205-236` |
| 记忆压缩 | `src/sortie_deck/mission_memory.py:255-285` |
| 阶段摘要 | `src/sortie_deck/mission_memory.py:360-395` |
| memory.md 路径 | `src/sortie_deck/mission_memory.py:83-96` |
| 压缩窗口配置 | `src/sortie_deck/settings.py:213-216` |
| 记忆预算配置 | `src/sortie_deck/settings.py:74-76` |
