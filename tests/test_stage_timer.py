"""阶段计时锚点的回归测试。

背景：前端「执行中 Ns」原先只以 SSE 事件到达时间为锚点，页面一刷新锚点丢失，
计时从 0 重新开始。锚点改为服务端持久化后，这里锁定四条契约：
1. 进入阶段时写入 stage_started_at / stage_started_stage 并落盘；
2. 该字段能被详情接口序列化（刷新后前端读得到）；
3. 任务不存在时安全返回空串，不抛异常；
4. 阶段入口同时推进 current_stage，与 stage_started_stage 始终保持一致
   （2026-11-10 事故：current_stage 只在阶段结束时更新，导致整段执行期间
   流程图高亮上一阶段、顶部却在给新阶段计时）。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from sortie_deck.models import Initiative
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.store import InitiativeStore


def test_mark_stage_started_persists_anchor(tmp_path):
    store = InitiativeStore(tmp_path / "initiatives.json")
    ini = Initiative(title="计时锚点", brief="刷新后继续计时")
    store.upsert(ini)

    started = Orchestrator.mark_stage_started(
        SimpleNamespace(store=store), ini.id, "eng_integrate"
    )

    assert started, "阶段开始时间必须返回 ISO 时间串"
    saved = store.get(ini.id)
    assert saved is not None
    assert saved.stage_started_stage == "eng_integrate"
    assert saved.stage_started_at is not None
    assert saved.stage_started_at.isoformat() == started
    # 重新从磁盘读取，等价于刷新页面后重新拉详情：锚点必须还在
    reopened = InitiativeStore(store.path).get(ini.id)
    assert reopened is not None
    assert reopened.stage_started_at == saved.stage_started_at


def test_mark_stage_started_advances_current_stage(tmp_path):
    """阶段入口就推进 current_stage，与计时锚点同源。

    旧实现只写时间戳，current_stage 要等 astream 回吐（阶段结束）才更新，
    于是整段执行期间两个字段互相矛盾：流程图高亮上一阶段，顶部在给新阶段计时。
    """
    store = InitiativeStore(tmp_path / "initiatives.json")
    ini = Initiative(title="阶段入口推进", brief="b", current_stage="qa_verify")
    store.upsert(ini)

    Orchestrator.mark_stage_started(SimpleNamespace(store=store), ini.id, "deploy_preview")

    saved = store.get(ini.id)
    assert saved is not None
    # 两个字段必须同源，否则刷新前后前端算出的活跃阶段不一致
    assert saved.current_stage == "deploy_preview"
    assert saved.current_stage == saved.stage_started_stage


def test_mark_stage_started_keeps_stages_in_sync_across_hops(tmp_path):
    """连续推进多个阶段时，current_stage 不得残留上一阶段的值。"""
    store = InitiativeStore(tmp_path / "initiatives.json")
    ini = Initiative(title="连续推进", brief="b")
    store.upsert(ini)
    orch = SimpleNamespace(store=store)

    for stage in ("intake", "eng_implement", "qa_verify", "deploy_preview"):
        Orchestrator.mark_stage_started(orch, ini.id, stage)
        saved = store.get(ini.id)
        assert saved is not None
        assert saved.current_stage == stage
        assert saved.stage_started_stage == stage


def test_mark_stage_started_tolerates_missing_mission(tmp_path):
    store = InitiativeStore(tmp_path / "initiatives.json")
    assert Orchestrator.mark_stage_started(SimpleNamespace(store=store), "ini_missing", "intake") == ""


def test_initiative_serializes_stage_anchor():
    ini = Initiative(title="序列化", brief="b")
    ini.stage_started_at = datetime.fromisoformat("2026-11-05T06:51:43+00:00")
    ini.stage_started_stage = "qa_verify"

    data = ini.model_dump(mode="json")

    assert data["stage_started_stage"] == "qa_verify"
    assert data["stage_started_at"].startswith("2026-11-05T06:51:43")
