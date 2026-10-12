from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from sortie_deck import notify as notify_module
from sortie_deck.decisions import (
    DecisionService,
    DecisionStore,
    HumanDecision,
)
from sortie_deck.models import utc_now
from sortie_deck.notify import (
    NotificationChannel,
    Notifier,
    build_payload,
    dingtalk_sign,
    redact,
    signed_url,
)

# --- redaction ------------------------------------------------------------


def test_redact_strips_secrets_and_tokens() -> None:
    text = "call with api_key=abc123 and Bearer deadbeef and sk-abcdef123456"
    cleaned = redact(text)
    assert "abc123" not in cleaned
    assert "deadbeef" not in cleaned
    assert "sk-abcdef123456" not in cleaned
    assert cleaned.count("[REDACTED]") == 3


def test_redact_truncates_long_bodies() -> None:
    cleaned = redact("x" * 500, limit=50)
    assert len(cleaned) <= 50
    assert cleaned.endswith("...")


def test_redact_keeps_short_clean_text() -> None:
    assert redact("hello") == "hello"


# --- dingtalk signing -----------------------------------------------------


def test_dingtalk_sign_is_deterministic() -> None:
    first = dingtalk_sign("secret", 1700000000000)
    second = dingtalk_sign("secret", 1700000000000)
    assert first == second
    sign, ts = first
    assert ts == 1700000000000
    assert sign


def test_signed_url_appends_params_only_when_secret_present() -> None:
    url = "https://oapi.dingtalk.com/robot/send?access_token=t"
    assert signed_url(url, "") == url
    signed = signed_url(url, "secret")
    assert "timestamp=" in signed
    assert "sign=" in signed
    assert signed.startswith(url)


# --- payloads -------------------------------------------------------------


def test_build_payload_matches_each_provider() -> None:
    assert build_payload("dingtalk", title="t", body="b")["msgtype"] == "text"
    assert build_payload("feishu", title="t", body="b")["msg_type"] == "text"
    assert build_payload("webhook", title="t", body="b")["event"] == "decision"


async def test_notifier_without_channels_sends_nothing() -> None:
    notifier = Notifier()
    assert await notifier.send(title="t", body="b") == []


