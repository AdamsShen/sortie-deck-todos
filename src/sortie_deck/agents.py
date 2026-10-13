from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from sortie_deck.models import RoleId, new_id, utc_now
from sortie_deck.templates import load_persona

# Built-in slots: kept for seeding and UI defaults. The set is **no longer
# closed** — `sortie_deck.roles.RoleRegistry` owns the authoritative list and
# custom / forge-created roles are accepted as-is.
BUILTIN_AGENT_SLOTS: tuple[str, ...] = (
    "product",
    "design",
    "eng",
    "eng_ios",
    "eng_android",
    "eng_web",
    "eng_backend",
    "eng_agent",
    "qa",
    "deploy",
)


def _normalize_slot(value: str) -> str:
    key = (value or "").strip()
    if not key:
        raise ValueError("slot is required")
    return key


# Any non-empty role key is a valid slot.
AgentSlot = Annotated[str, AfterValidator(_normalize_slot)]
Visibility = Literal["public", "private"]


class PublishedAgent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agt_"))
    slug: str
    title: str
    slot: AgentSlot
    summary: str = ""
    persona: str
    tags: list[str] = Field(default_factory=list)
    toolkit_ids: list[str] = Field(default_factory=list)
    visibility: Visibility = "public"
    author_id: str = ""
    author_name: str = ""
    version: int = 1
    published: bool = True
    # preset:<slot> | URL | data URI
    avatar: str | None = None
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


class CreateAgentRequest(BaseModel):
    title: str
    slot: AgentSlot
    summary: str = ""
    persona: str
    tags: list[str] = Field(default_factory=list)
    toolkit_ids: list[str] = Field(default_factory=list)
    visibility: Visibility = "public"
    slug: str | None = None
    published: bool = True
    avatar: str | None = None


class UpdateAgentRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    persona: str | None = None
    tags: list[str] | None = None
    toolkit_ids: list[str] | None = None
    visibility: Visibility | None = None
    published: bool | None = None
    slot: AgentSlot | None = None
    avatar: str | None = None


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", text.strip().lower()).strip("-")
    return (s or "agent")[:48]


_SEED: list[dict] = [
    {
        "slug": "prd-scout",
        "title": "PRD Scout",
        "slot": "product",
        "summary": "前场产品侦察：把客户口述压成可验收 PRD",
        "tags": ["product", "field"],
        "persona": (
            "You are a product scout embedded with the customer.\n"
            "Turn messy field notes into crisp PRD + acceptance criteria.\n"
            "Prefer measurable outcomes over feature laundry lists.\n"
            + load_persona("product")
        ),
    },
    {
        "slug": "design-navigator",
        "title": "Design Navigator",
        "slot": "design",
        "summary": "交互与视觉导航：把 PRD 落成可交付设计规格",
        "tags": ["design", "ui", "ux"],
        "persona": (
            "You are the fireteam product designer.\n"
            "Produce flows, screens, and states engineers can ship without re-asking.\n"
            + load_persona("design")
        ),
    },
    {
        "slug": "platform-eng-raider",
        "title": "Platform Eng Raider",
        "slot": "eng",
        "summary": "平台工程突击：小 diff、可回滚、遵守仓库约定",
        "tags": ["eng", "platform"],
        "persona": (
            "You are a platform engineer on a forward-deployed fireteam.\n"
            "Ship small reversible diffs; document risks in implementation notes.\n"
            + load_persona("eng")
        ),
    },
    {
        "slug": "ios-raider",
        "title": "iOS Raider",
        "slot": "eng_ios",
        "summary": "iOS 突击：Swift/SwiftUI，对齐设计与验收",
        "tags": ["eng", "ios", "mobile"],
        "persona": (
            "You are the fireteam iOS engineer.\n"
            + load_persona("eng_ios")
        ),
    },
    {
        "slug": "android-raider",
        "title": "Android Raider",
        "slot": "eng_android",
        "summary": "Android 突击：Kotlin/Jetpack，对齐设计与验收",
        "tags": ["eng", "android", "mobile"],
        "persona": (
            "You are the fireteam Android engineer.\n"
            + load_persona("eng_android")
        ),
    },
    {
        "slug": "web-raider",
        "title": "Web Raider",
        "slot": "eng_web",
        "summary": "Web 前端突击：组件化、可访问、贴合设计",
        "tags": ["eng", "web", "frontend"],
        "persona": (
            "You are the fireteam web engineer.\n"
            + load_persona("eng_web")
        ),
    },
    {
        "slug": "backend-raider",
        "title": "Backend Raider",
        "slot": "eng_backend",
        "summary": "后端突击：API、数据契约、可观测",
        "tags": ["eng", "backend", "api"],
        "persona": (
            "You are the fireteam backend engineer.\n"
            + load_persona("eng_backend")
        ),
    },
    {
        "slug": "agent-raider",
        "title": "Agent Raider",
        "slot": "eng_agent",
        "summary": "Agent 开发突击：工具链、提示、评测门禁",
        "tags": ["eng", "agent", "mcp"],
        "persona": (
            "You are the fireteam agent engineer.\n"
            + load_persona("eng_agent")
        ),
    },
    {
        "slug": "qa-gatekeeper",
        "title": "QA Gatekeeper",
        "slot": "qa",
        "summary": "门禁测试官：用验收标准裁决 pass/fail",
        "tags": ["qa", "clearance"],
        "persona": (
            "You are the fireteam QA gatekeeper.\n"
            "Judge only against acceptance criteria; failures must be actionable.\n"
            + load_persona("qa")
        ),
    },
]


class AgentCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            agents = [
                PublishedAgent(
                    slug=s["slug"],
                    title=s["title"],
                    slot=s["slot"],
                    summary=s["summary"],
                    persona=s["persona"],
                    tags=s["tags"],
                    author_name="Sortie",
                    visibility="public",
                )
                for s in _SEED
            ]
            self._write(agents)

    def _read(self) -> list[PublishedAgent]:
        return [PublishedAgent.model_validate(x) for x in json.loads(self.path.read_text(encoding="utf-8"))]

    def _write(self, agents: list[PublishedAgent]) -> None:
        self.path.write_text(
            json.dumps([a.model_dump(mode="json") for a in agents], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(
        self,
        *,
        q: str | None = None,
        slot: str | None = None,
        user_id: str | None = None,
        include_private: bool = True,
    ) -> list[PublishedAgent]:
        with self._lock:
            agents = self._read()
        out: list[PublishedAgent] = []
        for a in agents:
            if not a.published and a.author_id != user_id:
                continue
            if a.visibility == "private" and a.author_id != user_id:
                continue
            if slot and a.slot != slot:
                continue
            if q:
                needle = q.lower()
                blob = f"{a.title} {a.summary} {' '.join(a.tags)} {a.slug}".lower()
                if needle not in blob:
                    continue
            out.append(a)
        return sorted(out, key=lambda x: x.updated_at, reverse=True)

    def get(self, agent_id: str) -> PublishedAgent:
        for a in self._read():
            if a.id == agent_id or a.slug == agent_id:
                return a
        raise KeyError(agent_id)

    def create(self, req: CreateAgentRequest, *, author_id: str, author_name: str) -> PublishedAgent:
        slug = _slugify(req.slug or req.title)
        with self._lock:
            agents = self._read()
            existing = {a.slug for a in agents}
            base = slug
            n = 2
            while slug in existing:
                slug = f"{base}-{n}"
                n += 1
            agent = PublishedAgent(
                slug=slug,
                title=req.title.strip(),
                slot=req.slot,
                summary=req.summary.strip(),
                persona=req.persona.strip(),
                tags=[t.strip() for t in req.tags if t.strip()],
                toolkit_ids=list(req.toolkit_ids),
                visibility=req.visibility,
                published=req.published,
                author_id=author_id,
                author_name=author_name,
                avatar=req.avatar or f"preset:{req.slot}",
            )
            agents.append(agent)
            self._write(agents)
            return agent

    def update(self, agent_id: str, req: UpdateAgentRequest, *, editor_id: str, is_admin: bool) -> PublishedAgent:
        with self._lock:
            agents = self._read()
            for i, a in enumerate(agents):
                if a.id != agent_id and a.slug != agent_id:
                    continue
                if a.author_id and a.author_id != editor_id and not is_admin:
                    raise PermissionError("only author or admin can edit")
                data = a.model_dump()
                patch = req.model_dump(exclude_unset=True)
                for key in ("title", "summary", "persona"):
                    if key in patch and patch[key] is not None:
                        patch[key] = patch[key].strip()
                if "tags" in patch and patch["tags"] is not None:
                    patch["tags"] = [t.strip() for t in patch["tags"] if t.strip()]
                if "toolkit_ids" in patch and patch["toolkit_ids"] is not None:
                    patch["toolkit_ids"] = [str(x) for x in patch["toolkit_ids"]]
                data.update(patch)
                data["version"] = int(data.get("version", 1)) + 1
                data["updated_at"] = utc_now().isoformat()
                agents[i] = PublishedAgent.model_validate(data)
                self._write(agents)
                return agents[i]
        raise KeyError(agent_id)

    def delete(self, agent_id: str, *, editor_id: str, is_admin: bool) -> None:
        with self._lock:
            agents = self._read()
            keep: list[PublishedAgent] = []
            found = None
            for a in agents:
                if a.id == agent_id or a.slug == agent_id:
                    found = a
                    continue
                keep.append(a)
            if not found:
                raise KeyError(agent_id)
            if found.author_id and found.author_id != editor_id and not is_admin:
                raise PermissionError("only author or admin can delete")
            self._write(keep)

    def resolve_slot_map(self, role_agent_ids: dict[str, str]) -> dict[RoleId, PublishedAgent]:
        """Map pipeline slot -> published agent for mission loadout."""
        resolved: dict[RoleId, PublishedAgent] = {}
        for slot, agent_id in role_agent_ids.items():
            if not agent_id:
                continue
            agent = self.get(agent_id)
            if agent.slot != slot:
                raise ValueError(f"agent {agent.slug} is slot={agent.slot}, not {slot}")
            resolved[RoleId(slot)] = agent
        return resolved
