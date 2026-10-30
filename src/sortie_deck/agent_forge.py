"""Agent forge — generate a missing role agent (persona + contract) on demand.

Flow: ``propose`` (LLM or template) → self-check → ``pending-review`` →
human edits/approves → :class:`~sortie_deck.roles.RoleRegistry` registration
(and optional publish into the agent catalog).

Generated agents are **never** auto-published: an unverified persona going
straight into a live pipeline is exactly the failure mode the review flow
exists to prevent.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from sortie_deck.agent_contract import AgentContract, contract_from_mapping
from sortie_deck.agents import AgentCatalog, CreateAgentRequest, PublishedAgent
from sortie_deck.models import new_id, utc_now
from sortie_deck.persona import (
    PersonaExample,
    PersonaSpec,
    parse_persona,
    persona_quality_issues,
    render_persona,
    spec_from_mapping,
)
from sortie_deck.roles import RoleRegistry, RoleSpec, normalize_role_key

DraftStatus = Literal["pending-review", "approved", "rejected"]

_FORGE_SYSTEM = """你是 Sortie Deck 的角色锻造官（agent forge），负责为缺失的职能
设计一个可以直接上岗的专家 Agent。

角色人设按提示词工程的六段式编写（结构参考 DeepSeek 提示词库的功能型模板）：
  角色：一句话身份，必须以「你是拥有 10 年经验的资深<领域>专家」开头
  背景：2-4 条，说明业务环境、上下游、产物用途与时间预算
  技能：4-6 条「能力名：一句解释」，包含该职能需要的知识储备
  工作流：3-5 步执行顺序，动词开头
  示例：1-3 条，写清典型输入与期望输出
  约束：4-6 条硬约束，含禁止事项、边界与失败处理

按职能选择侧重：
  - 决策/写作型（产品、设计、发布等）：定位、能力、知识储备、输入输出说明
  - 评测/裁决型（QA 等）：判定标准前置，pass/fail 必须可观测、可复现
  - 工程实现型：工作流固定为「读产物 → 说明问题与方案 → 最小 diff 实现 → 自检」
  - 结构化产出型：在约束中写死字段、类型、缺失值约定，只输出约定格式
  - 人设/提示词生成型：写清身份、经历、语言风格与知识边界

Return JSON:
  role_key: short snake_case ascii id (e.g. parenting, stocks_research)
  title: human readable title
  summary: one line description of what this agent delivers
  persona: {
    "role": "...", "background": "...", "skills": ["..."], "workflow": ["..."],
    "examples": [{"situation": "...", "response": "..."}], "constraints": ["..."]
  }
  inputs: array of artifact kinds it reads
  outputs: array of artifact kinds it produces (must not be empty)
  handoff_targets: array of role ids it may hand off to (subset of the role catalog)
  tags: array of short tags

