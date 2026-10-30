"""P2：门禁结构化 + 三端来源审计。"""

from __future__ import annotations

import asyncio

import pytest

from sortie_deck import service as service_mod
from sortie_deck.api_keys import ApiKeyStore
from sortie_deck.auth import AuthStore, CreateUserRequest
from sortie_deck.models import CreateInitiativeRequest, HitlAction
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.service import MissionService, Principal, ServiceError
from sortie_deck.settings import Settings


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    auth = AuthStore(tmp_path / "users.json", token_secret="test-secret")
    keys = ApiKeyStore(tmp_path / "api_keys.sqlite")
    monkeypatch.setattr(service_mod, "_auth", auth)
    monkeypatch.setattr(service_mod, "_api_keys", keys)
    user = auth.create_user(
        CreateUserRequest(username="ada", password="pw123456", display_name="Ada")
    )
    yield auth, user
    keys.close()


async def _mission_at_gate(tmp_path, owner_id: str | None = None):
    cfg = Settings(
        data_dir=tmp_path / "data",
        deploy_executor="mock",
        kb_backend="local",
        pipeline_llm=False,
        room_llm=False,
    )
    orch = Orchestrator(cfg)
    await orch.startup()
    ini = await orch.create_and_start(
        CreateInitiativeRequest(
            title="gate",
            brief="新增一个健康检查接口",
            coding_executor="mock",
            harness="mock",
            auto_start=True,
            auto_confirm_pipeline=True,
            created_by=owner_id,
        )
    )
    for _ in range(200):
        await asyncio.sleep(0.1)
        current = orch.get(ini.id)
        assert current is not None
        if current.status.value == "waiting_hitl":
            return orch, current
    await orch.shutdown()
    raise AssertionError("任务没有到达门禁")


@pytest.mark.asyncio
async def test_gate_payload_is_structured(tmp_path):
    orch, ini = await _mission_at_gate(tmp_path)
    try:
        hitl = ini.pending_hitl
        assert hitl is not None
        # agent 需要这些字段才能"复述在批什么"，否则只能盲批
        assert hitl.action_help, "action_help 不能为空"
        assert "approve" in hitl.action_help
        assert hitl.risk, "risk 不能为空"
        assert hitl.artifact_summary or hitl.artifacts, "至少要能说清产物"
        assert hitl.irreversible is False  # intake 不是破坏性阶段
    finally:
        await orch.shutdown()


def test_irreversible_detection():
    from sortie_deck.graph import describe_stage_risk, stage_is_irreversible

    assert stage_is_irreversible("deploy_preview") is True
    assert stage_is_irreversible("ship") is True
    assert stage_is_irreversible("product_prd") is False
    assert stage_is_irreversible("anything", {"irreversible": True}) is True
    assert "不可撤销" in describe_stage_risk("deploy_preview")
    assert "只读评审" in describe_stage_risk("product_prd")


@pytest.mark.asyncio
async def test_hitl_source_is_recorded_on_all_channels(tmp_path, stores):
    _auth, user = stores
    orch, ini = await _mission_at_gate(tmp_path, user.id)
    try:
        principal = Principal(user=user, source="cli")
        svc = MissionService(orch)
        updated = await svc.hitl(principal, ini.id, action=HitlAction.APPROVE)

        messages = [e.message for e in updated.timeline if e.kind == "hitl"]
        assert any("via 命令行" in m for m in messages), messages
        assert "（命令行）" in str((updated.meta or {}).get("last_hitl_by"))
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
async def test_second_channel_gets_conflict_with_handled_by(tmp_path, stores):
    _auth, user = stores
    orch, ini = await _mission_at_gate(tmp_path, user.id)
    try:
        svc = MissionService(orch)
        first = Principal(user=user, source="mcp")
        await svc.hitl(first, ini.id, action=HitlAction.APPROVE)

        fresh = svc.get(first, ini.id)
        assert "Claude Code" in str((fresh.meta or {}).get("last_hitl_by"))

        # 任务不在等待门禁时（这里用 stop 制造），再提交必须 conflict，
        # 且要告知"已被谁处理"，而不是静默重复执行
        await svc.stop(first, ini.id)
        with pytest.raises(ServiceError) as exc:
            await svc.hitl(first, ini.id, action=HitlAction.APPROVE)
        assert exc.value.code == "conflict"
        assert "Claude Code" in exc.value.message
    finally:
        await orch.shutdown()
