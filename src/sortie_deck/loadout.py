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
import logging
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
    capability: str = "",
    *,
    limit: int = 5,
    kinds: list[str] | None = None,
    query: str = "",
    allowed_ids: set[str] | None = None,
) -> list[tuple[ToolkitItem, float]]:
    """Rank toolkit items against one capability label (cheap inverted match).

    capability / query 二者至少传一个：capability 是预定义的能力标签（模式 1），
    query 是模型在推理循环内给出的自由文本（模式 2 元工具），两者词元合并打分。
    allowed_ids 传入时做硬过滤（org_policy 白名单），None 表示不过滤。

    召回后端（方案 A）：TDT_TOOLKIT_RECALL_BACKEND=weknora 时先走 WeKnora
    hybrid-search（向量+BM25→RRF→rerank，条目经 toolkit_index 镜像）；未配置、
    抛异常、或命中为空（镜像刚建尚未解析完成 / 查询词只与本地标签匹配）时，
    一律降级本地三层加权打分，保证召回不因外部检索缺席而归零。
    """
    if store is None:
        return []
    combined = f"{capability} {query}".strip()
    if combined:
        try:
            from sortie_deck.toolkit_index import mirror_available, weknora_recall

            if mirror_available():
                rows = weknora_recall(
                    store,
                    combined,
                    kinds=kinds,
                    allowed_ids=allowed_ids,
                    limit=limit,
                )
                # 命中为空不等于"本地也没有"：镜像解析是异步的，新建条目短时间检索不到；
                # 放行到本地打分兜底（本地永远持有全量条目）
                if rows:
                    return rows
        except Exception as exc:  # noqa: BLE001 — 混合检索失败降级本地打分
            logging.getLogger(__name__).warning(
                "toolkit weknora recall failed, fallback to local scoring: %s", exc
            )
    want = _tokens(combined)
    if not want:
        return []
    scored: list[tuple[ToolkitItem, float]] = []
    for item in store.list():
        if not item.enabled:
            continue
        if kinds and item.kind not in kinds:
            continue
        if allowed_ids is not None and item.id not in allowed_ids:
            continue
        score = _score(item, want)
        if score > 0:
            scored.append((item, score))
    return sorted(scored, key=lambda pair: pair[1], reverse=True)[:limit]


# --- in-loop meta-tool (mode 2) ---------------------------------------------
# 预算常量：对齐方案文档 3.3 节（每次搜索 ≤5 候选、单阶段累计上浮 ≤8 条、
# skill 正文上浮 ≤5000 字符），防止元工具路径把阶段上下文撑爆。
SEARCH_TOP_K = 5
PICK_BUDGET_PER_STAGE = 8
SUMMARY_FLOAT_LIMIT = 200
SKILL_BODY_FLOAT_LIMIT = 5000
# skill_run 意图：每阶段最多执行 4 次脚本；回填 brief 的单次输出摘要上限
SKILL_RUN_BUDGET_PER_STAGE = 4
SKILL_OUTPUT_FLOAT_LIMIT = 8000


def summarize_candidates(pairs: list[tuple[ToolkitItem, float]]) -> list[dict[str, str]]:
    """把召回结果压成给模型看的候选摘要（toolkit_search 意图的返回载荷）。

    输入: recall() 的输出，如 [(ToolkitItem(id="tk_x", kind="mcp"), 0.9), ...]
    输出: [{"id": "tk_x", "kind": "mcp", "name": "filesystem",
            "summary": "官方 filesystem MCP，读写仓库沙箱"}]
    summary 截断到 SUMMARY_FLOAT_LIMIT，防止单条候选污染上下文。
    """
    out: list[dict[str, str]] = []
    for item, _score in pairs:
        out.append(
            {
                "id": item.id,
                "kind": item.kind,
                "name": item.name,
                "summary": (item.summary or "")[:SUMMARY_FLOAT_LIMIT],
            }
        )
    return out


def unfreeze_item(item: ToolkitItem) -> str:
    """把一个被点名的条目"解冻"为注入阶段上下文的文本（toolkit_pick 的产物）。

    tool → 端点与用途；mcp → 连接方式（env 永不展开明文）；skill → 正文，
    超过 SKILL_BODY_FLOAT_LIMIT 截断并标注。脚本只执行不读入（不在此展开）。
    输入: ToolkitItem；输出: 多行文本，直接追加进 toolkit_brief。
    """
    if item.kind == "tool":
        return f"- [tool/{item.runtime}] {item.name}: {item.endpoint or item.summary}"
    if item.kind == "mcp":
        target = item.url or " ".join([item.command, *item.args]).strip() or item.summary
        return f"- [mcp/{item.transport}] {item.name}: {target}"
    body = (item.body or "").strip()
    if len(body) > SKILL_BODY_FLOAT_LIMIT:
        body = body[:SKILL_BODY_FLOAT_LIMIT] + "\n… [已截断，完整正文请经 skill 执行获取]"
    return f"- [skill] {item.name}\n{body}"


async def pick_candidate(
    store: ToolkitStore | None,
    item_id: str,
    *,
    allowed_ids: set[str] | None = None,
    probe: bool = True,
) -> tuple[ToolkitItem | None, str]:
    """校验一次 toolkit_pick 点名，返回 (条目, 失败原因)。

    失败原因用于回传给模型（可换条目或换词重搜），永远不抛异常——
    pick 失败只降级不中断阶段。校验顺序：存在 → 启用 → 白名单 → 探活。
    """
    if store is None:
        return None, "toolkit store unavailable"
    try:
        item = store.get(item_id)
    except KeyError:
        return None, f"item `{item_id}` not found"
    if not item.enabled:
        return None, f"item `{item_id}` is disabled"
    if allowed_ids is not None and item.id not in allowed_ids:
        return None, f"item `{item_id}` is outside the org allowlist"
    if probe:
        status, detail = await probe_item(item)
        if status not in {"ok", "skipped"}:
            return None, f"probe {status}: {detail}"
    return item, ""


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