persona 的语言与 capability 保持一致；示例必须具体到可以照着做。
Never invent role ids outside the provided catalog for handoff_targets.
Return ONLY JSON.
"""


def _template_spec(*, title: str, role_key: str) -> PersonaSpec:
    """LLM 不可用时的六段式兜底人设，保证所有产出结构一致。"""
    return PersonaSpec(
        role=f"你是拥有 10 年经验的资深{title}专家，负责 `{role_key}` 职责的端到端交付。",
        background=(
            f"- 你在 Sortie Deck 的 AI Native 交付流水线中承担 `{role_key}` 角色，"
            "上游产物是你的输入，下游角色会直接消费你的交付物。\n"
            "- 单阶段预算有限：优先交付可验证的主路径，其余标注为后续跟进。"
        ),
        skills=[
            "问题澄清：把模糊诉求拆成可执行的目标、范围与边界。",
            "方案设计：给出可落地的方法、步骤与风险取舍。",
            "交付自检：按约定产物交付，并在交付前逐条自检完整性。",
        ],
        workflow=[
            "读上游产物与任务简报，确认输入与验收要求。",
            "列出关键未知，用最小可行假设补齐并标注待确认。",
            "产出约定交付物，逐条对照验收要求自检。",
        ],
        examples=[
            PersonaExample(
                situation="上游只给了一句模糊的需求描述",
                response=(
                    "先补齐目标、范围、边界三项假设，再给出可验收的交付物骨架，"
                    "并列出待确认清单。"
                ),
            )
        ],
        constraints=[
            "只产出约定范围内的交付物，不擅自扩展需求。",
            "不臆造上游未提供的事实；不确定的信息标注「待确认」。",
            "交付前必须自检产物完整性，不把未完成的内容写成已完成。",
        ],
    )


def _persona_from_payload(
    payload: Any, *, title: str, role_key: str
) -> tuple[str, PersonaSpec]:
    """把 LLM 返回的 persona（dict 或文本）统一成 (编译后文本, 结构化 spec)。"""
    spec = spec_from_mapping(payload)
    if spec is None and isinstance(payload, str) and payload.strip():
        spec = parse_persona(payload)
    if spec is None or not render_persona(spec).strip():
        spec = _template_spec(title=title, role_key=role_key)
    return render_persona(spec), spec


class ForgeRequest(BaseModel):
    capability: str
    title: str = ""
    role_key: str | None = None
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    requested_by: str = "Operator"


class ForgedAgentDraft(BaseModel):
    id: str = Field(default_factory=lambda: new_id("fgd_"))
    role_key: str
    title: str
    summary: str = ""
    persona: str = ""
    # 结构化人设：审核台按六段式编辑，approve 时随 persona 一起发布
    persona_spec: PersonaSpec | None = None
    contract: AgentContract
    tags: list[str] = Field(default_factory=list)
    status: DraftStatus = "pending-review"
    # Self-check findings surfaced in the review UI; approval is still allowed
    issues: list[str] = Field(default_factory=list)
    # Near-duplicate existing roles (forge de-duplication aid)
    similar: list[dict[str, Any]] = Field(default_factory=list)
    generator: Literal["llm", "template"] = "template"
    requested_by: str = ""
    review_note: str = ""
    reviewed_by: str = ""
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


class ForgeStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[ForgedAgentDraft]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [ForgedAgentDraft.model_validate(x) for x in raw]

    def _write(self, rows: list[ForgedAgentDraft]) -> None:
        self.path.write_text(
            json.dumps([r.model_dump(mode="json") for r in rows], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(self, *, status: str | None = None) -> list[ForgedAgentDraft]:
        rows = self._read()
        if status:
            rows = [r for r in rows if r.status == status]
        return sorted(rows, key=lambda r: r.updated_at, reverse=True)

    def get(self, draft_id: str) -> ForgedAgentDraft:
        for row in self._read():
            if row.id == draft_id:
                return row
        raise KeyError(draft_id)

    def save(self, draft: ForgedAgentDraft) -> ForgedAgentDraft:
        draft.updated_at = utc_now().isoformat()
        with self._lock:
            rows = self._read()
            for i, row in enumerate(rows):
                if row.id == draft.id:
                    rows[i] = draft
                    self._write(rows)
                    return draft
            rows.append(draft)
            self._write(rows)
        return draft


def _unique_role_key(roles: RoleRegistry, base: str) -> str:
    key = normalize_role_key(base)
    if not key:
        return ""
    if not roles.is_known(key):
        return key
    n = 2
    while roles.is_known(f"{key}_{n}"):
        n += 1
    return f"{key}_{n}"


class AgentForge:
    """Propose / review / publish role agents."""

    def __init__(
        self,
        *,
        store: ForgeStore,
        roles: RoleRegistry,
        agents: AgentCatalog | None = None,
        llm: Any | None = None,
    ) -> None:
        self.store = store
        self.roles = roles
        self.agents = agents
        self._llm = llm

    def _client(self) -> Any:
        if self._llm is not None:
            return self._llm
        from sortie_deck.llm import LlmClient

        # 尊重 TDT_LLM_CLI_FALLBACK：硬编码 True 会让"关闭 CLI 回退"失效
        return LlmClient()

    # --- propose -------------------------------------------------------

    async def propose(self, req: ForgeRequest) -> ForgedAgentDraft:
        """Create a pending-review draft for the requested capability."""
        catalog = self.roles.known_role_keys()
        draft_data, generator = await self._draft_from_llm(req, catalog)
        if draft_data is None:
            draft_data = self._draft_from_template(req)
            generator = "template"

        title = str(draft_data.get("title") or req.title or req.capability).strip()
        role_key = _unique_role_key(self.roles, req.role_key or draft_data.get("role_key") or title)
        if not role_key:
            role_key = new_id("role_")

        contract = contract_from_mapping({**draft_data, "role_key": role_key}, role_key=role_key)
        if contract is None:
            contract = AgentContract(
                role_key=role_key,
                title=title,
                outputs=[f"{role_key}_deliverable.md"],
                on_fail="escalate",
            )

        persona_text, persona_spec = _persona_from_payload(
            draft_data.get("persona"), title=title, role_key=role_key
        )

        draft = ForgedAgentDraft(
            role_key=role_key,
            title=title,
            summary=str(draft_data.get("summary") or req.summary or req.capability).strip(),
            persona=persona_text,
            persona_spec=persona_spec,
            contract=contract,
            tags=[str(t).strip() for t in (draft_data.get("tags") or req.tags) if str(t).strip()],
            generator=generator,  # type: ignore[arg-type]
            requested_by=req.requested_by,
        )
        draft.issues = self._self_check(draft)
        draft.similar = [
            {"role_key": spec.role_key, "title": spec.title, "score": round(score, 2)}
            for spec, score in self.roles.find_similar(
                title=title, summary=draft.summary, tags=draft.tags
            )[:3]
        ]
        return self.store.save(draft)

    async def _draft_from_llm(
        self, req: ForgeRequest, catalog: list[str]
    ) -> tuple[dict[str, Any] | None, str]:
        client = self._client()
        if not getattr(client, "enabled", False):
            return None, "template"
        user = json.dumps(
            {
                "capability": req.capability,
                "title": req.title,
                "summary": req.summary,
                "tags": req.tags,
                "role_catalog": catalog,
            },
            ensure_ascii=False,
        )
        try:
            data = await client.chat_json(system=_FORGE_SYSTEM, user=user)
        except Exception:  # noqa: BLE001 — forge falls back to a template draft
            return None, "template"
        if not isinstance(data, dict) or not data:
            return None, "template"
        return data, "llm"

    def _draft_from_template(self, req: ForgeRequest) -> dict[str, Any]:
        title = (req.title or req.capability).strip()
        key = normalize_role_key(req.role_key or title) or "role"
        return {
            "role_key": req.role_key or title,
            "title": title,
            "summary": req.summary or req.capability,
            "persona": _template_spec(title=title, role_key=key).model_dump(mode="json"),
            "outputs": [f"{key}_deliverable.md"],
            "tags": list(req.tags),
        }

    def _self_check(self, draft: ForgedAgentDraft) -> list[str]:
        issues = list(draft.contract.problems())
        known = self.roles.keys()
        unknown = [t for t in draft.contract.handoff_targets if t not in known and t != draft.role_key]
        if unknown:
            issues.append(f"handoff_targets not in role catalog: {sorted(unknown)}")
        if draft.contract.role_key != draft.role_key:
            issues.append("contract role_key does not match draft role_key")
        # 人设质量门禁：六段式缺失、缺 10 年经验、示例/约束不足都进入审核台
        spec = draft.persona_spec or parse_persona(draft.persona)
        issues.extend(persona_quality_issues(spec))
        return issues

    # --- review --------------------------------------------------------

    def list(self, *, status: str | None = None) -> list[ForgedAgentDraft]:
        return self.store.list(status=status)

    def get(self, draft_id: str) -> ForgedAgentDraft:
        return self.store.get(draft_id)

    def update(self, draft_id: str, patch: dict[str, Any]) -> ForgedAgentDraft:
        """Human edits before approval (title / summary / persona / spec / contract / tags)."""
        draft = self.store.get(draft_id)
        if draft.status == "approved":
            raise ValueError("approved drafts are immutable — register a new version instead")
        data = draft.model_dump()
        spec_patch = patch.get("persona_spec", "__absent__")
        if spec_patch != "__absent__" and spec_patch is not None:
            try:
                spec = PersonaSpec.model_validate(spec_patch)
            except ValidationError as exc:
                raise ValueError(f"invalid persona_spec: {exc}") from exc
            data["persona_spec"] = spec.model_dump(mode="json")
            data["persona"] = render_persona(spec)
        elif spec_patch is None:
            data["persona_spec"] = None
        elif "persona" in patch and patch["persona"] is not None:
            # 原文编辑：同步解析出结构化视图，保证审核台两种模式一致
            data["persona"] = patch["persona"]
            data["persona_spec"] = parse_persona(str(patch["persona"])).model_dump(mode="json")
        for key in ("title", "summary", "persona", "role_key", "tags"):
            if key in patch and patch[key] is not None:
                data[key] = patch[key]
        if isinstance(patch.get("contract"), dict):
            try:
                AgentContract.model_validate(patch["contract"])
            except ValidationError as exc:
                raise ValueError(f"invalid contract: {exc}") from exc
            data["contract"] = patch["contract"]
        try:
            updated = ForgedAgentDraft.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"invalid draft: {exc}") from exc
        updated.role_key = normalize_role_key(updated.role_key) or updated.role_key
        # keep the contract keyed to the draft so handoff validation stays coherent
        updated.contract.role_key = updated.role_key
        updated.issues = self._self_check(updated)
        return self.store.save(updated)

    def reject(self, draft_id: str, *, note: str = "", reviewer: str = "") -> ForgedAgentDraft:
        draft = self.store.get(draft_id)
        draft.status = "rejected"
        draft.review_note = note
        draft.reviewed_by = reviewer
        return self.store.save(draft)

    def approve(
        self,
        draft_id: str,
        *,
        reviewer: str = "",
        register_role: bool = True,
        publish_agent: bool = False,
        toolkit_ids: list[str] | None = None,
        coding: bool = False,
    ) -> tuple[ForgedAgentDraft, RoleSpec | None, PublishedAgent | None]:
        """Approve a draft: register the role and optionally publish the agent."""
        draft = self.store.get(draft_id)
        if draft.status == "rejected":
            raise ValueError("cannot approve a rejected draft")

        role_spec: RoleSpec | None = None
        if register_role:
            role_spec = self.roles.register(
                RoleSpec(
                    role_key=draft.role_key,
                    title=draft.title,
                    summary=draft.summary,
                    source="forge",
                    coding=coding,
                    handoff_targets=list(draft.contract.handoff_targets),
                    tags=list(draft.tags),
                )
            )

        published: PublishedAgent | None = None
        if publish_agent and self.agents is not None:
            published = self.agents.create(
                CreateAgentRequest(
                    title=draft.title,
                    slot=draft.role_key,
                    summary=draft.summary,
                    persona=draft.persona,
                    persona_spec=draft.persona_spec,
                    tags=[*draft.tags, "forge"],
                    toolkit_ids=list(toolkit_ids or []),
                    visibility="public",
                    published=True,
                ),
                author_id="forge",
                author_name=reviewer or "Agent Forge",
            )

        draft.status = "approved"
        draft.reviewed_by = reviewer
        draft.issues = self._self_check(draft)
        self.store.save(draft)
        return draft, role_spec, published


_FORGE: AgentForge | None = None
_FORGE_LOCK = threading.Lock()


def get_agent_forge() -> AgentForge:
    global _FORGE
    if _FORGE is None:
        with _FORGE_LOCK:
            if _FORGE is None:
                from sortie_deck.roles import get_role_registry
                from sortie_deck.settings import settings

                _FORGE = AgentForge(
                    store=ForgeStore(settings.data_dir / "forge.json"),
                    roles=get_role_registry(),
                    agents=AgentCatalog(settings.agents_db),
                )
    return _FORGE


def reset_agent_forge() -> None:
    global _FORGE
    _FORGE = None
