"""Pipeline tracks + planner agent for propose → confirm → edit."""

from __future__ import annotations

import re
from typing import Any, Literal

from sortie_deck.templates import TEMPLATES_DIR, load_pipeline_template
from sortie_deck.parallel import (
    _PLATFORM_BRANCH,
    attach_depends_on,
    selftest_id_for,
)

PipelineTrack = Literal["express", "standard", "default", "custom"]
Complexity = Literal["auto", "trivial", "normal", "complex"]

TRACK_FILES = {
    "default": "default_pipeline.yaml",
    "express": "express_pipeline.yaml",
    "standard": "standard_pipeline.yaml",
    # Non-engineering requests (研究 / 育儿 / 股票 …) run on the same machinery
    "research": "research_pipeline.yaml",
}

COMPLEXITY_TO_TRACK: dict[str, PipelineTrack] = {
    "trivial": "express",
    "normal": "standard",
    "complex": "standard",
}

PLANNER_AGENT = "pipeline_planner"
PLANNER_TITLE = "航线规划官"

# Heuristic signals for auto track selection
_TRIVIAL_PATTERNS = [
    r"一行",
    r"一字",
    r"改个?字",
    r"小改",
    r"微调",
    r"hotfix",
    r"hot[\s-]?fix",
    r"typo",
    r"错别字",
    r"文案",
    r"copy\s*fix",
    r"one[\s-]?liner?",
    r"one[\s-]?line",
    r"trivial",
    r"quick\s*fix",
    r"快速修复",
    r"紧急修复",
    r"配置变更",
    r"config\s*only",
    r"bump\s+version",
    r"改个?色",
    r"改文案",
]

_COMPLEX_PATTERNS = [
    r"新功能",
    r"从零",
    r"重构",
    r"迁移",
    r"架构",
    r"多端",
    r"联调多方",
    r"合规",
    r"支付",
    r"权限体系",
    r"rewrite",
    r"migration",
    r"epic",
    r"平台级",
]

_NEED_CASES = [r"用例", r"测试计划", r"test\s*case", r"acceptance"]
_NEED_INTEGRATE = [r"联调", r"多服务", r"integrate", r"跨端"]
_SKIP_PRD = [r"无需\s*prd", r"跳过\s*prd", r"no\s*prd"]
_NEED_DESIGN = [
    r"设计",
    r"\bui\b",
    r"\bux\b",
    r"figma",
    r"视觉",
    r"交互",
    r"界面",
    r"原型",
    r"design",
]
_SKIP_DESIGN = [r"无需\s*设计", r"跳过\s*设计", r"no\s*design", r"skip\s*design"]

# Specialist eng stages — inserted when brief mentions the platform
_PLATFORM_SIGNALS: list[tuple[str, list[str]]] = [
    ("eng_ios", [r"\bios\b", r"iphone", r"ipad", r"swiftui", r"\bswift\b", r"uikit"]),
    ("eng_android", [r"android", r"kotlin", r"jetpack", r"compose"]),
    ("eng_web", [r"\bweb\b", r"前端", r"react", r"vue", r"next\.?js", r"typescript", r"css"]),
    ("eng_backend", [r"后端", r"backend", r"api\s*服务", r"fastapi", r"spring", r"微服务", r"服务端"]),
    ("eng_agent", [r"\bagent\b", r"智能体", r"langgraph", r"agent\s*开发", r"mcp\s*工具"]),
]

