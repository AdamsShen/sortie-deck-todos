from __future__ import annotations

import json
from typing import Any

from sortie_deck.llm import LlmClient
from sortie_deck.models import RoleId
from sortie_deck.pipeline import (
    PLANNER_AGENT,
    PLANNER_TITLE,
    TRACK_FILES,
    catalog_by_id,
    normalize_specs,
    plan_pipeline,
    resolve_track,
)

_SYSTEM = """You are Sortie Deck's pipeline_planner agent.
Propose a software delivery pipeline as JSON with keys:
  track_hint: one of express|standard|default
  rationale: short Chinese or English explanation
  stages: array of {id, label?, role?, hitl_after?, parallel_group?, branch?, depends_on?}
Only use stage ids from the provided catalog. Always include intake first and done last.
When multiple eng_* specialists (eng_web, eng_backend, eng_ios, …) should run together,
give EACH its own tech design stage (`<role>_tech_design`, owned by that role) in an
`eng_planning` parallel wave, then independent build→selftest branch chains in an
`eng_delivery` parallel wave, then eng_integrate.
Do NOT emit a single shared `eng_tech_design`; do NOT use a shared `eng_selftest`.
Human confirmation is mandatory after your proposal — never assume start.
Return ONLY JSON.
"""

_LOADOUT_SYSTEM_TEMPLATE = """You are Sortie Deck's squad loadout planner.
Given a mission brief, a role catalog, and proposed pipeline stages, return JSON:
  needed_slots: array of role ids chosen from the provided role catalog
  missing_slots: array (at most 2) of roles the mission clearly needs but the catalog lacks,
                 each {"role_key": "<snake_case ascii>", "title": "<name>",
                       "capability": "<what it delivers>", "why": "<why the catalog cannot cover it>"}
  rationale: short explanation in the same language as the brief
Only include needed_slots that the stages / brief actually need. Always include product, qa,
deploy when those stages exist.
Use specific eng_* roles (eng_backend, eng_web, eng_ios, eng_android, eng_agent) whenever the
requirement needs more than one engineering discipline — never collapse them into the generic eng.
Reserve the generic eng role for cases where exactly ONE engineering role can deliver the whole
build.
needed_slots must ONLY contain catalog roles — never invent an id there. missing_slots is the
only place for a genuinely new discipline (e.g. a domain specialist); use it sparingly (at most
2), never for something an existing role already covers, and never duplicate a catalog role.
Missing slots become review-pending agent drafts, so a wrong one costs a human review.
Return ONLY JSON.
"""

_COLLAB_SYSTEM = """You are Sortie Deck's mission intake assessor.
Decide whether a request needs a *multi-agent* squad at all, and if so which roles.
Return JSON:
  multi_agent: bool — false when a single role can deliver the whole request
  roles: array of role ids from the provided role catalog
  rationale: short explanation in the same language as the brief
When multi_agent is false, roles must be empty or contain exactly one role.
When the request needs more than one engineering discipline, list each specific eng_* role
(eng_backend, eng_web, eng_ios, eng_android, eng_agent) separately — do not use the generic
eng role to cover multiple disciplines. Only fall back to generic eng when exactly one
engineering role can handle the entire request.
Never invent a role id that is not in the catalog.
Return ONLY JSON.
"""

# Built-in fallback used when the role registry is unavailable.
_BUILTIN_SLOTS: frozenset[str] = frozenset(m.value for m in RoleId)

# 单次建队最多登记多少个「目录外角色」（每个都会产生一条待审草稿，多了就失控）
_MAX_MISSING_SLOTS = 2


def valid_loadout_slots() -> set[str]:
    """Role keys the loadout planner may pick — registry-driven, not a closed set."""
    try:
        from sortie_deck.roles import get_role_registry

        return set(get_role_registry().known_role_keys())
    except Exception:  # noqa: BLE001 — registry failure must not block planning
        return set(_BUILTIN_SLOTS)


