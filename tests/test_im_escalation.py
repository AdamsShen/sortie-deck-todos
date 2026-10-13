from __future__ import annotations

from pathlib import Path

import pytest

from sortie_deck.bridges import (
    PROVIDERS,
    BridgeMapping,
    BridgeStore,
    dingtalk_text_from_payload,
    feishu_text_from_payload,
    slack_text_from_payload,
)
from sortie_deck.decisions import get_decision_service
from sortie_deck.models import CreateInitiativeRequest, InitiativeStatus
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.settings import Settings

# --- inbound parsing ------------------------------------------------------


def test_dingtalk_robot_callback_shape_is_parsed() -> None:
    channel, author, text = dingtalk_text_from_payload(
        {"conversationId": "cid_1", "senderNick": "Ada", "text": {"content": "同意按 PRD"}}
    )
    assert channel == "cid_1"
    assert author == "Ada"
    assert text == "同意按 PRD"


def test_dingtalk_relay_shape_is_parsed() -> None:
    channel, author, text = dingtalk_text_from_payload(
        {"conversation_id": "cid_2", "content": "/approve"}
    )
    assert channel == "cid_2"
    assert author == "dingtalk"
    assert text == "/approve"


def test_dingtalk_without_channel_or_text_is_empty() -> None:
    channel, _author, text = dingtalk_text_from_payload({})
    assert channel is None
    assert text == ""


def test_other_providers_still_parse() -> None:
    assert slack_text_from_payload(
        {"channel_id": "c", "user_name": "a", "text": "hi"}
    ) == ("c", "a", "hi")
    channel, _author, text = feishu_text_from_payload(
        {
            "event": {
                "message": {"chat_id": "fc", "content": '{"text": "hello"}'},
                "sender": {"sender_id": {"open_id": "ou_1"}},
            }
        }
    )
    assert channel == "fc"
    assert text == "hello"


def test_dingtalk_is_a_known_provider_for_bridge_mapping(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridges.json")
    store.upsert(
        BridgeMapping(channel_id="cid_1", initiative_id="ini_1", provider="dingtalk", label="qa")
    )
    assert "dingtalk" in PROVIDERS
    mapping = store.resolve("dingtalk", "cid_1")
    assert mapping is not None
    assert mapping.initiative_id == "ini_1"


# --- escalation ladder ----------------------------------------------------


@pytest.fixture()
async def orch(tmp_path: Path):
    cfg = Settings(
        data_dir=tmp_path / "data",
        deploy_executor="mock",
        im_escalation=True,
        decision_sla_seconds=60,
        decision_fallback_action="pause",
    )
    orchestrator = Orchestrator(cfg)
    await orchestrator.startup()
    try:
        yield orchestrator
    finally:
        await orchestrator.shutdown()


async def test_escalation_pauses_the_mission_and_records_the_decision(orch) -> None:
    ini = await orch.create_initiative(
        CreateInitiativeRequest(title="Escalate", brief="需求存在矛盾", creator_name="Ada")
    )
    decision = await orch.escalate_to_human(
        ini.id,
        "验收边界以哪份文档为准？",
        stage="qa_cases",
        context="PRD 与验收单冲突",
        options=["PRD", "验收单"],
        assignee="Ada",
    )
    assert decision is not None
    assert decision.status == "pending"

    current = orch.get(ini.id)
    assert current is not None
    assert current.status == InitiativeStatus.WAITING_HITL
    assert current.pending_hitl is not None
    assert current.meta.get("pending_decision_id") == decision.decision_id
    assert any("escalated to a human" in e.message for e in current.timeline)


async def test_escalation_disabled_only_records_a_timeline_note(tmp_path: Path) -> None:
    cfg = Settings(data_dir=tmp_path / "data", deploy_executor="mock", im_escalation=False)
    orchestrator = Orchestrator(cfg)
    await orchestrator.startup()
    try:
        ini = await orchestrator.create_initiative(
            CreateInitiativeRequest(title="x", brief="y", creator_name="Ada")
        )
        decision = await orchestrator.escalate_to_human(ini.id, "无人可问")
        assert decision is None
        current = orchestrator.get(ini.id)
        assert current is not None
        assert any("IM is disabled" in e.message for e in current.timeline)
    finally:
        await orchestrator.shutdown()


async def test_unauthorized_reply_is_rejected(orch) -> None:
    ini = await orch.create_initiative(
        CreateInitiativeRequest(title="Escalate", brief="b", creator_name="Ada")
    )
    decision = await orch.escalate_to_human(
        ini.id, "q", stage="build", assignee="Ada"
    )
    assert decision is not None
    outcome = await orch.resolve_decision(
        decision.decision_id, answer="按 PRD", by="Mallory"
    )
    assert outcome.accepted is False
    assert "not the bound decider" in outcome.reason
    assert orch.get(ini.id).status == InitiativeStatus.WAITING_HITL


async def test_authorized_reply_is_recorded_once(orch) -> None:
    ini = await orch.create_initiative(
        CreateInitiativeRequest(title="Escalate", brief="b", creator_name="Ada")
    )
    decision = await orch.escalate_to_human(ini.id, "q", stage="build", assignee="Ada")
    assert decision is not None

    first = await orch.resolve_decision(decision.decision_id, answer="按 PRD", by="Ada")
    assert first.accepted is True
    current = orch.get(ini.id)
    assert current is not None
    assert current.meta.get("pending_decision_id") is None

    second = await orch.resolve_decision(decision.decision_id, answer="按 PRD", by="Ada")
    assert second.accepted is False
    assert second.duplicate is True


async def test_sweep_expires_overdue_decisions(orch) -> None:
    from datetime import timedelta

    from sortie_deck.models import utc_now

    ini = await orch.create_initiative(
        CreateInitiativeRequest(title="Escalate", brief="b", creator_name="Ada")
    )
    decision = await orch.escalate_to_human(ini.id, "q", stage="build")
    assert decision is not None

    service = get_decision_service()
    assert service is not None
    stored = service.get(decision.decision_id)
    stored.deadline_at = (utc_now() - timedelta(seconds=5)).isoformat()
    stored.fallback_action = "abort"
    service.store.save(stored)

    expired = await orch.sweep_decisions()
    assert [d.decision_id for d in expired] == [decision.decision_id]
    current = orch.get(ini.id)
    assert current is not None
    assert current.status == InitiativeStatus.STOPPED
    assert any("applying `abort`" in e.message for e in current.timeline)