# Catalog-only stages (not always on a base track; planner / editor can insert)
_SELFTEST_EXTRA: list[dict[str, Any]] = [
    {
        "name": "eng_ios_selftest",
        "role": "eng_ios",
        "label": "iOS 自测",
        "label_en": "iOS Self-test",
        "output_contract": ["selftest.md"],
    },
    {
        "name": "eng_android_selftest",
        "role": "eng_android",
        "label": "Android 自测",
        "label_en": "Android Self-test",
        "output_contract": ["selftest.md"],
    },
    {
        "name": "eng_web_selftest",
        "role": "eng_web",
        "label": "Web 自测",
        "label_en": "Web Self-test",
        "output_contract": ["selftest.md"],
    },
    {
        "name": "eng_backend_selftest",
        "role": "eng_backend",
        "label": "后端自测",
        "label_en": "Backend Self-test",
        "output_contract": ["selftest.md"],
    },
    {
        "name": "eng_agent_selftest",
        "role": "eng_agent",
        "label": "Agent 自测",
        "label_en": "Agent Self-test",
        "output_contract": ["selftest.md"],
    },
]

_EXTRA_STAGES: list[dict[str, Any]] = [
    {
        "name": "eng_ios",
        "role": "eng_ios",
        "label": "iOS 开发",
        "label_en": "iOS Build",
        "output_contract": ["implementation.md", "tasks.json", "diff.patch"],
    },
    {
        "name": "eng_android",
        "role": "eng_android",
        "label": "Android 开发",
        "label_en": "Android Build",
        "output_contract": ["implementation.md", "tasks.json", "diff.patch"],
    },
    {
        "name": "eng_web",
        "role": "eng_web",
        "label": "Web 开发",
        "label_en": "Web Build",
        "output_contract": ["implementation.md", "tasks.json", "diff.patch"],
    },
    {
        "name": "eng_backend",
        "role": "eng_backend",
        "label": "后端开发",
        "label_en": "Backend Build",
        "output_contract": ["implementation.md", "tasks.json", "diff.patch"],
    },
    {
        "name": "eng_agent",
        "role": "eng_agent",
        "label": "Agent 开发",
        "label_en": "Agent Build",
        "output_contract": ["implementation.md", "tasks.json", "diff.patch"],
    },
    *_SELFTEST_EXTRA,
]


def _stage_from_yaml(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": s["name"],
        "name": s["name"],
        "label": s.get("label") or s["name"],
        "label_en": s.get("label_en") or s.get("label") or s["name"],
        "role": s.get("role") or "product",
        "hitl_after": bool(s.get("hitl_after")),
        "on_fail": s.get("on_fail"),
        "on_reject": s.get("on_reject"),
        "output_contract": list(s.get("output_contract") or []),
        "executor": s.get("executor"),
    }


def stage_catalog() -> list[dict[str, Any]]:
    """Union of all known stages across tracks (canonical defs)."""
    by_id: dict[str, dict[str, Any]] = {}
    # Prefer standard (richest) then default then express
    for track in ("standard", "default", "express"):
        tpl = load_pipeline_template(track)
        for s in tpl.get("stages", []):
            sid = s["name"]
            if sid not in by_id:
                by_id[sid] = _stage_from_yaml(s)
    for s in _EXTRA_STAGES:
        sid = s["name"]
        if sid not in by_id:
            by_id[sid] = _stage_from_yaml(s)
    # Stable order
    order = [
        "intake",
        "product_prd",
        "eng_tech_design",
        "design_ui",
        "qa_cases",
        "eng_implement",
        "eng_ios",
        "eng_android",
        "eng_web",
        "eng_backend",
        "eng_agent",
        "eng_ios_selftest",
        "eng_android_selftest",
        "eng_web_selftest",
        "eng_backend_selftest",
        "eng_agent_selftest",
        "eng_selftest",
        "eng_integrate",
        "qa_handoff",
        "qa_verify",
        "deploy_preview",
        "done",
    ]
    out = [by_id[i] for i in order if i in by_id]
    for sid, spec in by_id.items():
        if sid not in {s["id"] for s in out}:
            out.append(spec)
    return out


def catalog_by_id() -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in stage_catalog()}