def clean_missing_slots(raw: Any, allowed: set[str]) -> list[dict[str, Any]]:
    """校验规划官提出的「目录外角色」，只保留确实需要新建的条目。

    输入示例（模型返回）：``[{"role_key": "pet_funeral", "title": "宠物殡葬策划",
    "capability": "...", "why": "..."}]``
    输出示例：``[{"role_key": "pet_funeral", "title": "...", "capability": "...", "why": "..."}]``

    规则（缺失即降级，绝不阻塞建队）：
    - role_key 必须是 snake_case 可归一化的键；与目录内角色同名 → 丢弃（应走 needed_slots）
    - 与已有角色语义高度相似 → 丢弃（find_similar 去重，避免重复造轮子）
    - 最多 ``_MAX_MISSING_SLOTS`` 条，重复键去重
    """
    rows = raw if isinstance(raw, list) else []
    if not rows:
        return []
    try:
        from sortie_deck.roles import get_role_registry, normalize_role_key

        registry = get_role_registry()
    except Exception:  # noqa: BLE001 — registry unavailable → cannot validate, so refuse all
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if isinstance(item, str):
            item = {"role_key": item}
        if not isinstance(item, dict):
            continue
        key = normalize_role_key(str(item.get("role_key") or item.get("slot") or ""))
        title = str(item.get("title") or "").strip()
        if not key or key in seen or key in allowed:
            continue
        capability = str(item.get("capability") or "").strip()
        why = str(item.get("why") or "").strip()
        similar = registry.find_similar(title=title or key, summary=capability)[:1]
        if similar and similar[0][1] >= 0.55:
            # 高度相似说明目录里已有能胜任的角色，交给 needed_slots 而非新建
            continue
        seen.add(key)
        out.append(
            {
                "role_key": key,
                "title": title or key,
                "capability": capability or why or title or key,
                "why": why,
            }
        )
        if len(out) >= _MAX_MISSING_SLOTS:
            break
    return out


async def plan_pipeline_llm(
    *,
    title: str = "",
    brief: str = "",
    hint_track: str | None = None,
    complexity: str = "auto",
    client: LlmClient | None = None,
) -> dict[str, Any] | None:
    """Optional LLM planner. Returns None on failure so caller can fall back."""
    llm = client or LlmClient()
    if not llm.enabled:
        return None
    catalog = catalog_by_id()
    catalog_summary = [
        {"id": s["id"], "label": s.get("label"), "role": s.get("role")} for s in catalog.values()
    ]
    hint = hint_track if hint_track in TRACK_FILES else None
    user = json.dumps(
        {
            "title": title,
            "brief": brief,
            "complexity": complexity,
            "hint_track": hint,
            "catalog": catalog_summary,
        },
        ensure_ascii=False,
    )
    try:
        data = await llm.chat_json(system=_SYSTEM, user=user)
    except Exception:
        return None

    track = data.get("track_hint")
    if track not in TRACK_FILES:
        track, _ = resolve_track(complexity=complexity, brief=brief, title=title)
    try:
        stages = normalize_specs(data.get("stages") or [])
    except Exception:
        return None
    if not stages:
        return None

    return {
        "agent": PLANNER_AGENT,
        "agent_title": PLANNER_TITLE,
        "track_hint": track,
        "rationale": data.get("rationale")
        or f"LLM planner proposed `{track}` stages pending human confirm.",
        "notes": [f"planner=llm:{llm.backend}"],
        "stages": stages,
        "status": "pending",
        "planner": "llm",
        "planner_backend": llm.backend,
        "confidence": float(data.get("confidence") or 0.6),
    }


