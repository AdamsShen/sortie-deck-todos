#!/usr/bin/env python3
"""任务执行评测报告（P0 只读原型）。

读取本地真实数据，输出 Markdown / JSON 报告，不修改任何任务、不调用 LLM：

- ``data/initiatives.json``：任务终态、timeline、HITL、交接、产物引用；
- ``data/missions/<id>/``：产物文件、聊天记录；
- ``data/verify/<run>/report.json``：已有的功能验收报告。

当前只覆盖「能从既有数据直接复算」的指标。阶段耗时、token、成本、并行收益
需要先补埋点（见 docs/plans/2026-10-27-task-execution-evaluation.md），报告里
会显式标为不可用，不用估算值填充。

用法：
    python scripts/eval_report.py
    python scripts/eval_report.py --out -            # 只打印，不落盘
    python scripts/eval_report.py --data-dir data --out data/eval/reports/report.md
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
try:  # 兼容未 pip install -e 的临时环境
    from sortie_deck.contracts import REQUIRED
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from sortie_deck.contracts import REQUIRED

logger = logging.getLogger("eval_report")

_STATUS_TERMINAL = {"done", "failed", "stopped"}
_REWORK_ACTIONS = {"reject", "edit_instruction", "reroute"}


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round(max(0.0, (end - start).total_seconds() / 3600), 2)


def _percentile(values: list[float], ratio: float) -> float | None:
    """最近秩百分位；样本很小时只作为量级参考。"""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return round(ordered[idx], 2)


def _gini(values: list[int]) -> float | None:
    """按角色阶段执行次数近似负载均衡；不是 token 口径。"""
    vals = sorted(v for v in values if v > 0)
    if not vals:
        return None
    total = sum(vals)
    n = len(vals)
    if total == 0:
        return 0.0
    weighted = sum((i + 1) * v for i, v in enumerate(vals))
    return round((2 * weighted) / (n * total) - (n + 1) / n, 3)


def _load_initiatives(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return [v for v in raw.values() if isinstance(v, dict)]
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, dict)]
    raise ValueError(f"unsupported initiatives payload: {type(raw).__name__}")


def _stage_runs(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """阶段执行记录：只取带 data.status 的 info 事件，避免把确认/咨询算进去。"""
    rows: list[dict[str, Any]] = []
    for event in timeline:
        if event.get("kind") != "info":
            continue
        data = event.get("data") or {}
        status = data.get("status")
        if status not in {"ok", "fail", "needs_hitl"}:
            continue
        rows.append(
            {
                "stage": str(event.get("stage") or ""),
                "role": str(event.get("role") or ""),
                "status": status,
                "executor": str(data.get("executor") or ""),
                "qa_attempt": data.get("qa_attempt"),
                "at": event.get("at"),
            }
        )
    return rows


def _hitl_stats(
    timeline: list[dict[str, Any]], last_event_at: datetime | None
) -> dict[str, Any]:
    """统计 HITL 请求/决策/返工，并把等待区间配对成 wait_hours。

    配对规则：先遇到「等待人类确认 / escalation requested」事件记 pending，
    再遇到带 ``data.action`` 的事件视为一次决策；未配对的等待按「仍挂起」统计。
    """
    requests = 0
    decisions = 0
    actions: Counter[str] = Counter()
    wait_hours = 0.0
    pending_at: datetime | None = None
    open_count = 0
    open_hours = 0.0

    for event in timeline:
        if event.get("kind") != "hitl":
            continue
        data = event.get("data") or {}
        message = str(event.get("message") or "")
        at = _parse_dt(event.get("at"))
        is_wait = (
            "action" not in data
            and (
                "Waiting for human confirmation" in message
                or "escalation requested" in message
                or ("allowed_actions" in data and "prompt" in data)
            )
        )
        if is_wait:
            requests += 1
            pending_at = at
            continue
        action = data.get("action")
        if action:
            decisions += 1
            actions[str(action)] += 1
            if pending_at is not None and at is not None:
                wait_hours += max(0.0, (at - pending_at).total_seconds() / 3600)
                pending_at = None

    if pending_at is not None:
        open_count = 1
        if last_event_at is not None:
            open_hours = max(0.0, (last_event_at - pending_at).total_seconds() / 3600)

    return {
        "requests": requests,
        "decisions": decisions,
        "actions": dict(actions),
        "rework": sum(actions[a] for a in _REWORK_ACTIONS),
        "wait_hours": round(wait_hours, 2),
        "open_count": open_count,
        "open_hours": round(open_hours, 2),
        "last_action": next(
            (str((e.get("data") or {}).get("action")) for e in reversed(timeline)
             if e.get("kind") == "hitl" and (e.get("data") or {}).get("action")),
            "",
        ),
    }


def _handoff_stats(timeline: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for event in timeline:
        if event.get("kind") != "transition":
            continue
        handoff = (event.get("data") or {}).get("handoff")
        if handoff in {"accepted", "denied"}:
            counts[str(handoff)] += 1
    return {"accepted": counts["accepted"], "denied": counts["denied"]}


def _chat_stats(mission_dir: Path) -> dict[str, int]:
    """统计主房间 + 子房间消息；没有聊天目录返回全 0。"""
    chat_dir = mission_dir / "chat"
    files = sorted(chat_dir.glob("*.jsonl")) if chat_dir.exists() else []
    total = human = agent = system = 0
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                total += 1
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                kind = str(row.get("actor_kind") or "")
                if kind == "human":
                    human += 1
                elif kind == "agent":
                    agent += 1
                elif kind == "system":
                    system += 1
        except OSError:
            continue
    return {"files": len(files), "total": total, "human": human, "agent": agent, "system": system}


def _task_artifact_stats(
    initiative_id: str, missions_dir: Path
) -> dict[str, Any]:
    """产物数量/体积/契约完整度/测试结果/diff 规模。"""
    art_dir = missions_dir / initiative_id / "artifacts"
    stage_dirs = (
        {p.name: p for p in art_dir.iterdir() if p.is_dir()} if art_dir.exists() else {}
    )
    files = [p for p in art_dir.rglob("*") if p.is_file()] if art_dir.exists() else []

    contract_checks = 0
    contract_pass = 0
    missing: dict[str, list[str]] = {}
    for stage, required in REQUIRED.items():
        stage_dir = stage_dirs.get(stage)
        if stage_dir is None:
            continue
        contract_checks += 1
        absent = [name for name in required if not (stage_dir / name).is_file()]
        if absent:
            missing[stage] = absent
        else:
            contract_pass += 1

    diff_lines = 0
    files_written_sum = 0
    files_written_max = 0
    test_passed: bool | None = None
    for path in files:
        if path.name == "diff.patch":
            try:
                diff_lines += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                pass
        if path.name == "tasks.json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            value = payload.get("files_written") if isinstance(payload, dict) else None
            if isinstance(value, int):
                files_written_sum += value
                files_written_max = max(files_written_max, value)
        if path.name == "test_report.json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("passed"), bool):
                test_passed = payload["passed"]

    return {
        "stage_dirs": len(stage_dirs),
        "stage_dir_names": sorted(stage_dirs),
        "files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "contract_checks": contract_checks,
        "contract_pass": contract_pass,
        "contract_missing": missing,
        "diff_lines": diff_lines,
        "files_written_sum": files_written_sum,
        "files_written_max": files_written_max,
        "test_passed": test_passed,
    }


def _milestones(
    planned_stages: list[str],
    runs: list[dict[str, Any]],
    artifact_stats: dict[str, Any],
) -> dict[str, Any]:
    """里程碑达成：阶段 ok + 该阶段契约文件齐全（无契约的阶段只看 ok）。"""
    planned = [s for s in planned_stages if s not in {"intake", "done"}]
    by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        by_stage[run["stage"]].append(run)
    missing = artifact_stats.get("contract_missing") or {}
    stage_dirs = set(artifact_stats.get("stage_dir_names") or [])
    achieved: list[str] = []
    failed: list[str] = []
    not_run: list[str] = []
    for stage in planned:
        stage_runs = by_stage.get(stage) or []
        if not stage_runs:
            not_run.append(stage)
            continue
        ok = any(run["status"] == "ok" for run in stage_runs)
        contract_ok = stage not in REQUIRED or (stage in stage_dirs and stage not in missing)
        if ok and contract_ok:
            achieved.append(stage)
        else:
            failed.append(stage)
    total = len(planned)
    executed = len(achieved) + len(failed)
    return {
        "planned": total,
        "achieved": len(achieved),
        "failed": failed,
        "not_run": not_run,
        "executed": executed,
        "rate": round(len(achieved) / total, 3) if total else None,
        "rate_executed": round(len(achieved) / executed, 3) if executed else None,
        "coverage": round(executed / total, 3) if total else None,
    }


def _task_metrics(initiative: dict[str, Any], missions_dir: Path) -> dict[str, Any]:
    initiative_id = str(initiative.get("id") or "")
    timeline = [e for e in (initiative.get("timeline") or []) if isinstance(e, dict)]
    runs = _stage_runs(timeline)
    stage_counts = Counter(run["stage"] for run in runs)
    retry_stages = {stage: n for stage, n in stage_counts.items() if n > 1}

    created = _parse_dt(initiative.get("created_at"))
    updated = _parse_dt(initiative.get("updated_at"))
    event_times = [t for t in (_parse_dt(e.get("at")) for e in timeline) if t is not None]
    first_event = min(event_times) if event_times else None
    last_event = max(event_times) if event_times else None

    seconds = Counter(
        t.replace(microsecond=0).isoformat() for t in event_times
    )
    max_same_second = max(seconds.values()) if seconds else 0
    batched_ratio = round(max_same_second / len(event_times), 3) if event_times else 0.0

    hitl = _hitl_stats(timeline, last_event)
    handoff = _handoff_stats(timeline)
    chat = _chat_stats(missions_dir / initiative_id)
    artifacts = _task_artifact_stats(initiative_id, missions_dir)

    meta = initiative.get("meta") or {}
    planned_stages = [str(s) for s in (meta.get("pipeline_stages") or [])]
    milestone = _milestones(planned_stages, runs, artifacts)

    # 首次通过率按「每个阶段第一次执行」算；qa_attempt 是任务级计数器，
    # 用它会把后续阶段的首跑误判成失败（例如 qa_attempt=3 的 deploy 首跑）。
    first_run_by_stage: dict[str, dict[str, Any]] = {}
    for run in runs:
        first_run_by_stage.setdefault(run["stage"], run)
    fp_total = len(first_run_by_stage)
    fp_ok = sum(1 for run in first_run_by_stage.values() if run["status"] == "ok")
    role_load = Counter(run["role"] for run in runs if run["role"])

    event_span = _hours(first_event, last_event)
    wall = _hours(created, updated)
    active_estimate = (
        round(max(0.0, event_span - hitl["wait_hours"]), 2) if event_span is not None else None
    )

    failure_flags: dict[str, int] = {}
    if retry_stages:
        failure_flags["step_repetition"] = sum(n - 1 for n in retry_stages.values())
    if str(initiative.get("status")) == "done" and (milestone["rate"] or 0) < 1:
        failure_flags["premature_termination"] = 1
    if str(initiative.get("status")) == "done":
        has_qa = any(run["stage"] == "qa_verify" and run["status"] == "ok" for run in runs)
        if not has_qa or artifacts.get("test_passed") is not True:
            failure_flags["no_or_incomplete_verification"] = 1

    return {
        "initiative_id": initiative_id,
        "status": str(initiative.get("status") or ""),
        "track": str(meta.get("pipeline_track") or initiative.get("template") or ""),
        "harness": str(initiative.get("harness") or ""),
        "coding_executor": str(meta.get("coding_executor") or ""),
        "created_at": initiative.get("created_at"),
        "updated_at": initiative.get("updated_at"),
        "wall_hours": wall,
        "event_span_hours": event_span,
        "active_estimate_hours": active_estimate,
        "events": len(timeline),
        "batched_event_ratio": batched_ratio,
        "stage_runs": len(runs),
        "stage_ok": sum(1 for r in runs if r["status"] == "ok"),
        "stage_fail": sum(1 for r in runs if r["status"] == "fail"),
        "stage_needs_hitl": sum(1 for r in runs if r["status"] == "needs_hitl"),
        "first_pass_yield": round(fp_ok / fp_total, 3) if fp_total else None,
        "first_pass_total": fp_total,
        "first_pass_ok": fp_ok,
        "retry_stages": retry_stages,
        "hitl": hitl,
        "handoff": handoff,
        "handoff_accept_rate": round(
            handoff["accepted"] / (handoff["accepted"] + handoff["denied"]), 3
        )
        if (handoff["accepted"] + handoff["denied"])
        else None,
        "chat": chat,
        "role_load": dict(role_load),
        "role_load_gini": _gini(list(role_load.values())),
        "milestone": milestone,
        "artifacts": artifacts,
        "failure_flags": failure_flags,
    }


def _load_verify_reports(verify_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not verify_dir.exists():
        return rows
    for path in sorted(verify_dir.glob("*/report.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("verify 报告无法解析: %s", path)
            continue
        payload["_path"] = str(path)
        rows.append(payload)
    return rows


def _aggregate_tasks(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    terminal = [t for t in tasks if t["status"] in _STATUS_TERMINAL]
    done = [t for t in terminal if t["status"] == "done"]

    stage_runs = sum(t["stage_runs"] for t in tasks)
    stage_ok = sum(t["stage_ok"] for t in tasks)
    stage_fail = sum(t["stage_fail"] for t in tasks)

    contract_checks = sum(t["artifacts"]["contract_checks"] for t in tasks)
    contract_pass = sum(t["artifacts"]["contract_pass"] for t in tasks)

    fp_total = sum(t["first_pass_total"] for t in tasks)
    fp_ok = sum(t["first_pass_ok"] for t in tasks)

    decisions = sum(t["hitl"]["decisions"] for t in tasks)
    rework = sum(t["hitl"]["rework"] for t in tasks)
    hitl_tasks = sum(1 for t in tasks if t["hitl"]["requests"] > 0)

    accepted = sum(t["handoff"]["accepted"] for t in tasks)
    denied = sum(t["handoff"]["denied"] for t in tasks)

    wall_values = [t["wall_hours"] for t in terminal if t["wall_hours"] is not None]
    hitl_wait_values = [t["hitl"]["wait_hours"] for t in tasks]
    active_values = [
        t["active_estimate_hours"] for t in terminal if t["active_estimate_hours"] is not None
    ]

    planned_total = sum(t["milestone"]["planned"] for t in tasks)
    achieved_total = sum(t["milestone"]["achieved"] for t in tasks)
    executed_total = sum(t["milestone"]["executed"] for t in tasks)
    wait_total = sum(hitl_wait_values)
    active_total = sum(active_values)

    return {
        "tasks": len(tasks),
        "terminal": len(terminal),
        "done": len(done),
        "failed": sum(1 for t in terminal if t["status"] == "failed"),
        "stopped": sum(1 for t in terminal if t["status"] == "stopped"),
        "running": sum(1 for t in tasks if t["status"] == "running"),
        "waiting_hitl": sum(1 for t in tasks if t["status"] == "waiting_hitl"),
        "draft": sum(1 for t in tasks if t["status"] == "draft"),
        "task_success_rate": round(len(done) / len(terminal), 3) if terminal else None,
        "stage_runs": stage_runs,
        "stage_ok": stage_ok,
        "stage_fail": stage_fail,
        "stage_success_rate": round(stage_ok / stage_runs, 3) if stage_runs else None,
        "contract_checks": contract_checks,
        "contract_pass": contract_pass,
        "contract_pass_rate": round(contract_pass / contract_checks, 3)
        if contract_checks
        else None,
        "first_pass_yield": round(fp_ok / fp_total, 3) if fp_total else None,
        "first_pass_ok": fp_ok,
        "first_pass_total": fp_total,
        "hitl_tasks": hitl_tasks,
        "hitl_rate": round(hitl_tasks / len(tasks), 3) if tasks else None,
        "hitl_decisions": decisions,
        "hitl_rework": rework,
        "hitl_rework_rate": round(rework / decisions, 3) if decisions else None,
        "hitl_open_tasks": sum(1 for t in tasks if t["hitl"]["open_count"]),
        "hitl_wait_hours_total": round(wait_total, 2),
        "hitl_open_hours_total": round(
            sum(t["hitl"]["open_hours"] for t in tasks), 2
        ),
        "hitl_wait_share": round(wait_total / (wait_total + active_total), 3)
        if (wait_total + active_total) > 0
        else None,
        "handoff_accepted": accepted,
        "handoff_denied": denied,
        "handoff_accept_rate": round(accepted / (accepted + denied), 3)
        if (accepted + denied)
        else None,
        "wall_hours_p50": _percentile(wall_values, 0.5),
        "wall_hours_p95": _percentile(wall_values, 0.95),
        "active_estimate_hours_total": round(sum(active_values), 2)
        if active_values
        else None,
        "milestones_planned": planned_total,
        "milestones_achieved": achieved_total,
        "milestones_executed": executed_total,
        "milestone_rate": round(achieved_total / planned_total, 3) if planned_total else None,
        "milestone_rate_executed": round(achieved_total / executed_total, 3)
        if executed_total
        else None,
        "milestone_coverage": round(executed_total / planned_total, 3)
        if planned_total
        else None,
        "artifact_files": sum(t["artifacts"]["files"] for t in tasks),
        "artifact_bytes": sum(t["artifacts"]["bytes"] for t in tasks),
        "diff_lines": sum(t["artifacts"]["diff_lines"] for t in tasks),
        "chat_messages": sum(t["chat"]["total"] for t in tasks),
    }


def _aggregate_verify(reports: list[dict[str, Any]]) -> dict[str, Any]:
    by_harness: dict[str, dict[str, int]] = defaultdict(
        lambda: {"runs": 0, "pass": 0, "fail": 0, "skip": 0}
    )
    failed_checks: list[dict[str, Any]] = []
    failure_by_check: Counter[tuple[str, str]] = Counter()
    for report in reports:
        harness = str(report.get("harness") or "?")
        row = by_harness[harness]
        row["runs"] += 1
        summary = report.get("summary") or {}
        for key in ("pass", "fail", "skip"):
            row[key] += int(summary.get(key) or 0)
        for check in report.get("checks") or []:
            if isinstance(check, dict) and check.get("status") == "fail":
                check_id = str(check.get("id") or "")
                check_name = str(check.get("name") or "")
                failure_by_check[(check_id, check_name)] += 1
                failed_checks.append(
                    {
                        "run_id": report.get("run_id"),
                        "harness": harness,
                        "id": check.get("id"),
                        "name": check.get("name"),
                        "detail": check.get("detail"),
                    }
                )
    total_pass = sum(r["pass"] for r in by_harness.values())
    total_fail = sum(r["fail"] for r in by_harness.values())
    return {
        "runs": len(reports),
        "pass": total_pass,
        "fail": total_fail,
        "skip": sum(r["skip"] for r in by_harness.values()),
        "pass_rate": round(total_pass / (total_pass + total_fail), 3)
        if (total_pass + total_fail)
        else None,
        "by_harness": dict(by_harness),
        "failure_by_check": [
            {"id": key[0], "name": key[1], "count": count}
            for key, count in failure_by_check.most_common()
        ],
        "failed_checks": failed_checks,
    }


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _render_markdown(
    tasks: list[dict[str, Any]],
    aggregate: dict[str, Any],
    verify: dict[str, Any],
    data_quality: dict[str, Any],
    *,
    data_dir: Path,
) -> str:
    batched_ids = data_quality.get("batched_task_ids") or []
    batched_label = "、".join(batched_ids[:5])
    if len(batched_ids) > 5:
        batched_label += " 等"
    lines = [
        "# Sortie Deck 任务执行评测报告（真实数据）",
        "",
        f"- 数据源：`{data_dir}`",
        f"- 生成时间：{datetime.now(UTC).isoformat(timespec='seconds')}",
        "- 性质：只读报告；不调用 LLM，不修改任务，不做 token/成本估算。",
        "- 快照说明：读取的是生成时刻的 `initiatives.json`；若 API 正在运行，任务状态可能继续变化。",
        "",
        "## 1. 结论摘要",
        "",
        f"- 任务总数 **{aggregate['tasks']}**：done {aggregate['done']} / failed "
        f"{aggregate['failed']} / stopped {aggregate['stopped']} / running "
        f"{aggregate['running']} / waiting_hitl {aggregate['waiting_hitl']} / draft "
        f"{aggregate['draft']}。",
        f"- 终态任务成功率 **{_pct(aggregate['task_success_rate'])}**"
        f"（{aggregate['done']}/{aggregate['terminal']}）。",
        f"- 阶段成功率 **{_pct(aggregate['stage_success_rate'])}**"
        f"（{aggregate['stage_ok']}/{aggregate['stage_runs']}），"
        f"失败阶段 {aggregate['stage_fail']} 次。",
        f"- 契约完整率 **{_pct(aggregate['contract_pass_rate'])}**"
        f"（{aggregate['contract_pass']}/{aggregate['contract_checks']} 个有契约的阶段）。",
        f"- 首次通过率 **{_pct(aggregate['first_pass_yield'])}**"
        f"（{aggregate['first_pass_ok']}/{aggregate['first_pass_total']} 个执行过的阶段首跑即 ok）。",
        f"- HITL：{aggregate['hitl_tasks']} 个任务触发过门禁，"
        f"决策 {aggregate['hitl_decisions']} 次，返工 {aggregate['hitl_rework']} 次"
        f"（返工率 {_pct(aggregate['hitl_rework_rate'])}）；"
        f"仍在等待的任务 {aggregate['hitl_open_tasks']} 个。",
        f"- 等待时长：已配对 HITL 等待合计 **{_fmt(aggregate['hitl_wait_hours_total'])} 小时**；"
        f"未解决等待 {aggregate['hitl_open_tasks']} 个"
        f"（可估算 {_fmt(aggregate['hitl_open_hours_total'])} 小时）；"
        f"终态任务 active 估算合计 {_fmt(aggregate['active_estimate_hours_total'])} 小时。",
        f"- HITL 等待占「active + 等待」的 **{_pct(aggregate['hitl_wait_share'])}**；"
        "当前瓶颈主要是等人，不是执行。",
        f"- 交接：accepted {aggregate['handoff_accepted']} / denied "
        f"{aggregate['handoff_denied']}，接受率 {_pct(aggregate['handoff_accept_rate'])}。",
        f"- 里程碑：总体 **{_pct(aggregate['milestone_rate'])}**"
        f"（{aggregate['milestones_achieved']}/{aggregate['milestones_planned']}）；"
        f"已执行阶段达成率 **{_pct(aggregate['milestone_rate_executed'])}**"
        f"（{aggregate['milestones_achieved']}/{aggregate['milestones_executed']}），"
        f"计划覆盖率 {_pct(aggregate['milestone_coverage'])}。",
        f"- 产物：{aggregate['artifact_files']} 个文件 / "
        f"{aggregate['artifact_bytes'] / 1024:.1f} KB；diff 合计 "
        f"{aggregate['diff_lines']} 行；聊天消息 {aggregate['chat_messages']} 条。",
        f"- 既有 verify 报告：{verify['runs']} 次运行，"
        f"pass {verify['pass']} / fail {verify['fail']} / skip {verify['skip']}，"
        f"通过率 {_pct(verify['pass_rate'])}"
        + (
            "；失败最多的是 "
            + "、".join(
                f"{row['id']} {row['count']} 次"
                for row in (verify.get("failure_by_check") or [])[:3]
            )
            + "。"
            if verify.get("failure_by_check")
            else "。"
        ),
        "",
        "## 2. 任务终态与成功率",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 任务总数 | {aggregate['tasks']} |",
        f"| 终态任务 | {aggregate['terminal']} |",
        f"| done | {aggregate['done']} |",
        f"| failed | {aggregate['failed']} |",
        f"| stopped | {aggregate['stopped']} |",
        f"| running | {aggregate['running']} |",
        f"| waiting_hitl | {aggregate['waiting_hitl']} |",
        f"| draft | {aggregate['draft']} |",
        f"| 终态成功率 | {_pct(aggregate['task_success_rate'])} |",
        "",
        "> 成功率分母只算终态（done + failed + stopped）；running / waiting_hitl / draft 单列，"
        "避免把「还没跑完」当成失败。",
        "",
        "## 3. 执行效率",
        "",
        "| 指标 | 值 | 说明 |",
        "|---|---|---|",
        f"| 终态任务墙钟 p50 | {_fmt(aggregate['wall_hours_p50'])} h | `updated_at - created_at`，含等待/空闲 |",
        f"| 终态任务墙钟 p95 | {_fmt(aggregate['wall_hours_p95'])} h | 小样本，仅作量级参考 |",
        f"| HITL 等待合计 | {_fmt(aggregate['hitl_wait_hours_total'])} h | 已配对「等待 → 决策」区间 |",
        f"| 未解决 HITL 等待 | {aggregate['hitl_open_tasks']} 个 | 末次事件若就是等待事件，时长不可估算 |",
        f"| active 估算合计 | {_fmt(aggregate['active_estimate_hours_total'])} h | 事件跨度减去 HITL 等待，非精确耗时 |",
        "",
        "> 当前数据没有 `duration_ms`、也没有 OTel span 导出，阶段级耗时/token/成本/并行收益"
        "**无法计算**；上表只给任务级近似值，不用估算值填充空白。",
        "",
        "## 4. 阶段、契约与里程碑",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 阶段执行次数 | {aggregate['stage_runs']} |",
        f"| ok / fail | {aggregate['stage_ok']} / {aggregate['stage_fail']} |",
        f"| 阶段成功率 | {_pct(aggregate['stage_success_rate'])} |",
        f"| 首次通过率 | {_pct(aggregate['first_pass_yield'])}"
        f"（{aggregate['first_pass_ok']}/{aggregate['first_pass_total']}） |",
        f"| 契约检查 / 通过 | {aggregate['contract_checks']} / {aggregate['contract_pass']} |",
        f"| 契约完整率 | {_pct(aggregate['contract_pass_rate'])} |",
        f"| 里程碑总体达成 | {aggregate['milestones_achieved']}/{aggregate['milestones_planned']}"
        f"（{_pct(aggregate['milestone_rate'])}） |",
        f"| 里程碑已执行达成 | {aggregate['milestones_achieved']}/{aggregate['milestones_executed']}"
        f"（{_pct(aggregate['milestone_rate_executed'])}） |",
        f"| 计划覆盖率 | {_pct(aggregate['milestone_coverage'])} |",
        "",
        "> 里程碑口径：`meta.pipeline_stages` 去掉 intake/done；阶段至少一次 `ok`，"
        "且该阶段在 `contracts.REQUIRED` 里的必需文件齐全。未执行的阶段计入分母，"
        "便于暴露「计划了但没跑」的缺口。首次通过率在阶段数很少的任务上会被抬高，"
        "应按任务分组看，不要只看全局值。",
        "",
        "## 5. 协作指标",
        "",
        "| 指标 | 值 | 说明 |",
        "|---|---|---|",
        f"| HITL 任务占比 | {_pct(aggregate['hitl_rate'])} | 有门禁请求的任务 / 任务总数 |",
        f"| HITL 决策数 | {aggregate['hitl_decisions']} | `data.action` 事件数 |",
        f"| 人工返工率 | {_pct(aggregate['hitl_rework_rate'])} | reject/edit/reroute / 决策数 |",
        f"| 交接接受率 | {_pct(aggregate['handoff_accept_rate'])} | accepted / (accepted+denied) |",
        f"| 聊天消息数 | {aggregate['chat_messages']} | 主房间 + 子房间 JSONL 行数 |",
        "",
        "> 协作质量分（Communication / Planning / Coordination）当前没有 judge 数据，"
        "按 `null` 处理，不按 0 分计入。角色负载 Gini 只在逐任务明细里给出，"
        "口径是阶段执行次数，不是 token。",
        "",
        "## 6. 产物与质量",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 产物文件数 | {aggregate['artifact_files']} |",
        f"| 产物体积 | {aggregate['artifact_bytes'] / 1024:.1f} KB |",
        f"| diff 行数 | {aggregate['diff_lines']} |",
        f"| 聊天消息 | {aggregate['chat_messages']} |",
        "",
        "## 7. 逐任务明细",
        "",
        "| 任务 | 状态 | track | harness | 墙钟 h | 事件 | 阶段 ok/fail | HITL 决策 | 交接 a/d | 产物 | 里程碑 | 首次通过 |",
        "|---|---|---|---|---:|---:|---|---:|---|---:|---|---:|",
    ]
    for task in sorted(tasks, key=lambda t: str(t.get("created_at") or ""), reverse=True):
        hitl = task["hitl"]
        handoff = task["handoff"]
        milestone = task["milestone"]
        lines.append(
            "| {id} | {status} | {track} | {harness} | {wall} | {events} | "
            "{ok}/{fail} | {hitl} | {a}/{d} | {art} | {ms} | {fp} |".format(
                id=task["initiative_id"],
                status=task["status"],
                track=task["track"] or "-",
                harness=task["harness"] or "-",
                wall=_fmt(task["wall_hours"]),
                events=task["events"],
                ok=task["stage_ok"],
                fail=task["stage_fail"],
                hitl=hitl["decisions"],
                a=handoff["accepted"],
                d=handoff["denied"],
                art=task["artifacts"]["files"],
                ms="n/a"
                if milestone["rate"] is None
                else f"{milestone['achieved']}/{milestone['planned']}",
                fp=_pct(task["first_pass_yield"]),
            )
        )

    lines += [
        "",
        "## 8. 既有 verify 报告聚合",
        "",
        "| harness | 运行次数 | pass | fail | skip | 通过率 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for harness, row in sorted(verify["by_harness"].items()):
        tested = row["pass"] + row["fail"]
        rate = round(row["pass"] / tested, 3) if tested else None
        lines.append(
            f"| {harness} | {row['runs']} | {row['pass']} | {row['fail']} | "
            f"{row['skip']} | {_pct(rate)} |"
        )
    if verify.get("failure_by_check"):
        lines += [
            "",
            "失败项聚合（同一检查项跨运行累计）：",
            "",
            "| id | 检查项 | 失败次数 |",
            "|---|---|---:|",
        ]
        for row in verify["failure_by_check"]:
            lines.append(f"| {row['id']} | {row['name']} | {row['count']} |")
    if verify["failed_checks"]:
        lines += [
            "",
            "失败明细（最多展示 10 条）：",
            "",
            "| run | harness | id | 检查项 | 说明 |",
            "|---|---|---|---|---|",
        ]
        for row in verify["failed_checks"][:10]:
            lines.append(
                f"| {row['run_id']} | {row['harness']} | {row['id']} | "
                f"{row['name']} | {row['detail']} |"
            )

    lines += [
        "",
        "## 9. 数据质量与盲区",
        "",
        f"- 有元数据的任务：{data_quality['initiatives']} 个；任务目录："
        f"{data_quality['mission_dirs']} 个；**孤儿目录** "
        f"{data_quality['orphan_mission_dirs']} 个（无对应 initiative，"
        "多为 workspace/memory 残留，不能纳入评测）。",
        f"- 有聊天记录的任务：{data_quality['tasks_with_chat']} 个；"
        f"有产物的任务：{data_quality['tasks_with_artifacts']} 个；"
        f"有 guardrails 的任务：{data_quality['tasks_with_guardrails']} 个。",
        f"- 有 `duration_ms` 的任务：{data_quality['tasks_with_duration_ms']} 个；"
        "阶段耗时需要先补 `StageResult.meta['duration_ms']` 或启用 OTel。",
        f"- timeline 事件高度集中（同一秒占比 >50%）的任务："
        f"{data_quality['tasks_with_batched_events']} 个"
        + (f"（{batched_label}）" if batched_label else "")
        + "；这些任务的墙钟/事件跨度不能当执行耗时。",
        f"- OTel span 导出：{data_quality['otel_spans']}（当前 data 目录没有 trace 后端产物）；"
        "token、成本、并行收益暂不可用。",
        f"- `mr_merged` 同步：{data_quality['mr_merged_synced']}（未接 GitLab 合并状态）。",
        "- `waiting_hitl` 任务若末次事件就是等待事件，已等待时长无法计算；"
        "报告只统计可配对的「等待 → 决策」区间，不猜测。",
        "",
        "## 10. 口径说明",
        "",
        "1. 成功率只用终态任务；`needs_hitl` 不按失败计。",
        "2. 墙钟 `updated_at - created_at` 含 HITL 等待与空闲，不能当执行耗时。",
        "3. active 估算 = 事件跨度 - 已配对 HITL 等待，仅用于量级对比；"
        "事件时间被批处理时会低估。",
        "4. 契约完整率只检查 `contracts.REQUIRED` 中的文件是否存在，不判断内容质量。",
        "5. 首次通过率 = 每个阶段第一次执行即 `ok` 的阶段数 / 执行过的阶段数；"
        "不按任务级 `qa_attempt` 计，避免后续阶段首跑被误判。",
        "6. 协作质量分需要离线 judge 与人工校准，本报告不生成。",
        "",
    ]
    return "\n".join(lines)


def _data_quality(
    initiatives: list[dict[str, Any]],
    missions_dir: Path,
) -> dict[str, Any]:
    mission_dirs = [p for p in missions_dir.iterdir() if p.is_dir()] if missions_dir.exists() else []
    known = {str(i.get("id")) for i in initiatives}
    batched = 0
    batched_ids: list[str] = []
    with_chat = 0
    with_artifacts = 0
    with_guardrails = 0
    with_duration = 0
    for task in initiatives:
        timeline = [e for e in (task.get("timeline") or []) if isinstance(e, dict)]
        if any((e.get("data") or {}).get("duration_ms") is not None for e in timeline):
            with_duration += 1
        times = [t for t in (_parse_dt(e.get("at")) for e in timeline) if t is not None]
        if times:
            counter = Counter(t.replace(microsecond=0).isoformat() for t in times)
            if max(counter.values()) / len(times) > 0.5:
                batched += 1
                batched_ids.append(str(task.get("id") or ""))
        mission = missions_dir / str(task.get("id"))
        if (mission / "chat").exists():
            with_chat += 1
        if (mission / "artifacts").exists():
            with_artifacts += 1
        if (task.get("meta") or {}).get("guardrails"):
            with_guardrails += 1
    return {
        "initiatives": len(initiatives),
        "mission_dirs": len(mission_dirs),
        "orphan_mission_dirs": sum(1 for p in mission_dirs if p.name not in known),
        "tasks_with_chat": with_chat,
        "tasks_with_artifacts": with_artifacts,
        "tasks_with_guardrails": with_guardrails,
        "tasks_with_duration_ms": with_duration,
        "tasks_with_batched_events": batched,
        "batched_task_ids": batched_ids,
        "otel_spans": 0,
        "mr_merged_synced": 0,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="任务执行评测报告（只读）")
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"), help="数据根目录")
    parser.add_argument(
        "--out",
        default="",
        help="Markdown 输出路径；默认 data/eval/reports/<日期>-task-eval.md，传 - 只打印",
    )
    parser.add_argument("--json-out", default="", help="JSON 输出路径；默认与 Markdown 同名 .json")
    parser.add_argument("--no-verify", action="store_true", help="跳过 data/verify 报告聚合")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    data_dir = Path(args.data_dir).expanduser().resolve()
    initiatives_path = data_dir / "initiatives.json"
    if not initiatives_path.exists():
        logger.error("找不到任务数据: %s", initiatives_path)
        return 2

    initiatives = _load_initiatives(initiatives_path)
    missions_dir = data_dir / "missions"
    tasks = [_task_metrics(item, missions_dir) for item in initiatives]
    aggregate = _aggregate_tasks(tasks)
    verify = (
        {"runs": 0, "pass": 0, "fail": 0, "skip": 0, "pass_rate": None,
         "by_harness": {}, "failed_checks": []}
        if args.no_verify
        else _aggregate_verify(_load_verify_reports(data_dir / "verify"))
    )
    quality = _data_quality(initiatives, missions_dir)
    markdown = _render_markdown(
        tasks, aggregate, verify, quality, data_dir=data_dir
    )

    if args.out == "-":
        sys.stdout.write(markdown)
    else:
        out_path = (
            Path(args.out)
            if args.out
            else data_dir / "eval" / "reports" / f"{datetime.now().strftime('%Y-%m-%d')}-task-eval.md"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
        json_path = (
            Path(args.json_out)
            if args.json_out
            else out_path.with_suffix(".json")
        )
        json_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "data_dir": str(data_dir),
                    "aggregate": aggregate,
                    "verify": verify,
                    "data_quality": quality,
                    "tasks": tasks,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("报告已写入 %s", out_path)
        logger.info("JSON 已写入 %s", json_path)

    summary = {
        "tasks": aggregate["tasks"],
        "success_rate": aggregate["task_success_rate"],
        "stage_success_rate": aggregate["stage_success_rate"],
        "hitl_wait_hours": aggregate["hitl_wait_hours_total"],
        "verify_pass_rate": verify["pass_rate"],
    }
    logger.info("摘要: %s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