def list_tracks() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # Built-in order first, then any extra registered track (e.g. research)
    order = [
        "express",
        "standard",
        "default",
        *[k for k in TRACK_FILES if k not in {"express", "standard", "default"}],
    ]
    for track in order:
        tpl = load_pipeline_template(track)
        stages = [_stage_from_yaml(s) for s in tpl.get("stages", [])]
        out.append(
            {
                "id": track,
                "name": tpl.get("name", track),
                "description": tpl.get("description", ""),
                "complexity": tpl.get("complexity", "normal"),
                "stages": stages,
                "stage_count": len(stages),
            }
        )
    return out


def stage_rail(track: str, *, lang: str = "zh") -> list[dict[str, str]]:
    tpl = load_pipeline_template(track)
    rail: list[dict[str, str]] = []
    for s in tpl.get("stages", []):
        label = s.get("label_en") if lang == "en" else s.get("label")
        rail.append({"id": s["name"], "label": label or s["name"]})
    return rail


def specs_to_rail(specs: list[dict[str, Any]], *, lang: str = "zh") -> list[dict[str, str]]:
    rail: list[dict[str, str]] = []
    for s in specs:
        if s.get("enabled") is False:
            continue
        label = s.get("label_en") if lang == "en" else s.get("label")
        entry: dict[str, Any] = {
            "id": s.get("id") or s.get("name"),
            "label": label or s.get("id") or "?",
        }
        if s.get("label_en"):
            entry["label_en"] = s["label_en"]
        if s.get("role"):
            entry["role"] = s["role"]
        if s.get("parallel_group"):
            entry["parallel_group"] = s["parallel_group"]
        if s.get("branch"):
            entry["branch"] = s["branch"]
        if s.get("depends_on"):
            entry["depends_on"] = list(s["depends_on"])
        rail.append(entry)
    return rail


def resolve_track(
    *,
    track: str | None = None,
    complexity: Complexity | str = "auto",
    brief: str = "",
    title: str = "",
) -> tuple[str, str]:
    if track in TRACK_FILES:
        return track, f"explicit track={track}"

    if complexity and complexity != "auto" and complexity in COMPLEXITY_TO_TRACK:
        mapped = COMPLEXITY_TO_TRACK[complexity]
        return mapped, f"complexity={complexity} → {mapped}"

    suggested, reason = suggest_track(f"{title}\n{brief}")
    return suggested, reason


def suggest_track(text: str) -> tuple[PipelineTrack, str]:
    blob = (text or "").strip().lower()
    if not blob:
        return "standard", "empty brief → standard"

    for pat in _TRIVIAL_PATTERNS:
        if re.search(pat, blob, re.IGNORECASE):
            return "express", f"matched trivial signal /{pat}/ → express"

    for pat in _COMPLEX_PATTERNS:
        if re.search(pat, blob, re.IGNORECASE):
            return "standard", f"matched complex signal /{pat}/ → standard"

    compact = re.sub(r"\s+", "", blob)
    if len(compact) <= 24 and not re.search(r"需求|功能|模块|系统", blob):
        return "express", "short brief → express"

    return "standard", "default → standard"


def _yaml_stages(track: str) -> list[dict[str, Any]]:
    tpl = load_pipeline_template(track)
    return [_stage_from_yaml(s) for s in tpl.get("stages", [])]


