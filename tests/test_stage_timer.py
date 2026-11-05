"""阶段计时锚点的回归测试。

背景：前端「执行中 Ns」原先只以 SSE 事件到达时间为锚点，页面一刷新锚点丢失，
计时从 0 重新开始。锚点改为服务端持久化后，这里锁定三条契约：
1. 进入阶段时写入 stage_started_at / stage_started_stage 并落盘；
2. 该字段能被详情接口序列化（刷新后前端读得到）；
3. 任务不存在时安全返回空串，不抛异常。
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
