from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from sortie_deck.models import RoleId, new_id, utc_now
from sortie_deck.persona import PersonaSpec, render_persona
from sortie_deck.templates import load_persona_spec

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
    # 编译后的 system prompt：运行时唯一来源（graph / executor / plugins 只读这个字段）
    persona: str
    # 结构化人设（角色/背景/技能/工作流/示例/约束），工坊编辑用；旧数据可为空
    persona_spec: PersonaSpec | None = None
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
    # persona 与 persona_spec 至少给一个；给了 spec 时以 spec 为准重新编译
    persona: str = ""
    persona_spec: PersonaSpec | None = None
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
    persona_spec: PersonaSpec | None = None
    tags: list[str] | None = None
    toolkit_ids: list[str] | None = None
    visibility: Visibility | None = None
    published: bool | None = None
    slot: AgentSlot | None = None
    avatar: str | None = None


def _resolve_persona(persona: str, spec: PersonaSpec | None) -> tuple[str, PersonaSpec | None]:
    """按「结构化优先」规则确定 persona 文本。

    传了 ``persona_spec`` 时以 spec 为准重新渲染，忽略原始文本；否则使用自由
    文本 ``persona``。两者都为空视为非法请求。
    """
    if spec is not None:
        text = render_persona(spec).strip()
        if not text:
            raise ValueError("persona_spec 渲染结果为空")
        return text, spec
    text = (persona or "").strip()
    if not text:
        raise ValueError("persona 与 persona_spec 至少提供一个")
    return text, None


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", text.strip().lower()).strip("-")
    return (s or "agent")[:48]


# 内置专家种子：底座人设来自 packages/templates/personas/<slot>.md，
# role_line 只覆盖「角色」首句，用于表达该专家在流水线中的特化定位。
_SEED: list[dict] = [
    {
        "slug": "prd-scout",
        "title": "PRD Scout",
        "slot": "product",
        "summary": "前场产品侦察：把客户口述压成可验收 PRD",
        "tags": ["product", "field"],
        "role_line": (
            "你是拥有 10 年经验的资深产品专家，长期在客户现场做产品侦察，"
            "擅长把混乱的口述笔记压成可验收的 PRD 与验收标准。"
        ),
    },
    {
        "slug": "design-navigator",
        "title": "Design Navigator",
        "slot": "design",
        "summary": "交互与视觉导航：把 PRD 落成可交付设计规格",
        "tags": ["design", "ui", "ux"],
        "role_line": (
            "你是拥有 10 年经验的资深产品设计师，擅长把 PRD 快速落成流程、状态"
            "与设计令牌齐全的可交付规格。"
        ),
    },
    {
        "slug": "platform-eng-raider",
        "title": "Platform Eng Raider",
        "slot": "eng",
        "summary": "平台工程突击：小 diff、可回滚、遵守仓库约定",
        "tags": ["eng", "platform"],
        "role_line": (
            "你是拥有 10 年经验的资深平台工程师，擅长在既有仓库约定下交付"
            "小 diff、可回滚、有测试兜底的实现。"
        ),
    },
    {
        "slug": "ios-raider",
        "title": "iOS Raider",
        "slot": "eng_ios",
        "summary": "iOS 突击：Swift/SwiftUI，对齐设计与验收",
        "tags": ["eng", "ios", "mobile"],
        "role_line": (
            "你是拥有 10 年经验的资深 iOS 工程师，擅长用 Swift / SwiftUI 把设计规格"
            "落成符合 HIG 与审核要求的实现。"
        ),
    },
    {
        "slug": "android-raider",
        "title": "Android Raider",
        "slot": "eng_android",
        "summary": "Android 突击：Kotlin/Jetpack，对齐设计与验收",
        "tags": ["eng", "android", "mobile"],
        "role_line": (
            "你是拥有 10 年经验的资深 Android 工程师，擅长用 Kotlin 与现代 Jetpack"
            "组件交付兼容性可控的实现。"
        ),
    },
    {
        "slug": "web-raider",
        "title": "Web Raider",
        "slot": "eng_web",
        "summary": "Web 前端突击：组件化、可访问、贴合设计",
        "tags": ["eng", "web", "frontend"],
        "role_line": (
            "你是拥有 10 年经验的资深 Web 前端工程师，擅长把设计令牌与交互规格"
            "落成可访问、可维护的组件实现。"
        ),
    },
    {
        "slug": "backend-raider",
        "title": "Backend Raider",
        "slot": "eng_backend",
        "summary": "后端突击：API、数据契约、可观测",
        "tags": ["eng", "backend", "api"],
        "role_line": (
            "你是拥有 10 年经验的资深后端工程师，擅长在保持接口兼容的前提下"
            "演进 API、数据模型与可观测能力。"
        ),
    },
    {
        "slug": "agent-raider",
        "title": "Agent Raider",
        "slot": "eng_agent",
        "summary": "Agent 开发突击：工具链、提示、评测门禁",
        "tags": ["eng", "agent", "mcp"],
        "role_line": (
            "你是拥有 10 年经验的资深 Agent 系统工程师，擅长设计可评测、"
            "可观测、成本与权限可控的智能体系统。"
        ),
    },
    {
        "slug": "qa-gatekeeper",
        "title": "QA Gatekeeper",
        "slot": "qa",
        "summary": "门禁测试官：用验收标准裁决 pass/fail",
        "tags": ["qa", "clearance"],
        "role_line": (
            "你是拥有 10 年经验的资深质量工程师，只按验收标准裁决 pass/fail，"
            "并能把失败收敛到可立即修复的粒度。"
        ),
    },
]


class AgentCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            agents: list[PublishedAgent] = []
            for seed in _SEED:
                # 底座人设来自 personas/<slot>.md，只覆盖「角色」首句表达特化定位
                spec = load_persona_spec(seed["slot"])
                if seed.get("role_line"):
                    spec.role = str(seed["role_line"])
                agents.append(
                    PublishedAgent(
                        slug=seed["slug"],
                        title=seed["title"],
                        slot=seed["slot"],
                        summary=seed["summary"],
                        persona=render_persona(spec) or f"You are the {seed['slot']} role agent.",
                        persona_spec=spec,
                        tags=seed["tags"],
                        author_name="Sortie",
                        visibility="public",
                    )
                )
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
        persona, spec = _resolve_persona(req.persona, req.persona_spec)
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
                persona=persona,
                persona_spec=spec,
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
                # 结构化编辑优先：传了 spec 就重新编译 persona；只改文本则清掉旧 spec
                spec_patch = patch.pop("persona_spec", "__absent__")
                if spec_patch != "__absent__" and spec_patch is not None:
                    spec = PersonaSpec.model_validate(spec_patch)
                    patch["persona"] = render_persona(spec)
                    patch["persona_spec"] = spec.model_dump(mode="json")
                elif spec_patch is None or ("persona" in patch and patch["persona"] is not None):
                    # 显式清空 spec，或只改自由文本：旧 spec 不再代表当前 persona
                    patch["persona_spec"] = None
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