def plan_pipeline(
    *,
    title: str = "",
    brief: str = "",
    hint_track: str | None = None,
    complexity: Complexity | str = "auto",
) -> dict[str, Any]:
    """Internal pipeline_planner agent — proposes stages pending human confirm."""
    text = f"{title}\n{brief}"
    if hint_track in TRACK_FILES:
        track, reason = hint_track, f"hint track={hint_track}"
    else:
        track, reason = resolve_track(complexity=complexity, brief=brief, title=title)

    stages = _yaml_stages(track)
    notes: list[str] = [reason]

    # Adaptive add/remove on top of base track
    ids = [s["id"] for s in stages]
    catalog = catalog_by_id()

    def ensure(stage_id: str, after: str | None = None) -> None:
        nonlocal stages, ids
        if stage_id in ids or stage_id not in catalog:
            return
        spec = dict(catalog[stage_id])
        if after and after in ids:
            idx = ids.index(after) + 1
            stages.insert(idx, spec)
            ids.insert(idx, stage_id)
        else:
            # before done
            if "done" in ids:
                idx = ids.index("done")
                stages.insert(idx, spec)
                ids.insert(idx, stage_id)
            else:
                stages.append(spec)
                ids.append(stage_id)
        notes.append(f"+{stage_id}")

    def drop(stage_id: str) -> None:
        nonlocal stages, ids
        if stage_id not in ids or stage_id in {"intake", "done"}:
            return
        stages = [s for s in stages if s["id"] != stage_id]
        ids = [s["id"] for s in stages]
        notes.append(f"-{stage_id}")

    blob = text.lower()
    if track == "express":
        if any(re.search(p, blob, re.IGNORECASE) for p in _NEED_CASES):
            ensure("qa_cases", after="intake")
            notes.append("小改但提到用例 → 加用例节点")
        if any(re.search(p, blob, re.IGNORECASE) for p in _NEED_INTEGRATE):
            ensure("eng_integrate", after="eng_implement")
        if any(re.search(p, blob, re.IGNORECASE) for p in _NEED_DESIGN):
            after = "product_prd" if "product_prd" in ids else "intake"
            ensure("design_ui", after=after)
            notes.append("快轨但提到设计 → 加设计节点")
    elif track in {"standard", "default"}:
        if any(re.search(p, blob, re.IGNORECASE) for p in _SKIP_PRD):
            drop("product_prd")
            notes.append("简报要求跳过 PRD")
        if any(re.search(p, blob, re.IGNORECASE) for p in _SKIP_DESIGN):
            drop("design_ui")
            notes.append("简报要求跳过设计")
        elif "design_ui" not in ids and any(re.search(p, blob, re.IGNORECASE) for p in _NEED_DESIGN):
            after = "product_prd" if "product_prd" in ids else "intake"
            ensure("design_ui", after=after)
        if track == "standard" and (
            not any(re.search(p, blob, re.IGNORECASE) for p in _NEED_INTEGRATE + _COMPLEX_PATTERNS)
            and re.search(r"纯前端|文案|样式|css|ui\s*only", blob, re.IGNORECASE)
        ):
            # medium features can skip integrate if not multi-service
            drop("eng_integrate")
            notes.append("偏前端样式 → 去掉联调")

    # Specialize eng_implement → iOS / Android / Web / Backend / Agent when signals match.
    # Multi-platform → independent branch chains: build → selftest per lane, then 联调.
    platforms = [
        sid
        for sid, pats in _PLATFORM_SIGNALS
        if any(re.search(p, blob, re.IGNORECASE) for p in pats)
    ]
    if platforms and "eng_implement" in ids:
        eng_idx = ids.index("eng_implement")
        after_id = ids[eng_idx - 1] if eng_idx > 0 else None
        drop("eng_implement")
        insert_after = after_id
        if len(platforms) >= 2:
            if "eng_selftest" in ids:
                drop("eng_selftest")
            for sid in platforms:
                ensure(sid, after=insert_after)
                insert_after = sid
                st_id = selftest_id_for(sid)
                ensure(st_id, after=insert_after)
                insert_after = st_id
            delivery_group = "eng_delivery"
            platform_set = set(platforms)
            for s in stages:
                sid = s["id"]
                build = sid if sid in platform_set else None
                if build is None and sid.endswith("_selftest"):
                    cand = sid[: -len("_selftest")]
                    if cand in platform_set:
                        build = cand
                if build is None:
                    continue
                s["parallel_group"] = delivery_group
                s["branch"] = _PLATFORM_BRANCH.get(build, build.replace("eng_", "", 1))
                s["hitl_after"] = False
            if "eng_integrate" not in ids:
                ensure("eng_integrate", after=insert_after)
            notes.append(
                "多端图编排 → "
                + " ∥ ".join(f"{p}→{selftest_id_for(p)}" for p in platforms)
                + " → 联调"
            )
        else:
            ensure(platforms[0], after=insert_after)
            notes.append(f"多端研发细化 → {platforms[0]}")

    # Derive explicit depends_on edges (graph truth for UI / inspect)
    stages = attach_depends_on(stages)
    ids = [s["id"] for s in stages]

    # Normalize on_fail / on_reject targets to stages that exist
    id_set = {s["id"] for s in stages}
    first_build = next(
        (
            s["id"]
            for s in stages
            if s["id"]
            in {"eng_implement", "eng_ios", "eng_android", "eng_web", "eng_backend", "eng_agent"}
        ),
        None,
    )
    for s in stages:
        if s.get("on_fail") and s["on_fail"] not in id_set:
            s["on_fail"] = first_build or s["id"]
        if s.get("on_reject") and s["on_reject"] not in id_set:
            s["on_reject"] = first_build if s["on_reject"] == "eng_implement" else s["id"]
        # Retarget explicit eng_implement bounce when specialists replaced it
        if first_build and first_build != "eng_implement":
            if s.get("on_fail") == "eng_implement":
                s["on_fail"] = first_build
            if s.get("on_reject") == "eng_implement":
                s["on_reject"] = first_build

    rationale = (
        f"航线规划官根据「{title or '未命名'}」提案：基线 `{track}`。"
        + (" ".join(notes[1:]) if len(notes) > 1 else f" {notes[0]}")
        + " 请确认或增删改节点后再出击。"
    )

    return {
        "agent": PLANNER_AGENT,
        "agent_title": PLANNER_TITLE,
        "track_hint": track,
        "rationale": rationale,
        "notes": notes,
        "stages": stages,
        "status": "pending",
    }


