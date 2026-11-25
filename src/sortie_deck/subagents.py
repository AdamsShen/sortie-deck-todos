"""全局子代理池（canonical spec）。

设计边界（见 ``docs/plans/2026-11-24-全局子代理池.md``）：

1. **供给在编排层，决策在 harness**。这里只负责「这个 stage 允许有哪些子代理」，
   用不用、用哪个、传什么、返回什么，全部由 harness 里的父模型自己判断；
   我们唯一能施加影响的手段是 ``description`` 措辞与提示词里的委派判据。
2. **默认池只有 ``scout``（只读）**。写型子代理在并行波次已经负责并行的前提下
   收益不确定，且同一 worktree 多写者会互相覆盖，必须由模板显式声明 + 开关放行。
3. **能力用标签表达，不写 harness 工具名**。同一个 ``read_files`` 在 claude 是
   ``Read``、在 dsh 是 ``tool-fs``；具体映射由 provider 侧完成，模板与代码不感知
   vendor。
4. **空池 = 零行为**：``subagent_enabled=false`` 或本阶段没有任何只读能力时，
   既不注入 agent 定义，也不注入委派指引。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# 能力标签：只描述「能做什么」，不绑定任何 harness 的工具名
Capability = Literal["read_files", "search_code", "web", "shell", "write_files"]

# 只读能力集合：scout 只能用这些。``shell`` 会写文件（>、tee、npm i），归为写能力
READONLY_CAPABILITIES: tuple[str, ...] = ("read_files", "search_code", "web")

# harness → 子代理供给能力。
# native      = 能把池渲染成该 harness 的原生 agent 定义；
# prompt_only = 只能靠提示词引导（是否有内置委派工具取决于 harness 自身）；
# unsupported = 完全没有委派能力，此时必须空池——给出指引只会让模型假装委派并编造结果
SUPPORT_BY_HARNESS: dict[str, str] = {
    "claude_code": "native",
    "codex": "native",
    "opencode": "native",
    "dsh": "prompt_only",
    "cursor": "unsupported",
    "mock": "unsupported",
}

# capability → 各 harness 工具名。缺失表示该 harness 不支持按工具裁剪（继承父级）
CAPABILITY_TOOLS: dict[str, dict[str, list[str]]] = {
    "claude_code": {
        "read_files": ["Read"],
        "search_code": ["Grep", "Glob"],
        "web": ["WebSearch", "WebFetch"],
        "shell": ["Bash"],
        "write_files": ["Write", "Edit"],
    },
    # codex 的自定义代理只吃 name/description/developer_instructions + 模型字段，
    # 工具集继承父级，无法按 agent 裁剪（官方文档）
    "codex": {},
    # dsh 的 tool-subagent 定义机制待实测（见计划 P4），先不声明工具映射
    "dsh": {},
    "opencode": {
        "read_files": ["read"],
        "search_code": ["grep", "glob"],
        "web": ["webfetch"],
        "shell": ["bash"],
        "write_files": ["edit", "write"],
    },
    "cursor": {},
}

_SCOUT_DESCRIPTION = (
    "只读侦察：在代码库、文档或网页里定位目标并返回带出处的结论。"
    "当你需要读很多文件/检索很多结果、但最终只需要一小段结论时使用。"
)

_SCOUT_PROMPT = (
    "你是「{stage}」阶段（角色 {role}）的只读侦察子代理。\n"
    "只做三件事：定位（找到相关文件/来源）、取证（摘出关键片段与行号）、给结论。\n"
    "禁止修改任何文件；不要复述需求；不要输出过程流水账。\n"
    "输出格式：结论（3 行以内）→ 证据清单（`文件:行` 或来源 URL）→ 不确定点。\n"
    "你的输出会直接作为父代理的输入，只写它需要的信息。"
)

_GUIDANCE = """### 可委派的子代理（可选）
{agents}

委派判据（满足其一才委派，否则自己做——每次委派都是一次独立的模型调用）：
1. 需要大量检索/读文件，但结论只需要一小段；
2. 纯只读探查，不需要改文件；
3. 可以拆成互不依赖的子问题（可并行）。