async def test_notifier_disabled_sends_nothing() -> None:
    notifier = Notifier(
        channels=[NotificationChannel(provider="dingtalk", url="https://example.invalid")],
        enabled=False,
    )
    assert await notifier.send(title="t", body="b") == []


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    calls: list[tuple[str, dict[str, Any]]] = []
    status = 200

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        _FakeClient.calls.append((url, json or {}))
        return _FakeResponse(_FakeClient.status)


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch):
    _FakeClient.calls = []
    _FakeClient.status = 200
    monkeypatch.setattr(notify_module.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


async def test_notifier_posts_redacted_body(fake_http) -> None:
    notifier = Notifier(
        channels=[NotificationChannel(provider="dingtalk", url="https://example.invalid/robot")],
        enabled=True,
    )
    results = await notifier.send(title="需要决策", body="token=supersecret please decide")
    assert len(results) == 1
    assert results[0].ok is True
    _url, payload = fake_http.calls[0]
    assert "supersecret" not in payload["text"]["content"]
    assert "[REDACTED]" in payload["text"]["content"]


async def test_notifier_reports_channel_failure(fake_http) -> None:
    fake_http.status = 500
    notifier = Notifier(
        channels=[NotificationChannel(provider="feishu", url="https://example.invalid/hook")],
        enabled=True,
    )
    results = await notifier.send(title="t", body="b")
    assert results[0].ok is False
    assert "500" in results[0].error


async def test_notifier_survives_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom(_FakeClient):
        async def post(self, url: str, json: dict[str, Any] | None = None):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(notify_module.httpx, "AsyncClient", _Boom)
    notifier = Notifier(
        channels=[NotificationChannel(provider="dingtalk", url="https://example.invalid")],
        enabled=True,
    )
    results = await notifier.send(title="t", body="b")
    assert results[0].ok is False
    assert "connection refused" in results[0].error


# --- decision service -----------------------------------------------------


@pytest.fixture()
def service(tmp_path: Path) -> DecisionService:
    return DecisionService(
        store=DecisionStore(tmp_path / "decisions.json"),
        notifier=Notifier(enabled=False),
        default_sla_seconds=600,
    )


async def test_raise_decision_creates_a_pending_record(service: DecisionService) -> None:
    decision = await service.raise_decision(
        initiative_id="ini_1",
        stage="build",
        question="边界按 PRD 还是按验收单？",
        context="api_key=secret",
        options=["prd", "acceptance"],
        assignee="ada",
    )
    assert decision.status == "pending"
    assert decision.assignee == "ada"
    assert "secret" not in decision.context
    assert decision.deadline_at


async def test_raise_decision_is_idempotent_per_stage(service: DecisionService) -> None:
    first = await service.raise_decision(
        initiative_id="ini_1", stage="build", question="same question"
    )
    second = await service.raise_decision(
        initiative_id="ini_1", stage="build", question="same question"
    )
    assert first.decision_id == second.decision_id
    assert len(service.list(initiative_id="ini_1")) == 1


async def test_only_the_bound_decider_may_answer(service: DecisionService) -> None:
    decision = await service.raise_decision(
        initiative_id="ini_1", stage="build", question="q", assignee="ada"
    )
    denied = service.answer(decision.decision_id, answer="x", by="mallory")
    assert denied.accepted is False
    assert "not the bound decider" in denied.reason

    allowed = service.answer(decision.decision_id, answer="按 PRD", by="ada")
    assert allowed.accepted is True
    assert allowed.decision.status == "answered"


async def test_any_operator_can_answer_when_unassigned(service: DecisionService) -> None:
    decision = await service.raise_decision(initiative_id="ini_1", stage="build", question="q")
    outcome = service.answer(decision.decision_id, answer="ok", by="someone")
    assert outcome.accepted is True


async def test_duplicate_answer_is_a_noop(service: DecisionService) -> None:
    decision = await service.raise_decision(initiative_id="ini_1", stage="build", question="q")
    first = service.answer(decision.decision_id, answer="first", by="ada")
    second = service.answer(decision.decision_id, answer="second", by="ada")
    assert first.accepted is True
    assert second.accepted is False
    assert second.duplicate is True
    assert service.get(decision.decision_id).answer == "first"


async def test_admin_override_can_answer(service: DecisionService) -> None:
    decision = await service.raise_decision(
        initiative_id="ini_1", stage="build", question="q", assignee="ada"
    )
    outcome = service.answer(decision.decision_id, answer="override", by="root", is_admin=True)
    assert outcome.accepted is True


async def test_expire_due_applies_the_documented_fallback(service: DecisionService) -> None:
    decision = await service.raise_decision(
        initiative_id="ini_1",
        stage="build",
        question="q",
        sla_seconds=1,
        fallback_action="abort",
    )
    store = service.store
    decision.deadline_at = (utc_now() - timedelta(seconds=1)).isoformat()
    store.save(decision)

    expired = service.expire_due()
    assert [d.decision_id for d in expired] == [decision.decision_id]
    assert expired[0].status == "expired"
    assert "abort" in expired[0].fallback_reason
    assert service.next_fallback("ini_1") == "abort"


async def test_expired_decision_cannot_be_answered(service: DecisionService) -> None:
    decision = await service.raise_decision(
        initiative_id="ini_1", stage="build", question="q", sla_seconds=1
    )
    decision.deadline_at = (utc_now() - timedelta(seconds=1)).isoformat()
    service.store.save(decision)
    service.expire_due()
    outcome = service.answer(decision.decision_id, answer="late", by="ada")
    assert outcome.accepted is False
    assert "expired" in outcome.reason


async def test_unknown_decision_raises_key_error(service: DecisionService) -> None:
    with pytest.raises(KeyError):
        service.answer("dc_nope", answer="x", by="ada")


def test_decision_is_overdue_ignores_answered(tmp_path: Path) -> None:
    decision = HumanDecision(
        initiative_id="ini_1",
        question="q",
        status="answered",
        deadline_at=(utc_now() - timedelta(seconds=5)).isoformat(),
    )
    assert decision.is_overdue() is False


async def test_expired_decisions_are_not_reported_as_pending(service: DecisionService) -> None:
    await service.raise_decision(initiative_id="ini_1", stage="build", question="q", sla_seconds=0)
    service.expire_due()
    assert service.list(initiative_id="ini_1", status="pending") == []
