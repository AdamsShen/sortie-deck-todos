"""Just-in-time capability loading.

The model **selects, never invents**: capability needs are turned into a recall
over the toolkit library, the model picks from that candidate list only, and
every pick is runtime-probed before it is loaded. Anything that fails the probe
is *degraded with a reason* rather than breaking the stage.

Recall is a tag/keyword inverted match (no full-library scan, no LLM call), so
the hot path stays cheap; the model is only consulted when more than one
candidate can serve a need.
"""

from __future__ import annotations

import json
import re
import shutil
from typing import Any, Literal

from pydantic import BaseModel, Field

from sortie_deck.toolkit import ToolkitItem, ToolkitStore

ProbeStatus = Literal["ok", "degraded", "unavailable", "skipped"]

_SCORE_CAPABILITY_LABEL = 1.0
_SCORE_TAG = 0.6
_SCORE_TEXT = 0.25

_STOPWORDS = frozenset(
    {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "use", "using"}
)


def _tokens(text: str) -> set[str]:
    parts = re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", (text or "").lower())
    return {p for p in parts if p and p not in _STOPWORDS}


class CapabilityNeed(BaseModel):
    capability: str
    reason: str = ""


class LoadoutChoice(BaseModel):
    item_id: str
    name: str
    kind: str
    capability: str = ""
    score: float = 0.0
    probe: ProbeStatus = "skipped"
    detail: str = ""
    loaded: bool = False
    executable: bool = False
    read_only: bool = True


class LoadoutPlan(BaseModel):
    stage: str = ""
    role: str = ""
    needs: list[CapabilityNeed] = Field(default_factory=list)
    # capability → compact candidate summaries (audit trail of what was offered)
    candidates: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    selected: list[LoadoutChoice] = Field(default_factory=list)
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    manifest: str = ""
    planner: str = "heuristic"
    notes: list[str] = Field(default_factory=list)

    def loaded_item_ids(self) -> list[str]:
        return [c.item_id for c in self.selected if c.loaded]


# --- recall ---------------------------------------------------------------


def _score(item: ToolkitItem, want: set[str]) -> float:
    if not want:
        return 0.0
    # Capability labels are free text ("deploy health"), so tokenise them too
    labels = _tokens(" ".join(item.capabilities))
    tags = _tokens(" ".join(item.tags))
    text = _tokens(f"{item.name} {item.summary} {item.body}")
    score = 0.0
    for token in want:
        if token in labels:
            score += _SCORE_CAPABILITY_LABEL
        elif token in tags:
            score += _SCORE_TAG
        elif token in text:
            score += _SCORE_TEXT
    return round(score / max(1, len(want)), 3)


def recall(
    store: ToolkitStore | None,
    capability: str,
    *,
    limit: int = 5,
    kinds: list[str] | None = None,
) -> list[tuple[ToolkitItem, float]]:
    """Rank toolkit items against one capability label (cheap inverted match)."""
    if store is None:
        return []
    want = _tokens(capability)
    if not want:
        return []
    scored: list[tuple[ToolkitItem, float]] = []
    for item in store.list():
        if not item.enabled:
            continue
        if kinds and item.kind not in kinds:
            continue
        score = _score(item, want)
        if score > 0:
            scored.append((item, score))
    return sorted(scored, key=lambda pair: pair[1], reverse=True)[:limit]


# --- probe ----------------------------------------------------------------


def _first_command_token(item: ToolkitItem) -> str:
    source = item.command or item.endpoint or ""
    parts = str(source).strip().split()
    return parts[0] if parts else ""


async def probe_item(item: ToolkitItem) -> tuple[ProbeStatus, str]:
    """Cheap runtime availability check (no side effects, no network)."""
    if not item.enabled:
        return "unavailable", "item is disabled"
    if item.kind == "mcp":
        if item.transport and item.transport != "stdio":
            return "skipped", f"transport `{item.transport}` is not probed locally"
        command = _first_command_token(item)
        if not command:
            return "unavailable", "MCP item has no command"
        if shutil.which(command):
            return "ok", f"`{command}` found on PATH"
        return "unavailable", f"`{command}` not found on PATH"
    if item.kind == "tool":
        if item.runtime == "cli":
            command = _first_command_token(item)
            if not command:
                return "unavailable", "cli tool has no command"
            return (
                ("ok", f"`{command}` found on PATH")
                if shutil.which(command)
                else ("unavailable", f"`{command}` not found on PATH")
            )
        return "skipped", f"runtime `{item.runtime}` is not probed locally"
    if item.kind == "skill":
        if not item.scripts:
            return "skipped", "skill has no scripts (prompt-only)"
        if not item.executable:
            return "degraded", "skill scripts are not marked executable — prompt text only"
        return "ok", f"{len(item.scripts)} executable script(s)"
    return "skipped", "unknown toolkit kind"


# --- selection ------------------------------------------------------------

_SELECT_SYSTEM = """You are Sortie Deck's capability loadout planner.
For each capability need, pick at most one item id from the candidates offered
for that capability. You must ONLY choose ids that appear in the candidates.
Reply JSON: {"selections": [{"capability": "<need>", "item_id": "<id>"}],
"notes": ["..."]}
Return ONLY JSON.
"""


