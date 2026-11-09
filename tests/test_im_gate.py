"""门禁触达：优先审批卡片、失败降级文本、决策登记不重复触达。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sortie_deck.decisions import DecisionService, DecisionStore, set_decision_service
from sortie_deck.git_cred import new_secret_key
from sortie_deck.im import ImConnectionManager, ImConnectionStore
from sortie_deck.im.base import SendResult
from sortie_deck.im.router import ImDedupe, ImRouter
from sortie_deck.im.service import ImStack, set_stack
from sortie_deck.models import Initiative
from sortie_deck.orchestrator import Orchestrator


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, Initiative] = {}

    def upsert(self, ini: Initiative) -> Initiative:
        self.rows[ini.id] = ini
        return ini

    def get(self, initiative_id: str):
        return self.rows.get(initiative_id)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, *, title, body, meta=None, level="summary", limit=800):
        self.sent.append({"title": title, "body": body, "level": level})
        return []


class FakeProvider:
    name = "feishu"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.cards: list = []

    async def probe(self, conn):
        return True, ""

    async def send_card(self, conn, chat_id, card):
        self.cards.append({"chat_id": chat_id, "card": card})
        return SendResult(ok=self.ok, provider="feishu", error="" if self.ok else "boom")


def make_orchestrator(tmp_path, *, provider: FakeProvider | None = None):
    """手搓一个最小 Orchestrator：只保留门禁触达用到的属性。"""
    cfg = SimpleNamespace(
        bridges_db=tmp_path / "bridges.json",
        public_base_url="https://sortie.example.com",
        im_escalation=True,
        decision_sla_seconds=1800.0,
        decision_fallback_action="pause",
        webhook_timeout=5.0,
    )
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = cfg
    orch.store = FakeStore()
    orch.notifier = FakeNotifier()

    # 决策中心：真实实例（落 tmp_path），notifier 关掉避免与卡片重复触达
    set_decision_service(
        DecisionService(
            store=DecisionStore(tmp_path / "decisions.json"),
            notifier=None,
            enabled=True,
        )
    )

    if provider is not None:
        store = ImConnectionStore(tmp_path / "im.sqlite", secret_key=new_secret_key())
        conn = store.create(provider="feishu", mode="stream", app_id="cli", secret="s")
        store.set_status(conn.id, "connecting")
        store.set_status(conn.id, "connected")
        from sortie_deck.im.identity import ImIdentityStore, LinkCodeService
        from sortie_deck.im.qr_login import QrLoginService
        from sortie_deck.im.runner import ImRunner

        identity = ImIdentityStore(tmp_path / "im_identities.sqlite")
        manager = ImConnectionManager(store=store, providers={"feishu": provider})
        router = ImRouter(
            store=store,
            dedupe=ImDedupe(tmp_path / "im_events.sqlite"),
            identity=identity,
            link_codes=LinkCodeService(),
        )
        set_stack(
            ImStack(
                store=store,
                manager=manager,
                router=router,
                dedupe=router.dedupe,
                qr=QrLoginService(store=store),
                runner=ImRunner(store=store, router=router, providers={"feishu": provider}),
                identity=ImIdentityStore(tmp_path / "im_identities.sqlite"),
                link_codes=LinkCodeService(),
            )
        )
        # 新范式：门禁按「任务创建者的 IM 身份」推送，不再有会话→任务映射
        identity.bind(
            provider="feishu", sender_id="oc_1", user_id="usr_1", username="alice"
        )
    else:
        set_stack(None)

    return orch


@pytest.fixture(autouse=True)
def _reset_globals():
    yield
    set_decision_service(None)
    set_stack(None)


async def test_gate_sends_card_when_bound(tmp_path):
    provider = FakeProvider(ok=True)
    orch = make_orchestrator(tmp_path, provider=provider)
    ini = Initiative(id="ini_1", title="任务 A", brief="x", created_by="usr_1")
    orch.store.upsert(ini)

    await Orchestrator._notify_gate(
        orch, ini, "eng_build", "阶段 eng_build 等待确认", "hitl", {"prompt": "确认放行？"}
    )

    assert len(provider.cards) == 1
    sent = provider.cards[0]
    assert sent["chat_id"] == "oc_1"
    card = sent["card"]
    assert card.decision_id
    assert card.stage == "eng_build"
    assert [b.key for b in card.buttons] == ["approve", "rewrite", "reject", "stop"]
    assert card.fallback_url.endswith("/missions/ini_1")
    # 卡片发出去了就不该再发全局文本（否则用户收到两条）
    assert orch.notifier.sent == []


async def test_gate_falls_back_to_text_when_card_fails(tmp_path):
    provider = FakeProvider(ok=False)
    orch = make_orchestrator(tmp_path, provider=provider)
    ini = Initiative(id="ini_1", title="任务 A", brief="x", created_by="usr_1")

    await Orchestrator._notify_gate(
        orch, ini, "eng_build", "阶段等待确认", "hitl", {"prompt": "确认？"}
    )

    assert len(provider.cards) == 1
    # 卡片失败必须降级，不能静默（人可能在群里，工作台没人看）
    assert len(orch.notifier.sent) == 1
    assert "eng_build" in orch.notifier.sent[0]["title"]


async def test_gate_falls_back_when_no_binding(tmp_path):
    orch = make_orchestrator(tmp_path, provider=None)
    ini = Initiative(id="ini_1", title="任务 A", brief="x", created_by="usr_1")

    await Orchestrator._notify_gate(
        orch, ini, "eng_build", "阶段等待确认", "hitl", {"prompt": "确认？"}
    )
    assert len(orch.notifier.sent) == 1


async def test_gate_records_pending_decision_for_idempotency(tmp_path):
    """卡片按钮要能幂等，所以必须先有 decision_id 并写进 meta。"""
    provider = FakeProvider(ok=True)
    orch = make_orchestrator(tmp_path, provider=provider)
    ini = Initiative(id="ini_1", title="任务 A", brief="x", created_by="usr_1")

    await Orchestrator._notify_gate(
        orch, ini, "eng_build", "阶段等待确认", "hitl", {"prompt": "确认？"}
    )

    stored = orch.store.get("ini_1")
    decision_id = stored.meta.get("pending_decision_id")
    assert decision_id
    assert provider.cards[0]["card"].decision_id == decision_id

    from sortie_deck.decisions import get_decision_service

    decision = get_decision_service().get(decision_id)
    assert decision.status == "pending"
    assert decision.initiative_id == "ini_1"
    # 登记时不触达：触达由卡片负责，避免一条门禁两条消息
    assert decision.notifications == []


async def test_gate_reuses_pending_decision_on_reentry(tmp_path):
    """同一阶段重复触发门禁不能每次都新建决策，否则卡片会互相打架。"""
    provider = FakeProvider(ok=True)
    orch = make_orchestrator(tmp_path, provider=provider)
    ini = Initiative(id="ini_1", title="任务 A", brief="x", created_by="usr_1")

    await Orchestrator._notify_gate(
        orch, ini, "eng_build", "等待确认", "hitl", {"prompt": "确认？"}
    )
    await Orchestrator._notify_gate(
        orch, ini, "eng_build", "等待确认", "hitl", {"prompt": "确认？"}
    )

    from sortie_deck.decisions import get_decision_service

    pending = get_decision_service().list(initiative_id="ini_1", status="pending")
    assert len(pending) == 1
    # 两次发的卡片指向同一条决策
    assert provider.cards[0]["card"].decision_id == provider.cards[1]["card"].decision_id


async def test_gate_skips_card_when_im_disabled(tmp_path):
    """im_escalation=False：不建决策、不发卡片，只走全局文本。"""
    provider = FakeProvider(ok=True)
    orch = make_orchestrator(tmp_path, provider=provider)
    orch.cfg.im_escalation = False
    ini = Initiative(id="ini_1", title="任务 A", brief="x", created_by="usr_1")

    await Orchestrator._notify_gate(
        orch, ini, "eng_build", "等待确认", "hitl", {"prompt": "确认？"}
    )

    assert provider.cards == []
    assert len(orch.notifier.sent) == 1