def normalize_specs(raw: list[dict[str, Any]] | list[Any]) -> list[dict[str, Any]]:
    """Validate & fill specs from catalog; ensure intake…done sandwich."""
    catalog = catalog_by_id()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        sid = str(item.get("id") or item.get("name") or "").strip()
        if not sid or sid in seen:
            continue
        if item.get("enabled") is False:
            continue
        base = dict(catalog.get(sid) or {
            "id": sid,
            "name": sid,
            "label": item.get("label") or sid,
            "label_en": item.get("label_en") or sid,
            "role": item.get("role") or "product",
            "hitl_after": False,
            "output_contract": [],
        })
        if item.get("label"):
            base["label"] = item["label"]
        if item.get("label_en"):
            base["label_en"] = item["label_en"]
        if item.get("role"):
            base["role"] = item["role"]
        if "hitl_after" in item and item["hitl_after"] is not None:
            base["hitl_after"] = bool(item["hitl_after"])
        if "on_fail" in item:
            base["on_fail"] = item["on_fail"]
        if "on_reject" in item:
            base["on_reject"] = item["on_reject"]
        if item.get("parallel_group"):
            base["parallel_group"] = str(item["parallel_group"]).strip()
        elif base.get("parallel_group"):
            pass
        else:
            base.pop("parallel_group", None)
        if item.get("branch"):
            base["branch"] = str(item["branch"]).strip()
        elif base.get("branch"):
            pass
        else:
            base.pop("branch", None)
        if item.get("depends_on"):
            deps = item["depends_on"]
            if isinstance(deps, list):
                base["depends_on"] = [str(x) for x in deps if x]
            else:
                base.pop("depends_on", None)
        base["id"] = sid
        base["name"] = sid
        out.append(base)
        seen.add(sid)

    if not out or out[0]["id"] != "intake":
        intake = dict(catalog["intake"])
        out = [intake] + [s for s in out if s["id"] != "intake"]
    if out[-1]["id"] != "done":
        done = dict(catalog["done"])
        out = [s for s in out if s["id"] != "done"] + [done]

    id_set = {s["id"] for s in out}
    first_build = next(
        (
            s["id"]
            for s in out
            if s["id"]
            in {"eng_implement", "eng_ios", "eng_android", "eng_web", "eng_backend", "eng_agent"}
        ),
        None,
    )
    for s in out:
        if s.get("on_fail") and s["on_fail"] not in id_set:
            s["on_fail"] = first_build
        if s.get("on_reject") and s["on_reject"] not in id_set:
            s["on_reject"] = s["id"]
    # Fill depends_on when missing so confirmed pipelines always carry graph edges
    if not any(s.get("depends_on") for s in out):
        out = attach_depends_on(out)
    return out


