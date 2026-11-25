"""Parallel wave helpers — branch chains with fan-out / fan-in."""

from __future__ import annotations

from typing import Any


def wave_node_name(group: str) -> str:
    return f"__wave__{group}"


def wave_gate_node_name(group: str) -> str:
    """波次门禁节点名（interrupt 专用，与跑分支的节点分开以保住 checkpoint）。"""
    return f"__wave_gate__{group}"


def _stage_id(s: dict[str, Any]) -> str:
    return str(s.get("id") or s.get("name") or "")


def branch_id(s: dict[str, Any]) -> str:
    """Lane identity within a parallel wave. Defaults to the stage id (1-step branch)."""
    b = (s.get("branch") or "").strip()
    return b or _stage_id(s)


def group_wave_branches(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split wave members into ordered branch chains.

    Stages that share the same ``branch`` keep list order (build → selftest).
    Stages without ``branch`` each become a one-stage branch.
    """
    order: list[str] = []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for m in members:
        bid = branch_id(m)
        if bid not in buckets:
            order.append(bid)
            buckets[bid] = []
        buckets[bid].append(m)
    return [{"id": bid, "members": buckets[bid]} for bid in order]


def pending_wave_members(
    branches: list[dict[str, Any]], done: set[str] | list[str] | None = None
) -> list[str]:
    """按分支顺序列出尚未完成的波次成员。

    HITL 放行重入波次时用它算「还欠哪几个阶段」，避免整波重放：
    其它分支已跑完的成员直接复用结论。
    """
    skip = {str(n) for n in (done or set())}
    out: list[str] = []
    for br in branches:
        for m in br["members"]:
            name = str(m["name"])
            if name not in skip:
                out.append(name)
    return out


def merge_wave_checkpoint(
    member_order: list[str], prev_done: list[str], finished: list[str]
) -> list[str]:
    """合并断点：按计划顺序去重，保证重放顺序稳定（不依赖集合迭代序）。"""
    done = set(prev_done or []) | set(finished or [])
    return [n for n in member_order if n in done]


def clear_wave_checkpoint(
    group: str,
    done: dict[str, list[str]] | None,
    results: dict[str, dict[str, Any]] | None,
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    """清掉某个波次的断点。

    波次整体跑完（或有指令要求返工时）必须清空：下一次进入该波次应当全量重跑，
    不能拿旧结论跳过本该返工的阶段。
    """
    next_done = {k: v for k, v in (done or {}).items() if k != group}
    prefix = f"{group}::"
    next_results = {
        k: v for k, v in (results or {}).items() if not str(k).startswith(prefix)
    }
    return next_done, next_results


def collapse_to_waves(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group consecutive stages that share the same non-empty parallel_group.

    Returns a list of:
      {"kind": "single", "stage": <stage dict>}
      {"kind": "parallel", "group": str, "members": [...], "branches": [{"id","members"}, ...]}
    """
    waves: list[dict[str, Any]] = []
    i = 0
    while i < len(stages):
        s = stages[i]
        group = (s.get("parallel_group") or "").strip()
        if not group or s.get("name") in {"intake", "done"} or _stage_id(s) in {"intake", "done"}:
            waves.append({"kind": "single", "stage": s})
            i += 1
            continue
        members = [s]
        j = i + 1
        while j < len(stages):
            nxt = stages[j]
            if (nxt.get("parallel_group") or "").strip() != group:
                break
            nid = _stage_id(nxt)
            if nxt.get("name") in {"intake", "done"} or nid in {"intake", "done"}:
                break
            members.append(nxt)
            j += 1
        if len(members) == 1 and not (members[0].get("branch") or "").strip():
            waves.append({"kind": "single", "stage": s})
        else:
            waves.append(
                {
                    "kind": "parallel",
                    "group": group,
                    "members": members,
                    "branches": group_wave_branches(members),
                }
            )
        i = j
    return waves


_BUILD_IDS = {
    "eng_ios",
    "eng_android",
    "eng_web",
    "eng_backend",
    "eng_agent",
    "eng_implement",
}

_PLATFORM_BRANCH = {
    "eng_ios": "ios",
    "eng_android": "android",
    "eng_web": "web",
    "eng_backend": "backend",
    "eng_agent": "agent",
}


def selftest_id_for(build_id: str) -> str:
    if build_id == "eng_implement":
        return "eng_selftest"
    return f"{build_id}_selftest"


def assign_eng_parallel_group(stages: list[dict[str, Any]], group: str = "eng_build") -> list[dict[str, Any]]:
    """Mark consecutive specialized eng build stages as one parallel wave (legacy helper)."""
    out = [dict(s) for s in stages]
    ids = [_stage_id(s) for s in out]
    i = 0
    while i < len(out):
        sid = ids[i]
        if sid not in _BUILD_IDS or sid == "eng_implement":
            i += 1
            continue
        j = i
        while j < len(out) and ids[j] in _BUILD_IDS and ids[j] != "eng_implement":
            j += 1
        if j - i >= 2:
            for k in range(i, j):
                out[k]["parallel_group"] = group
                out[k]["hitl_after"] = False
                bid = _PLATFORM_BRANCH.get(ids[k])
                if bid:
                    out[k]["branch"] = bid
        i = j
    return out


def attach_depends_on(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive explicit depends_on edges from linear + branch structure.

    Planner / UI treat this as the graph truth; runtime still uses waves for execution.
    """
    out = [dict(s) for s in stages]
    waves = collapse_to_waves(out)
    # Map id -> depends_on list
    deps: dict[str, list[str]] = {_stage_id(s): [] for s in out}
    prev_tips: list[str] = []

    for wave in waves:
        if wave["kind"] == "single":
            sid = _stage_id(wave["stage"])
            if prev_tips:
                deps[sid] = list(prev_tips)
            prev_tips = [sid]
            continue

        branches = wave.get("branches") or group_wave_branches(wave["members"])
        tips: list[str] = []
        for br in branches:
            members = br["members"]
            for idx, m in enumerate(members):
                mid = _stage_id(m)
                if idx == 0:
                    deps[mid] = list(prev_tips) if prev_tips else []
                else:
                    deps[mid] = [_stage_id(members[idx - 1])]
            tips.append(_stage_id(members[-1]))
        prev_tips = tips

    for s in out:
        sid = _stage_id(s)
        d = deps.get(sid) or []
        if d:
            s["depends_on"] = d
        else:
            s.pop("depends_on", None)
    return out
