"""并发执行、跨进程事件、租约续期、存储安全的回归测试。

这些用例覆盖 docs/plans/2026-10-30-mission-concurrency.md 的验收标准：
任务之间可并行、同一任务互斥、事件跨进程可读、长任务租约不被抢。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from types import SimpleNamespace

import pytest

from sortie_deck.event_log import EventLog
from sortie_deck.models import CreateInitiativeRequest, Initiative, InitiativeStatus
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.run_queue import SqliteRunQueue
from sortie_deck.runtime import validate_settings
from sortie_deck.settings import Settings
from sortie_deck.store import InitiativeStore


def _cfg(tmp_path, **overrides) -> Settings:
    base: dict = {
        "data_dir": tmp_path / "data",
        "deploy_executor": "mock",
        "kb_backend": "local",
        "pipeline_llm": False,
        "room_llm": False,
    }
    base.update(overrides)
    return Settings(**base)


async def _new_mission(orch: Orchestrator, title: str) -> Initiative:
    return await orch.create_initiative(
        CreateInitiativeRequest(
            title=title,
            brief="验证并发执行",
            coding_executor="mock",
            harness="mock",
            creator_name="Ada",
        )
    )


class _Barrier:
    """两个任务都进入 astream 后才放行；串行实现会卡在第一个。"""

    def __init__(self, target: int = 2) -> None:
        self.count = 0
        self.target = target
        self.all_entered = asyncio.Event()
        self.release = asyncio.Event()

    def enter(self) -> None:
        self.count += 1
        if self.count >= self.target:
            self.all_entered.set()


def _idle_state() -> SimpleNamespace:
    """astream 结束后的图状态：无待执行节点、无 interrupt。"""
    return SimpleNamespace(tasks=[], next=(), values={"stage": "intake", "status": "running"})


class _BarrierGraph:
    def __init__(self, barrier: _Barrier) -> None:
        self.barrier = barrier

    async def astream(self, payload, config, stream_mode=None):
        self.barrier.enter()
        await self.barrier.release.wait()
        yield {"stage": "intake", "status": "running"}

    async def aget_state(self, config):
        return _idle_state()


@pytest.mark.asyncio
async def test_run_locks_are_isolated_per_mission(tmp_path):
    orch = Orchestrator(_cfg(tmp_path))
    await orch.startup()
    try:
        lock_a = await orch._run_lock_for("ini_a")
        lock_b = await orch._run_lock_for("ini_b")
        assert lock_a is not lock_b
        # 同一任务必须复用同一把锁，否则互斥失效
        assert await orch._run_lock_for("ini_a") is lock_a
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
async def test_two_missions_run_concurrently(tmp_path, monkeypatch):
    """核心回归：不同 mission 必须能同时进入图执行（全局锁会超时）。"""
    orch = Orchestrator(_cfg(tmp_path))
    await orch.startup()
    try:
        ini_a = await _new_mission(orch, "并发 A")
        ini_b = await _new_mission(orch, "并发 B")

        barrier = _Barrier(target=2)
        monkeypatch.setattr(orch, "_graph_for", lambda _ini: _BarrierGraph(barrier))

        async def _fake_apply(ini, result):
            ini.status = InitiativeStatus.DONE
            orch.store.upsert(ini)
            return ini

        monkeypatch.setattr(orch, "_apply_graph_result", _fake_apply)

        task_a = asyncio.create_task(orch._run_until_interrupt(ini_a.id))
        task_b = asyncio.create_task(orch._run_until_interrupt(ini_b.id))
        try:
            await asyncio.wait_for(barrier.all_entered.wait(), timeout=5)
        finally:
            barrier.release.set()
        await asyncio.gather(task_a, task_b)
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
async def test_same_mission_run_is_mutually_exclusive(tmp_path, monkeypatch):
    """同一 mission 的第二次 run 必须排在第一次之后（避免双写 checkpoint）。"""
    orch = Orchestrator(_cfg(tmp_path))
    await orch.startup()
    try:
        ini = await _new_mission(orch, "互斥")
        entered = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        class _Graph:
            async def astream(self, payload, config, stream_mode=None):
                order.append("enter")
                entered.set()
                await release.wait()
                yield {"stage": "intake"}

            async def aget_state(self, config):
                return _idle_state()

        monkeypatch.setattr(orch, "_graph_for", lambda _ini: _Graph())

        async def _fake_apply(inner_ini, result):
            inner_ini.status = InitiativeStatus.DONE
            orch.store.upsert(inner_ini)
            return inner_ini

        monkeypatch.setattr(orch, "_apply_graph_result", _fake_apply)

        first = asyncio.create_task(orch._run_until_interrupt(ini.id))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(orch._run_until_interrupt(ini.id))
        await asyncio.sleep(0.2)
        assert order == ["enter"], "第二个 run 在第一个结束前就进入图执行了"

        release.set()
        await asyncio.gather(first, second)
        assert order == ["enter", "enter"]
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
async def test_emit_persists_event_with_seq(tmp_path):
    orch = Orchestrator(_cfg(tmp_path))
    await orch.startup()
    try:
        queue = orch.subscribe("ini_e")
        await orch._emit("ini_e", {"type": "progress", "stage": "intake"})

        local = await asyncio.wait_for(queue.get(), timeout=2)
        assert local.get("seq", 0) > 0

        rows = orch.events.since("ini_e", 0)
        assert [r[1]["stage"] for r in rows] == ["intake"]
    finally:
        await orch.shutdown()


def test_event_log_is_readable_across_instances(tmp_path):
    """两个实例模拟 worker 进程写 / API 进程读。"""
    path = tmp_path / "events.sqlite"
    writer = EventLog(path)
    reader = EventLog(path)
    try:
        for idx in range(3):
            writer.append("ini_x", {"type": "progress", "n": idx})
        writer.append("ini_y", {"type": "progress", "n": 99})

        rows = reader.since("ini_x", 0)
        assert [r[1]["n"] for r in rows] == [0, 1, 2]
        seqs = [r[0] for r in rows]
        assert seqs == sorted(seqs)

        # 按 seq 续读：跳过第一条后只剩后两条
        assert [r[1]["n"] for r in reader.since("ini_x", seqs[0])] == [1, 2]
        assert reader.latest_seq("ini_y") > seqs[-1]

        reader.delete_mission("ini_x")
        assert reader.since("ini_x", 0) == []
        assert reader.since("ini_y", 0), "删除一个任务不应影响其他任务的事件"
    finally:
        writer.close()
        reader.close()


@pytest.mark.asyncio
async def test_lease_heartbeat_keeps_long_job_locked(tmp_path):
    """运行超过 lease 时长后，别的 worker 仍不能重复领取该 job。"""
    cfg = _cfg(tmp_path)
    queue = SqliteRunQueue(tmp_path / "runs.sqlite", lease_seconds=1)
    orch = Orchestrator(cfg)
    orch.run_queue = queue
    try:
        queue.enqueue("ini_lease", kind="start")
        claimed = queue.claim("worker-1")
        assert claimed is not None

        heartbeat = asyncio.create_task(
            orch._lease_heartbeat(claimed.id, "worker-1", interval=0.2)
        )
        try:
            await asyncio.sleep(1.4)  # 远超 lease_seconds=1
            assert queue.claim("worker-2") is None
        finally:
            heartbeat.cancel()
            with pytest.raises(asyncio.CancelledError):
                await heartbeat
    finally:
        await orch.shutdown()


def test_same_worker_id_cannot_claim_two_jobs_of_one_mission(tmp_path):
    """两个副本配了同名 worker 时，队列仍不能放行同一 mission 的第二个 job。"""
    queue = SqliteRunQueue(tmp_path / "runs.sqlite", lease_seconds=60)
    queue.enqueue("ini_same", kind="start")
    queue.enqueue("ini_same", kind="resume", payload={"decision": {}})

    first = queue.claim("worker-1")
    assert first is not None
    assert queue.claim("worker-1") is None, "同名 worker 绕过了 mission 互斥"

    queue.complete(first.id)
    assert queue.claim("worker-1") is not None


def test_storage_backend_must_be_sqlite(tmp_path):
    """JSON 后端已移除：无论 run_backend 是什么，配 json 都必须启动失败。"""
    for run_backend in ("inline", "sqlite"):
        bad = Settings(
            data_dir=tmp_path / "data", run_backend=run_backend, storage="json"
        )
        with pytest.raises(RuntimeError, match="TDT_STORAGE 只支持 sqlite"):
            validate_settings(bad)

    good = Settings(data_dir=tmp_path / "data", run_backend="sqlite", storage="sqlite")
    validate_settings(good)
    # 默认值本身就是 sqlite
    assert Settings(data_dir=tmp_path / "data").storage == "sqlite"


@pytest.mark.asyncio
async def test_concurrent_json_upserts_do_not_lose_tasks(tmp_path):
    store = InitiativeStore(tmp_path / "initiatives.json")
    items = [Initiative(title=f"m{idx}", brief="b") for idx in range(20)]
    await asyncio.gather(*(asyncio.to_thread(store.upsert, item) for item in items))
    assert len(store.list()) == 20


@pytest.mark.asyncio
async def test_legacy_json_missions_migrate_to_sqlite(tmp_path):
    json_store = InitiativeStore(tmp_path / "data" / "initiatives.json")
    legacy = Initiative(title="legacy", brief="b")
    json_store.upsert(legacy)

    orch = Orchestrator(_cfg(tmp_path))
    await orch.startup()
    try:
        assert orch.get(legacy.id) is not None
        data_dir = tmp_path / "data"
        assert (data_dir / ".storage-migration.json").exists()
        assert list(data_dir.glob("initiatives.json.bak-*")), "迁移前必须留备份"
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
async def test_warns_when_legacy_json_written_after_migration(tmp_path, caplog):
    """迁移后 JSON 仍被写入 = 旧版本进程还在跑，必须告警（否则数据会分裂）。"""
    json_path = tmp_path / "data" / "initiatives.json"
    InitiativeStore(json_path).upsert(Initiative(title="legacy", brief="b"))

    first = Orchestrator(_cfg(tmp_path))
    await first.startup()
    await first.shutdown()

    future = time.time() + 60
    os.utime(json_path, (future, future))

    second = Orchestrator(_cfg(tmp_path))
    with caplog.at_level(logging.WARNING):
        await second.startup()
        assert any("旧版本进程" in record.message for record in caplog.records)
    await second.shutdown()


@pytest.mark.asyncio
async def test_sse_stream_replays_persisted_events(tmp_path):
    """SSE 走事件表续读：快照建立基线后，后产生的事件按 seq 送达。"""
    from sortie_deck.api.app import _mission_event_stream

    orch = Orchestrator(_cfg(tmp_path))
    await orch.startup()
    try:
        ini = await _new_mission(orch, "SSE")
        stream = _mission_event_stream(orch, ini.id, is_owner=True)
        try:
            first = await asyncio.wait_for(anext(stream), timeout=5)
            second = await asyncio.wait_for(anext(stream), timeout=5)
            assert first["event"] == "snapshot"
            assert second["event"] == "room_snapshot"

            await orch._emit(ini.id, {"type": "subroom", "token": "owner-only"})
            await orch._emit(ini.id, {"type": "progress", "stage": "intake"})

            # owner 能收到小群事件
            seen = [await asyncio.wait_for(anext(stream), timeout=5) for _ in range(2)]
            assert [item["event"] for item in seen] == ["subroom", "progress"]
            assert json.loads(seen[1]["data"])["stage"] == "intake"
        finally:
            await stream.aclose()

        # 非 owner 过滤小群事件
        other = _mission_event_stream(orch, ini.id, is_owner=False)
        try:
            await asyncio.wait_for(anext(other), timeout=5)  # snapshot
            await asyncio.wait_for(anext(other), timeout=5)  # room_snapshot
            await orch._emit(ini.id, {"type": "subroom", "token": "hidden"})
            await orch._emit(ini.id, {"type": "progress", "stage": "qa"})
            item = await asyncio.wait_for(anext(other), timeout=5)
            assert item["event"] == "progress"
        finally:
            await other.aclose()
    finally:
        await orch.shutdown()


def test_worker_cli_requires_sqlite_backend(monkeypatch):
    from sortie_deck import cli as cli_mod
    from sortie_deck.settings import settings as global_settings

    monkeypatch.setattr(global_settings, "run_backend", "inline")
    with pytest.raises(SystemExit):
        cli_mod.main(["worker"])


@pytest.mark.asyncio
async def test_run_worker_flag_controls_worker_loop(tmp_path):
    cfg_on = _cfg(tmp_path, run_backend="sqlite", storage="sqlite")
    orch_on = Orchestrator(cfg_on)
    await orch_on.startup()
    try:
        assert orch_on._worker_task is not None
    finally:
        await orch_on.shutdown()

    cfg_off = _cfg(tmp_path, run_backend="sqlite", storage="sqlite", run_worker=False)
    orch_off = Orchestrator(cfg_off)
    await orch_off.startup()
    try:
        assert orch_off._worker_task is None
    finally:
        await orch_off.shutdown()
