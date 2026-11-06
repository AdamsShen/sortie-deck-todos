"""阶段指标：落库、聚合、重跑 attempt，以及耗时分析的严格权限。"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException

from sortie_deck.metrics import StageMetricsStore


def test_record_and_aggregate(tmp_path) -> None:
    store = StageMetricsStore(tmp_path / "metrics.sqlite")
    now = time.time()
    store.record(
        initiative_id="ini_a",
        stage="intake",
        role="product",
        harness="mock",
        status="ok",
        started_at=now,
        ended_at=now + 1.5,
        tool_calls=3,
        meta={"qa_attempt": 1},
    )
    store.record(
        initiative_id="ini_a",
        stage="eng_implement",
        role="eng",
        harness="mock",
        status="ok",
        started_at=now + 2,
        ended_at=now + 5,
        tool_calls=7,
        review_rounds=2,
        review_verdict="pass",
    )

    agg = store.list_missions(["ini_a", "ini_b"])
    assert agg["ini_a"]["stage_count"] == 2
    assert agg["ini_a"]["total_duration_ms"] == 4500
    assert agg["ini_a"]["tool_calls"] == 10
    assert agg["ini_a"]["review_rounds"] == 2
    # 执行起止取首阶段起点 / 末阶段终点，供列表「几点到几点」列
    assert agg["ini_a"]["first_started_at"] == pytest.approx(now)
    assert agg["ini_a"]["last_ended_at"] == pytest.approx(now + 5)
    assert "ini_b" not in agg


def test_attempt_increments_on_rerun(tmp_path) -> None:
    """HITL 打回 / 重试会产生同一阶段的第二条记录，attempt 自动递增。"""
    store = StageMetricsStore(tmp_path / "metrics.sqlite")
    for _ in range(2):
        store.record(
            initiative_id="ini_a",
            stage="qa_verify",
            started_at=time.time(),
            ended_at=time.time() + 1,
            status="fail",
        )
    assert [r["attempt"] for r in store.list_stages("ini_a")] == [1, 2]


def test_list_stages_orders_and_parses_meta(tmp_path) -> None:
    store = StageMetricsStore(tmp_path / "metrics.sqlite")
    base = time.time()
    store.record(
        initiative_id="ini_a",
        stage="later",
        started_at=base + 10,
        ended_at=base + 12,
        meta={"timeout": True},
    )
    store.record(
        initiative_id="ini_a", stage="earlier", started_at=base, ended_at=base + 1
    )

    rows = store.list_stages("ini_a")
    assert [r["stage"] for r in rows] == ["earlier", "later"]
    assert rows[1]["meta"]["timeout"] is True


def test_record_ignores_empty_identity(tmp_path) -> None:
    store = StageMetricsStore(tmp_path / "metrics.sqlite")
    assert store.record(initiative_id="", stage="intake", started_at=0, ended_at=0) == 0
    assert store.record(initiative_id="ini_a", stage="", started_at=0, ended_at=0) == 0


def test_metrics_owner_is_strict_for_admin_and_shared() -> None:
    """耗时分析只认创建者：admin 与被共享者都不能看别人的任务。"""
    from sortie_deck.api.app import _ensure_metrics_owner
    from sortie_deck.models import Initiative, MissionShare

    ini = Initiative(title="t", brief="b", created_by="usr_owner")
    ini.shared_with = [MissionShare(user_id="usr_shared")]

    owner = type("U", (), {"id": "usr_owner", "role": "member"})()
    admin = type("U", (), {"id": "usr_admin", "role": "admin"})()
    shared = type("U", (), {"id": "usr_shared", "role": "member"})()

    _ensure_metrics_owner(ini, owner)
    for user in (admin, shared):
        with pytest.raises(HTTPException) as exc:
            _ensure_metrics_owner(ini, user)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_orchestrator_records_metrics_for_mission(tmp_path) -> None:
    """端到端：跑完一个 mock 任务后，阶段耗时/步数必须已落库。"""
    from sortie_deck.models import CreateInitiativeRequest, HitlAction, HitlDecision
    from sortie_deck.orchestrator import Orchestrator
    from sortie_deck.settings import Settings

    cfg = Settings(
        data_dir=tmp_path / "data",
        deploy_executor="mock",
        kb_backend="local",
        pipeline_llm=False,
        room_llm=False,
    )
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_and_start(
            CreateInitiativeRequest(
                title="metrics probe",
                brief="一行改文案",
                coding_executor="mock",
                harness="mock",
                pipeline_track="express",
                auto_start=True,
                auto_confirm_pipeline=True,
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.05)
            current = orch.get(ini.id)
            assert current
            if current.status.value == "waiting_hitl":
                current = await orch.submit_hitl(
                    ini.id, HitlDecision(action=HitlAction.APPROVE)
                )
            ini = current
            if ini.status.value in {"done", "failed"}:
                break

        rows = orch.metrics.list_stages(ini.id)
        assert rows, "阶段指标未落库"
        assert cfg.metrics_sqlite.exists()
        assert {r["stage"] for r in rows} >= {"intake"}
        assert all(r["duration_ms"] >= 0 for r in rows)
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
async def test_insight_list_exposes_execution_window(monkeypatch) -> None:
    """列表接口暴露执行起止；无指标的老任务必须是 None，否则前端会把 0 渲染成 1970 年。"""
    from importlib import import_module

    from sortie_deck.auth import PublicUser
    from sortie_deck.models import Initiative

    app_mod = import_module("sortie_deck.api.app")
    now = time.time()
    ini_with = Initiative(title="有指标", brief="b", created_by="usr_owner")
    ini_old = Initiative(title="老任务", brief="b", created_by="usr_owner")

    class _Metrics:
        def list_missions(self, ids):
            assert ini_with.id in ids
            return {
                ini_with.id: {
                    "stage_count": 2,
                    "total_duration_ms": 4500,
                    "tool_calls": 10,
                    "review_rounds": 2,
                    "first_started_at": now,
                    "last_ended_at": now + 5,
                }
            }

    class _Orch:
        metrics = _Metrics()

        def list_initiatives(self):
            return [ini_with, ini_old]

    monkeypatch.setattr(app_mod, "orch", lambda: _Orch())
    user = PublicUser(
        id="usr_owner", username="owner", display_name="owner", role="member"
    )  # type: ignore[arg-type]
    data = await app_mod.list_insight_missions(limit=10, user=user)
    by_id = {m["id"]: m for m in data["missions"]}
    assert by_id[ini_with.id]["first_started_at"] == pytest.approx(now)
    assert by_id[ini_with.id]["last_ended_at"] == pytest.approx(now + 5)
    assert by_id[ini_with.id]["created_at"]
    assert by_id[ini_old.id]["first_started_at"] is None
    assert by_id[ini_old.id]["last_ended_at"] is None