async def propose_loadout_llm(
    *,
    brief: str = "",
    stages: list[dict[str, Any]] | None = None,
    client: LlmClient | None = None,
) -> dict[str, Any] | None:
    """LLM squad loadout. Returns None on failure."""
    llm = client or LlmClient()
    if not llm.enabled:
        return None
    allowed = valid_loadout_slots()
    user = json.dumps(
        {
            "brief": brief,
            "role_catalog": sorted(allowed),
            "stages": [
                {"id": s.get("id"), "role": s.get("role"), "label": s.get("label")}
                for s in (stages or [])
                if isinstance(s, dict)
            ],
        },
        ensure_ascii=False,
    )
    try:
        data = await llm.chat_json(system=_LOADOUT_SYSTEM_TEMPLATE, user=user)
    except Exception:
        return None
    slots = [str(s) for s in (data.get("needed_slots") or []) if str(s) in allowed]
    missing = clean_missing_slots(data.get("missing_slots"), allowed)
    if not slots and not missing:
        return None
    seen: set[str] = set()
    ordered: list[str] = []
    for s in slots:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return {
        "needed_slots": ordered,
        "missing_slots": missing,
        "rationale": str(data.get("rationale") or ""),
        "planner": "llm",
        "planner_backend": llm.backend,
    }


async def assess_collaboration(
    *,
    title: str = "",
    brief: str = "",
    use_llm: bool = True,
    client: LlmClient | None = None,
) -> dict[str, Any]:
    """Decide whether the mission needs a multi-agent squad, and which roles.

    The model may only *narrow* the squad — the fallback keeps today's behaviour
    (multi-agent) so an unavailable LLM never silently degrades a mission to a
    single agent. Role ids are validated against the registry catalog.
    """
    allowed = valid_loadout_slots()
    proposal = plan_pipeline(title=title, brief=brief, complexity="auto")
    heuristic_roles: list[str] = []
    for stage in proposal.get("stages") or []:
        role = str((stage or {}).get("role") or "").strip()
        if role and role in allowed and role not in heuristic_roles:
            heuristic_roles.append(role)
    for required in ("product", "qa", "deploy"):
        if required in allowed and required not in heuristic_roles:
            heuristic_roles.append(required)
    fallback = {
        "multi_agent": True,
        "roles": heuristic_roles,
        "rationale": "heuristic: pipeline stages imply a multi-agent squad",
        "planner": "heuristic",
        "planner_backend": None,
    }
    if not use_llm:
        return fallback
    llm = client or LlmClient()
    if not llm.enabled:
        return fallback
    user = json.dumps(
        {
            "title": title,
            "brief": brief,
            "role_catalog": sorted(allowed),
            "stage_roles": heuristic_roles,
        },
        ensure_ascii=False,
    )
    try:
        data = await llm.chat_json(system=_COLLAB_SYSTEM, user=user)
    except Exception:
        return fallback
    if not isinstance(data, dict):
        return fallback

    roles: list[str] = []
    for item in data.get("roles") or []:
        key = str(item).strip()
        if key and key in allowed and key not in roles:
            roles.append(key)
    multi = bool(data.get("multi_agent"))
    if not multi:
        roles = roles[:1]
    if not roles:
        roles = heuristic_roles[:1] if not multi else heuristic_roles
    return {
        "multi_agent": multi,
        "roles": roles,
        "rationale": str(data.get("rationale") or ""),
        "planner": "llm",
        "planner_backend": llm.backend,
    }


async def plan_pipeline_smart(
    *,
    title: str = "",
    brief: str = "",
    hint_track: str | None = None,
    complexity: str = "auto",
    use_llm: bool = False,
    client: LlmClient | None = None,
) -> dict[str, Any]:
    if use_llm:
        llm_proposal = await plan_pipeline_llm(
            title=title,
            brief=brief,
            hint_track=hint_track,
            complexity=complexity,
            client=client,
        )
        if llm_proposal:
            return llm_proposal
    proposal = plan_pipeline(
        title=title,
        brief=brief,
        hint_track=hint_track,
        complexity=complexity,
    )
    proposal["planner"] = "heuristic"
    proposal.setdefault("confidence", 0.85)
    return proposal