委派时必须写清：目标、可读范围、验收标准；只回结论与证据，不要过程。"""


class SubagentSpec(BaseModel):
    """一个可委派子代理的规范定义（provider 无关）。"""

    id: str
    # 路由信号：harness 的父模型只凭这段文字决定用不用。写"何时使用"，不写"是什么"
    description: str = ""
    prompt: str = ""
    mode: Literal["readonly", "write"] = "readonly"
    capabilities: list[str] = Field(default_factory=list)
    max_turns: int = 12
    # 空 = 继承父模型；harness 支持再透传
    model: str = ""
    source: Literal["builtin", "template", "role"] = "builtin"


class StageSubagentPlan(BaseModel):
    """本阶段可用的子代理池 + 注入提示词用的委派指引。"""

    stage: str = ""
    harness: str = ""
    agents: list[SubagentSpec] = Field(default_factory=list)
    # 全只读才允许并行（写型并行会互相覆盖同一 worktree）
    parallel: bool = False
    guidance: str = ""
    # disabled / native / prompt_only / unsupported：由 provider 回填，不静默降级
    support: str = ""
    notes: list[str] = Field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.agents)


def tool_names(harness: str, capabilities: list[str]) -> list[str]:
    """把能力标签翻译成某 harness 的工具名；不支持按工具裁剪时返回空表。"""
    mapping = CAPABILITY_TOOLS.get(str(harness or "").strip(), {})
    if not mapping:
        return []
    names: list[str] = []
    for cap in capabilities:
        for name in mapping.get(str(cap), []):
            if name not in names:
                names.append(name)
    return names


def _stage_readonly_capabilities(ctx: Any) -> set[str]:
    """本阶段 scout 可用的只读能力。

    口径与阶段工具策略一致：被 ``disallowed_tools`` 整体禁掉的能力不再供给，
    否则会给出一个"看起来能用、实际一调就被拒"的子代理。
    没有 ``disallowed_tools`` 的旧调用方按「全部只读能力可用」处理。
    """
    banned = {str(name or "").strip() for name in (getattr(ctx, "disallowed_tools", None) or [])}
    caps: set[str] = set()

    def _any_available(names: list[str]) -> bool:
        # 没有任何禁用项时视作全部可用（旧调用方 / mock 场景）
        return not banned or any(name not in banned for name in names)

    if _any_available(["Read"]):
        caps.add("read_files")
    if _any_available(["Grep", "Glob"]) or _any_available(["Read"]):
        # Grep/Glob 被禁但 Read 还在时，仍可以按路径读取——只是效率低，不算无能力
        caps.add("search_code")
    if _any_available(["WebSearch", "WebFetch"]):
        # 编码阶段同样需要查文档/查报错，web 不按阶段区分
        caps.add("web")
    return caps & set(READONLY_CAPABILITIES)


def _from_declared(
    row: dict[str, Any], *, allow_write: bool, max_turns: int, notes: list[str]
) -> SubagentSpec | None:
    """模板声明 → Spec；缺 id/description 的条目直接丢弃并记原因。"""
    agent_id = str(row.get("id") or "").strip()
    description = str(row.get("description") or "").strip()
    if not agent_id or not description:
        notes.append(f"忽略模板子代理（缺 id 或 description）: {row!r}"[:300])
        return None
    caps = [str(c).strip() for c in (row.get("capabilities") or []) if str(c).strip()]
    mode = str(row.get("mode") or "readonly").strip().lower()
    if mode == "write" and not allow_write:
        notes.append(f"子代理 `{agent_id}` 声明了 write，但 subagent_allow_write=false，降级为 readonly")
        mode = "readonly"
        caps = [c for c in caps if c in READONLY_CAPABILITIES]
    if mode == "readonly":
        # 只读契约硬约束：shell 与写文件能力一律剔除
        kept = [c for c in caps if c in READONLY_CAPABILITIES]
        if len(kept) != len(caps):
            notes.append(f"子代理 `{agent_id}` 的写能力已被剔除（mode=readonly）")
        caps = kept
    return SubagentSpec(
        id=agent_id,
        description=description,
        prompt=str(row.get("prompt") or "").strip() or description,
        mode="write" if mode == "write" else "readonly",
        capabilities=caps,
        max_turns=int(row.get("max_turns") or max_turns),
        model=str(row.get("model") or ""),
        source="template",
    )


def _merge(base: list[SubagentSpec], extra: list[SubagentSpec]) -> list[SubagentSpec]:
    """按 id 合并：模板声明覆盖同名内置，顺序保持声明序。"""
    by_id = {spec.id: spec for spec in base}
    order = [spec.id for spec in base]
    for spec in extra:
        if spec.id not in by_id:
            order.append(spec.id)
        by_id[spec.id] = spec
    return [by_id[key] for key in order]


def render_guidance(agents: list[SubagentSpec]) -> str:
    """委派指引：只在池非空时返回正文（空池必须零注入）。"""
    if not agents:
        return ""
    lines = [f"- `{spec.id}`（{'只读' if spec.mode == 'readonly' else '可写'}）：{spec.description}" for spec in agents]
    return _GUIDANCE.format(agents="\n".join(lines))


def resolve_subagents(ctx: Any, *, declared: list[dict[str, Any]] | None = None) -> StageSubagentPlan:
    """按阶段派生可用子代理池。

    输入：``StageContext``（stage / role / harness / disallowed_tools）+ 模板声明。
    输出：``StageSubagentPlan``；关闭开关或没有只读能力时返回空池（不注入任何内容）。

    默认只给 ``scout``：编码、调研、办公三类 stage 都至少有读文件能力，业务语义
    由模板声明补充，不在代码里硬编码角色。
    """
    from sortie_deck.settings import settings

    stage = str(getattr(ctx, "stage", "") or "")
    harness = str(getattr(ctx, "harness", "") or "")
    plan = StageSubagentPlan(stage=stage, harness=harness)
    if not bool(getattr(settings, "subagent_enabled", False)):
        plan.support = "disabled"
        return plan
    # 没有委派能力的 harness 直接空池：给了指引模型只会"假装委派"
    plan.support = SUPPORT_BY_HARNESS.get(harness.strip(), "prompt_only")
    if plan.support == "unsupported":
        plan.notes.append(f"harness `{harness}` 不支持子代理，本阶段空池")
        return plan

    max_turns = int(getattr(settings, "subagent_max_turns", 12) or 12)
    agents: list[SubagentSpec] = []
    caps = _stage_readonly_capabilities(ctx)
    if caps:
        role = str(getattr(ctx, "role", "") or "")
        role_name = str(getattr(getattr(ctx, "role", None), "value", role))
        agents.append(
            SubagentSpec(
                id="scout",
                description=_SCOUT_DESCRIPTION,
                prompt=_SCOUT_PROMPT.format(stage=stage, role=role_name),
                capabilities=[cap for cap in READONLY_CAPABILITIES if cap in caps],
                max_turns=max_turns,
            )
        )
    else:
        plan.notes.append("本阶段没有可用只读能力，scout 不供给")

    if declared:
        extra = [
            spec
            for row in declared
            if isinstance(row, dict)
            for spec in [
                _from_declared(
                    row,
                    allow_write=bool(getattr(settings, "subagent_allow_write", False)),
                    max_turns=max_turns,
                    notes=plan.notes,
                )
            ]
            if spec is not None
        ]
        agents = _merge(agents, extra)

    # description 预算：harness 会把所有 description 塞进主上下文（Claude 侧 15k token
    # 硬告警），超出就按声明序截断，宁可少给也不要撞预算
    budget = int(getattr(settings, "subagent_description_budget", 12_000) or 12_000)
    kept: list[SubagentSpec] = []
    used = 0
    for spec in agents:
        cost = len(spec.description) + len(spec.id) + 8
        if kept and used + cost > budget:
            plan.notes.append(f"子代理 description 超预算（{budget}），已丢弃 `{spec.id}` 及其后条目")
            break
        kept.append(spec)
        used += cost

    plan.agents = kept
    plan.parallel = bool(
        kept
        and all(spec.mode == "readonly" for spec in kept)
        and bool(getattr(settings, "subagent_parallel", True))
    )
    plan.guidance = render_guidance(kept)
    return plan