async def _select_with_llm(
    *,
    needs: list[CapabilityNeed],
    candidates: dict[str, list[dict[str, Any]]],
    brief: str,
    client: Any,
) -> tuple[dict[str, str], list[str]] | None:
    if client is None or not getattr(client, "enabled", False):
        return None
    user = json.dumps(
        {
            "brief": (brief or "")[:1500],
            "capability_needs": [n.model_dump(mode="json") for n in needs],
            "candidates": candidates,
        },
        ensure_ascii=False,
    )
    try:
        data = await client.chat_json(system=_SELECT_SYSTEM, user=user)
    except Exception:  # noqa: BLE001 — selection is best-effort
        return None
    if not isinstance(data, dict):
        return None
    picks: dict[str, str] = {}
    for row in data.get("selections") or []:
        if not isinstance(row, dict):
            continue
        capability = str(row.get("capability") or "").strip()
        item_id = str(row.get("item_id") or "").strip()
        allowed = {c["id"] for c in candidates.get(capability) or []}
        if capability and item_id and item_id in allowed:
            picks[capability] = item_id
    notes = [str(n) for n in (data.get("notes") or [])][:5]
    return picks, notes


def _heuristic_selection(candidates: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    picks: dict[str, str] = {}
    for capability, rows in candidates.items():
        if rows:
            picks[capability] = rows[0]["id"]
    return picks


# --- plan -----------------------------------------------------------------


async def build_loadout(
    *,
    store: ToolkitStore | None,
    needs: list[CapabilityNeed],
    stage: str = "",
    role: str = "",
    brief: str = "",
    use_llm: bool = True,
    client: Any = None,
    probe: bool = True,
    limit: int = 5,
) -> LoadoutPlan:
    """Recall → select → probe → load. Never raises."""
    plan = LoadoutPlan(stage=stage, role=role, needs=list(needs))
    if not needs:
        return plan
    if store is None:
        plan.notes.append("no toolkit library available — nothing loaded")
        return plan

    index: dict[str, ToolkitItem] = {}
    for need in needs:
        rows = recall(store, need.capability, limit=limit)
        plan.candidates[need.capability] = [
            {
                "id": item.id,
                "name": item.name,
                "kind": item.kind,
                "summary": item.summary[:120],
                "score": score,
                "read_only": item.read_only,
                "executable": item.executable,
            }
            for item, score in rows
        ]
        for item, _score_value in rows:
            index[item.id] = item

    picks: dict[str, str] | None = None
    # Only pay for a model call when there is actually something to choose between
    has_candidates = any(plan.candidates.get(need.capability) for need in needs)
    if use_llm and has_candidates:
        llm_result = await _select_with_llm(
            needs=needs, candidates=plan.candidates, brief=brief, client=client
        )
        if llm_result is not None:
            llm_picks, notes = llm_result
            plan.notes.extend(notes)
            if llm_picks:
                picks = llm_picks
                plan.planner = "llm"
            else:
                # A model that returns nothing usable falls back to the rules so a
                # stage is never left without the capabilities it asked for.
                plan.notes.append(
                    "model returned no usable selection — fell back to rule-based picks"
                )
    if picks is None:
        picks = _heuristic_selection(plan.candidates)

    chosen = set(picks.values())
    for capability, item_id in picks.items():
        item = index.get(item_id)
        if item is None:
            plan.rejected.append(
                {"capability": capability, "item_id": item_id, "reason": "not a recalled candidate"}
            )
            continue
        score = next(
            (
                row["score"]
                for row in plan.candidates.get(capability) or []
                if row["id"] == item_id
            ),
            0.0,
        )
        status, detail = await probe_item(item) if probe else ("skipped", "probe disabled")
        choice = LoadoutChoice(
            item_id=item.id,
            name=item.name,
            kind=item.kind,
            capability=capability,
            score=score,
            probe=status,
            detail=detail,
            loaded=status in {"ok", "skipped"},
            executable=item.executable,
            read_only=item.read_only,
        )
        plan.selected.append(choice)
        if not choice.loaded:
            plan.rejected.append(
                {"capability": capability, "item_id": item.id, "reason": detail}
            )
    # Candidates that were offered but not picked, for the audit trail
    for capability, rows in plan.candidates.items():
        for row in rows:
            if row["id"] not in chosen:
                plan.rejected.append(
                    {"capability": capability, "item_id": row["id"], "reason": "not selected"}
                )
    plan.manifest = manifest_for(plan.selected)
    return plan


def manifest_for(choices: list[LoadoutChoice]) -> str:
    """Prompt block describing what was actually loaded for this stage."""
    loaded = [c for c in choices if c.loaded]
    if not loaded:
        return ""
    lines = ["## Loaded capabilities (just-in-time)"]
    for choice in loaded:
        suffix = "" if choice.read_only else " · write ops need confirmation"
        lines.append(f"- [{choice.kind}] {choice.name} ({choice.capability}){suffix}")
    degraded = [c for c in choices if not c.loaded]
    if degraded:
        lines.append("Unavailable (degraded, do not call):")
        for choice in degraded:
            lines.append(f"- {choice.name}: {choice.detail}")
    return "\n".join(lines)
