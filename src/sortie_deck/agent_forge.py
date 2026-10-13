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
from sortie_deck.roles import RoleRegistry, RoleSpec, normalize_role_key

DraftStatus = Literal["pending-review", "approved", "rejected"]

_FORGE_SYSTEM = """You are Sortie Deck's agent forge.
Design ONE role agent that covers the requested capability. Return JSON:
  role_key: short snake_case ascii id (e.g. parenting, stocks_research)
  title: human readable title
  summary: one line description of what this agent delivers
  persona: the system prompt for the agent, 2-6 short lines, second person
  inputs: array of artifact kinds it reads
  outputs: array of artifact kinds it produces (must not be empty)
  handoff_targets: array of role ids it may hand off to (subset of the role catalog)
  tags: array of short tags
Never invent role ids outside the provided catalog for handoff_targets.
Return ONLY JSON.
"""

_TEMPLATE_PERSONA = (
    "You are the {title} agent on an AI Native delivery fireteam.\n"
    "Own the `{role_key}` responsibility end to end: clarify the goal, produce the\n"
    "agreed deliverables, and hand off with enough context for the next role.\n"
    "Prefer concrete, verifiable output over commentary.\n"
)


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

        return LlmClient(cli_fallback=True)

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

        draft = ForgedAgentDraft(
            role_key=role_key,
            title=title,
            summary=str(draft_data.get("summary") or req.summary or req.capability).strip(),
            persona=str(draft_data.get("persona") or "").strip()
            or _TEMPLATE_PERSONA.format(title=title, role_key=role_key),
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
        return {
            "role_key": req.role_key or title,
            "title": title,
            "summary": req.summary or req.capability,
            "persona": _TEMPLATE_PERSONA.format(
                title=title, role_key=normalize_role_key(req.role_key or title) or "role"
            ),
            "outputs": [f"{(normalize_role_key(req.role_key or title) or 'role')}_deliverable.md"],
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
        return issues

    # --- review --------------------------------------------------------

    def list(self, *, status: str | None = None) -> list[ForgedAgentDraft]:
        return self.store.list(status=status)

    def get(self, draft_id: str) -> ForgedAgentDraft:
        return self.store.get(draft_id)

    def update(self, draft_id: str, patch: dict[str, Any]) -> ForgedAgentDraft:
        """Human edits before approval (title / summary / persona / contract / tags)."""
        draft = self.store.get(draft_id)
        if draft.status == "approved":
            raise ValueError("approved drafts are immutable — register a new version instead")
        data = draft.model_dump()
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