def materialize_template(specs: list[dict[str, Any]], *, name: str = "custom") -> dict[str, Any]:
    stages = []
    for s in normalize_specs(specs):
        entry: dict[str, Any] = {
            "name": s["id"],
            "role": s.get("role") or "product",
            "label": s.get("label") or s["id"],
            "label_en": s.get("label_en") or s.get("label") or s["id"],
            "hitl_after": bool(s.get("hitl_after")),
        }
        if s.get("on_fail"):
            entry["on_fail"] = s["on_fail"]
        if s.get("on_reject"):
            entry["on_reject"] = s["on_reject"]
        if s.get("output_contract"):
            entry["output_contract"] = list(s["output_contract"])
        if s.get("executor"):
            entry["executor"] = s["executor"]
        if s.get("parallel_group"):
            entry["parallel_group"] = s["parallel_group"]
        if s.get("branch"):
            entry["branch"] = s["branch"]
        if s.get("depends_on"):
            entry["depends_on"] = list(s["depends_on"])
        stages.append(entry)
    return {
        "name": name,
        "track": "custom",
        "description": "User-confirmed custom pipeline",
        "stages": stages,
        "roles": load_pipeline_template("default").get("roles", []),
    }


def apply_proposal_to_meta(meta: dict[str, Any], proposal: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
    specs = normalize_specs(proposal.get("stages") or [])
    rail = specs_to_rail(specs)
    meta = dict(meta)
    meta["pipeline_proposal"] = {
        **proposal,
        "stages": specs,
        "status": "confirmed" if confirmed else "pending",
    }
    meta["pipeline_specs"] = specs
    meta["pipeline_stages"] = [s["id"] for s in specs]
    meta["pipeline_rail"] = rail
    meta["pipeline_track"] = "custom" if confirmed else proposal.get("track_hint") or meta.get("pipeline_track")
    meta["pipeline_confirmed"] = confirmed
    meta["track_reason"] = proposal.get("rationale") or meta.get("track_reason")
    return meta


_DIFF_KEYS = ("role", "hitl_after", "on_fail", "on_reject", "label")


def _specs_as_map(specs: list[dict[str, Any]] | list[str]) -> dict[str, dict[str, Any]]:
    if not specs:
        return {}
    if isinstance(specs[0], str):
        catalog = catalog_by_id()
        normalized = [dict(catalog[sid]) for sid in specs if sid in catalog]
    else:
        normalized = normalize_specs(specs)
    return {s["id"]: s for s in normalized}


def diff_specs(
    a: list[dict[str, Any]] | list[str],
    b: list[dict[str, Any]] | list[str],
) -> dict[str, list[str]]:
    """Compare two normalized stage lists; return added / removed / changed ids."""
    map_a = _specs_as_map(a)
    map_b = _specs_as_map(b)
    ids_a = set(map_a)
    ids_b = set(map_b)
    added = sorted(ids_b - ids_a)
    removed = sorted(ids_a - ids_b)
    changed = sorted(
        sid
        for sid in ids_a & ids_b
        if any(map_a[sid].get(k) != map_b[sid].get(k) for k in _DIFF_KEYS)
    )
    return {"added": added, "removed": removed, "changed": changed}


def available_template_names() -> list[str]:
    names = set(TRACK_FILES)
    for path in TEMPLATES_DIR.glob("*_pipeline.yaml"):
        stem = path.stem.replace("_pipeline", "")
        names.add(stem)
    return sorted(names)
