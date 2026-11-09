"""入站路由（新范式）：身份 → 命令 → Service。

旧语义是"IM 会话绑定一个任务，消息进房间"；现在 IM 是操作终端：
未绑定身份的账号只允许 /bind，绑定后所有命令走 MissionService，
与 CLI / MCP 共用同一套权限。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from sortie_deck.auth import PublicUser
from sortie_deck.git_cred import new_secret_key
from sortie_deck.im import ImConnectionStore
from sortie_deck.im.base import CardAction, InboundEvent
from sortie_deck.im.commands import ImCommands
from sortie_deck.im.intent import IntentParser
from sortie_deck.im.identity import ImIdentityStore, LinkCodeService
from sortie_deck.im.router import ImDedupe, ImRouter
from sortie_deck.models import InitiativeStatus
from sortie_deck.service import Principal, ServiceError


def mk_ini(
    mid: str = "ini_1",
    status: InitiativeStatus = InitiativeStatus.RUNNING,
    title: str = "任务",
    stage: str = "intake",
    pending: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=mid,
        title=title,
        status=status,
        current_stage=stage,
        created_at=time.time(),
        pending_hitl=SimpleNamespace(stage=pending, prompt="确认放行？") if pending else None,
        meta={},
    )


class FakeService:
    def __init__(self, missions: list | None = None) -> None:
        self.missions = list(missions or [])
        self.calls: list[tuple] = []

    def list_mine(self, principal, *, status=None, limit=None):
        rows = self.missions
        if status:
            rows = [m for m in rows if m.status.value == status]
        rows = sorted(rows, key=lambda m: m.created_at, reverse=True)
        return rows[:limit] if limit else rows

    def latest(self, principal):
        rows = self.list_mine(principal)
        return rows[0] if rows else None

    def get(self, principal, mission_id):
        for m in self.missions:
            if m.id == mission_id:
                return m
        raise ServiceError("not_found", f"任务不存在: {mission_id}")

    def artifacts(self, principal, mission_id):
        return []

    async def create_mission(self, principal, *, brief, **kwargs):
        self.calls.append(("create", brief))
        return mk_ini("ini_new", InitiativeStatus.DRAFT, title=brief[:10])

    async def confirm(self, principal, mission_id):
        self.calls.append(("confirm", mission_id))
        return mk_ini(mission_id)

    async def start(self, principal, mission_id):
        self.calls.append(("start", mission_id))
        return mk_ini(mission_id, InitiativeStatus.RUNNING, stage="product_prd")

    async def hitl(self, principal, mission_id, *, action, instruction=None, note=None):
        self.calls.append(("hitl", mission_id, action, note or instruction))
        return mk_ini(mission_id, InitiativeStatus.RUNNING, stage="eng_build")

    async def stop(self, principal, mission_id):
        self.calls.append(("stop", mission_id))
        return mk_ini(mission_id, InitiativeStatus.STOPPED)


def make_principal(name: str = "alice") -> Principal:
    return Principal(
        user=PublicUser(
            id="usr_1", username=name, display_name=name, role="member", status="active"
        ),
        source="im",
    )


@pytest.fixture()
def store(tmp_path) -> ImConnectionStore:
    return ImConnectionStore(tmp_path / "im.sqlite", secret_key=new_secret_key())


@pytest.fixture()
def identity(tmp_path) -> ImIdentityStore:
    return ImIdentityStore(tmp_path / "im_identities.sqlite")


class Replies:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    async def __call__(self, provider: str, chat_id: str, text: str) -> None:
        self.items.append((provider, chat_id, text))

    @property
    def last(self) -> str:
        return self.items[-1][2] if self.items else ""


def build_router(
    store,
    identity,
    *,
    service=None,
    principal=None,
    replies=None,
    background=None,
    orchestrator=None,
    intent=None,
) -> tuple[ImRouter, Replies]:
    reply = replies or Replies()
    router = ImRouter(
        store=store,
        dedupe=ImDedupe(store.path.parent / "dedupe.sqlite"),
        identity=identity,
        link_codes=LinkCodeService(),
        commands=ImCommands(service_factory=lambda: service or FakeService()),
        intent=intent if intent is not None else IntentParser(llm_factory=lambda: None),
        orchestrator_factory=lambda: orchestrator,
        background=background,
        reply=reply,
    )
    fixed = principal or make_principal()
    router._principal_for = lambda event: fixed  # type: ignore[method-assign]
    return router, reply


def ev(text: str, raw: str = "r1", **kwargs) -> InboundEvent:
    base = {
        "provider": "feishu",
        "channel_id": "oc_1",
        "sender_id": "ou_a",
        "sender_name": "alice",
        "text": text,
        "raw_id": raw,
    }
    base.update(kwargs)
    return InboundEvent(**base)


# --- 身份 ---------------------------------------------------------------


async def test_unbound_user_is_told_how_to_bind(store, identity):
    replies = Replies()
    router = ImRouter(
        store=store,
        dedupe=ImDedupe(store.path.parent / "d.sqlite"),
        identity=identity,
        link_codes=LinkCodeService(),
        reply=replies,
    )
    # 不注入 principal，走真实解析：没绑任何身份 → None
    result = await router.dispatch(ev("/list", raw="r_unbound"))
    assert result.accepted is False
    assert result.reason == "identity not bound"
    assert "/bind" in replies.last and "IM 集成" in replies.last


async def test_bind_command_creates_identity(store, identity):
    replies = Replies()
    router = ImRouter(
        store=store,
        dedupe=ImDedupe(store.path.parent / "d2.sqlite"),
        identity=identity,
        link_codes=LinkCodeService(),
        reply=replies,
    )
    code, _ = router.link_codes.issue(user_id="usr_1", username="alice")
    result = await router.dispatch(ev(f"/bind {code}", raw="r_bind"))
    assert result.accepted is True
    assert identity.resolve("feishu", "ou_a").username == "alice"
    assert "alice" in replies.last


async def test_invalid_code_does_not_bind(store, identity):
    replies = Replies()
    router = ImRouter(
        store=store,
        dedupe=ImDedupe(store.path.parent / "d3.sqlite"),
        identity=identity,
        link_codes=LinkCodeService(),
        reply=replies,
    )
    await router.dispatch(ev("/bind WRONG1", raw="r_bad"))
    assert identity.resolve("feishu", "ou_a") is None
    assert "无效" in replies.last


# --- 命令 ---------------------------------------------------------------


async def test_help(store, identity):
    router, replies = build_router(store, identity)
    result = await router.dispatch(ev("/help", raw="r_help"))
    assert result.accepted is True
    assert "/new" in replies.last and "/approve" in replies.last


async def test_new_creates_mission_and_asks_to_confirm(store, identity):
    service = FakeService()
    router, replies = build_router(store, identity, service=service)
    result = await router.dispatch(ev("/new 做一个用户查询接口", raw="r_new"))
    assert result.accepted is True
    assert result.initiative_id == "ini_new"
    assert service.calls[0] == ("create", "做一个用户查询接口")
    # 两步创建：必须先提示 confirm，不能自动开跑
    assert "/confirm ini_new" in replies.last


async def test_confirm_starts_mission(store, identity):
    service = FakeService([mk_ini("ini_d1", InitiativeStatus.DRAFT)])
    router, replies = build_router(store, identity, service=service)
    result = await router.dispatch(ev("/confirm ini_d1", raw="r_conf"))
    assert result.accepted is True
    assert ("start", "ini_d1") in service.calls
    assert "已启动" in replies.last


async def test_list_shows_my_missions(store, identity):
    service = FakeService(
        [
            mk_ini("ini_b", InitiativeStatus.WAITING_HITL, "任务B"),
            mk_ini("ini_a", InitiativeStatus.DONE, "任务A"),
        ]
    )
    router, replies = build_router(store, identity, service=service)
    await router.dispatch(ev("/list", raw="r_list"))
    assert "ini_a" in replies.last and "ini_b" in replies.last
    assert "待审批" in replies.last and "已完成" in replies.last


async def test_running_filters_status(store, identity):
    service = FakeService(
        [
            mk_ini("ini_run", InitiativeStatus.RUNNING),
            mk_ini("ini_done", InitiativeStatus.DONE),
            mk_ini("ini_hitl", InitiativeStatus.WAITING_HITL),
        ]
    )
    router, replies = build_router(store, identity, service=service)
    await router.dispatch(ev("/running", raw="r_run"))
    assert "ini_run" in replies.last and "ini_hitl" in replies.last
    assert "ini_done" not in replies.last


async def test_status_without_id_uses_latest(store, identity):
    service = FakeService([mk_ini("ini_latest", InitiativeStatus.RUNNING)])
    router, replies = build_router(store, identity, service=service)
    await router.dispatch(ev("/status", raw="r_status"))
    assert "ini_latest" in replies.last


async def test_approve_without_id_targets_pending_hitl(store, identity):
    """`/approve` 省略 ID 时落到最近一个待审批任务——IM 里不该逼用户记 ID。"""
    service = FakeService(
        [
            mk_ini("ini_running", InitiativeStatus.RUNNING),
            mk_ini("ini_pending", InitiativeStatus.WAITING_HITL, pending="eng_build"),
        ]
    )
    router, replies = build_router(store, identity, service=service)
    result = await router.dispatch(ev("/approve", raw="r_appr"))
    assert result.accepted is True
    assert ("hitl", "ini_pending", "approve", None) in service.calls
    assert "已放行" in replies.last


async def test_approve_with_id_and_note(store, identity):
    service = FakeService([mk_ini("ini_x", InitiativeStatus.WAITING_HITL)])
    router, _ = build_router(store, identity, service=service)
    await router.dispatch(ev("/approve ini_x 方案没问题", raw="r_appr2"))
    assert ("hitl", "ini_x", "approve", "方案没问题") in service.calls


async def test_reject_requires_reason(store, identity):
    service = FakeService([mk_ini("ini_x", InitiativeStatus.WAITING_HITL)])
    router, replies = build_router(store, identity, service=service)
    await router.dispatch(ev("/reject ini_x PRD 缺验收标准", raw="r_rej"))
    assert ("hitl", "ini_x", "reject", "PRD 缺验收标准") in service.calls
    assert "PRD 缺验收标准" in replies.last


async def test_stop_without_id_targets_running(store, identity):
    service = FakeService(
        [
            mk_ini("ini_run", InitiativeStatus.RUNNING),
            mk_ini("ini_pending", InitiativeStatus.WAITING_HITL),
        ]
    )
    router, replies = build_router(store, identity, service=service)
    await router.dispatch(ev("/stop", raw="r_stop"))
    assert ("stop", "ini_run") in service.calls
    assert "已停止" in replies.last


async def test_service_error_is_shown_to_user(store, identity):
    """权限/状态类错误要原样回给用户，而不是吞掉。"""
    service = FakeService([mk_ini("ini_x", InitiativeStatus.DONE)])

    async def deny(principal, mission_id, *, action, instruction=None, note=None):
        raise ServiceError("forbidden", "你没有该任务的控制权限")

    service.hitl = deny  # type: ignore[assignment]
    router, replies = build_router(store, identity, service=service)
    result = await router.dispatch(ev("/approve ini_x", raw="r_deny"))
    assert result.accepted is False
    assert "控制权限" in replies.last


async def test_plain_chitchat_gets_hint(store, identity):
    """听不懂时给可操作提示，而不是"我只接受命令"这种拒人千里的回复。"""
    router, replies = build_router(store, identity)
    result = await router.dispatch(ev("今天天气不错", raw="r_plain"))
    assert result.accepted is False
    assert "有哪些任务" in replies.last or "/help" in replies.last


async def test_natural_language_list_works(store, identity):
    """用户原话「目前有什么任务」必须能查——这是这次改动的起点。"""
    service = FakeService([mk_ini("ini_a", InitiativeStatus.DONE, "任务A")])
    router, replies = build_router(store, identity, service=service)
    result = await router.dispatch(ev("目前有什么任务", raw="r_nl_list"))
    assert result.accepted is True
    assert "ini_a" in replies.last


async def test_natural_language_running(store, identity):
    service = FakeService(
        [
            mk_ini("ini_run", InitiativeStatus.RUNNING),
            mk_ini("ini_done", InitiativeStatus.DONE),
        ]
    )
    router, replies = build_router(store, identity, service=service)
    await router.dispatch(ev("有哪些运行中的任务", raw="r_nl_run"))
    assert "ini_run" in replies.last


class FakeLlm:
    def __init__(self, data) -> None:
        self._data = data
        self.enabled = True

    async def chat_json(self, *, system, user, temperature=0.0):
        return self._data


async def test_natural_language_write_is_blocked(store, identity):
    """自然语言解析出 approve 也不能执行——否则群里一句"通过吧"就放行门禁。"""
    service = FakeService([mk_ini("ini_x", InitiativeStatus.WAITING_HITL)])
    router, replies = build_router(
        store,
        identity,
        service=service,
        intent=IntentParser(
            llm_factory=lambda: FakeLlm({"command": "approve", "args": ["ini_x"]})
        ),
    )
    result = await router.dispatch(ev("这个方案我觉得可以", raw="r_nl_approve"))
    assert result.accepted is False
    assert "/approve ini_x" in replies.last
    assert service.calls == []  # 一次 hitl 都没调


async def test_natural_language_new_is_allowed(store, identity):
    """`new` 只建草稿不启动，允许口语创建。"""
    service = FakeService()
    router, replies = build_router(
        store,
        identity,
        service=service,
        intent=IntentParser(
            llm_factory=lambda: FakeLlm(
                {"command": "new", "args": ["做一个用户查询接口"]}
            )
        ),
    )
    result = await router.dispatch(ev("帮我搞个用户查询的活儿", raw="r_nl_new"))
    assert result.accepted is True
    assert ("create", "做一个用户查询接口") in service.calls


async def test_unknown_command(store, identity):
    router, replies = build_router(store, identity)
    await router.dispatch(ev("/nope", raw="r_nope"))
    assert "未知命令" in replies.last


async def test_command_with_at_suffix(store, identity):
    """群聊 @机器人 会带 /help@botname 这种后缀。"""
    router, replies = build_router(store, identity)
    await router.dispatch(ev("/help@sortie", raw="r_at"))
    assert "可用命令" in replies.last


# --- 卡片动作 -----------------------------------------------------------


class FakeOrchestrator:
    def __init__(self, accepted: bool = True, duplicate: bool = False) -> None:
        self.resolved: list[dict] = []
        self.accepted = accepted
        self.duplicate = duplicate

    async def resolve_decision(
        self, decision_id, *, answer, by, channel="", is_admin=False, action=""
    ):
        self.resolved.append(
            {"id": decision_id, "by": by, "channel": channel, "action": action}
        )
        return SimpleNamespace(accepted=self.accepted, duplicate=self.duplicate, reason="被拒绝")


async def test_card_action_uses_bound_identity(store, identity):
    orch = FakeOrchestrator()
    router, _ = build_router(store, identity, orchestrator=orch)
    await router.dispatch(
        ev(
            "",
            raw="r_card",
            action=CardAction(action="approve", decision_id="dc_1", operator_id="ou_a"),
        )
    )
    call = orch.resolved[0]
    assert call["by"] == "alice"  # 绑定用户名，不是平台昵称
    assert call["action"] == "approve"
    assert call["channel"] == "im:feishu"


async def test_card_action_without_decision_id(store, identity):
    orch = FakeOrchestrator()
    router, replies = build_router(store, identity, orchestrator=orch)
    result = await router.dispatch(
        ev("", raw="r_card2", action=CardAction(action="approve", decision_id=""))
    )
    assert result.accepted is False
    assert orch.resolved == []


# --- 基础设施 -----------------------------------------------------------


async def test_duplicate_event_dropped(store, identity):
    service = FakeService()
    router, _ = build_router(store, identity, service=service)
    first = await router.dispatch(ev("/list", raw="dup_1"))
    second = await router.dispatch(ev("/list", raw="dup_1"))
    assert first.accepted is True
    assert second.duplicate is True


def test_dedupe_survives_new_instance(store, identity, tmp_path):
    from sortie_deck.im.router import ImDedupe as D

    a = D(tmp_path / "d.sqlite")
    assert a.claim("feishu", "x") is True
    b = D(tmp_path / "d.sqlite")
    assert b.claim("feishu", "x") is False


def test_dedupe_without_raw_id_passes(store, tmp_path):
    from sortie_deck.im.router import ImDedupe as D

    d = D(tmp_path / "d2.sqlite")
    assert d.claim("slack", "") is True
    assert d.claim("slack", "") is True


async def test_schedule_returns_immediately(store, identity):
    calls: list = []

    def fake_background(awaitable):
        calls.append(awaitable)
        awaitable.close()
        return None

    router, _ = build_router(store, identity, background=fake_background)
    router.schedule(ev("/help", raw="r_sched"))
    assert len(calls) == 1


async def test_ordinal_detail_uses_previous_list(store, identity):
    """「看一下第一个任务详情」要落到上一轮列表的第一条，而不是再回一遍列表。"""
    service = FakeService(
        [
            mk_ini("ini_old", InitiativeStatus.DONE, "旧任务"),
            mk_ini("ini_new", InitiativeStatus.RUNNING, "新任务"),
        ]
    )
    service.missions[0].created_at = 1.0
    service.missions[1].created_at = 2.0
    router, replies = build_router(store, identity, service=service)

    await router.dispatch(ev("目前有什么任务", raw="r_ctx_1"))
    result = await router.dispatch(ev("看一下第一个任务详情", raw="r_ctx_2"))

    assert result.initiative_id == "ini_new"
    assert "ini_new" in replies.last
    assert "你最近" not in replies.last


async def test_ordinal_without_previous_list_uses_latest(store, identity):
    """第一句就说"第一个"：按最近任务兜底，比回"我不知道你说哪个"更符合直觉。"""
    service = FakeService(
        [
            mk_ini("ini_a", InitiativeStatus.DONE),
            mk_ini("ini_b", InitiativeStatus.RUNNING),
        ]
    )
    service.missions[0].created_at = 1.0
    service.missions[1].created_at = 2.0
    router, replies = build_router(store, identity, service=service)

    result = await router.dispatch(ev("看一下第一个任务详情", raw="r_no_ctx"))
    assert result.initiative_id == "ini_b"


async def test_ordinal_out_of_range(store, identity):
    service = FakeService([mk_ini("ini_only", InitiativeStatus.DONE)])
    router, replies = build_router(store, identity, service=service)
    result = await router.dispatch(ev("第五个任务详情", raw="r_oor"))
    assert result.accepted is False
    assert "只有 1 个任务" in replies.last


async def test_last_ordinal(store, identity):
    service = FakeService(
        [
            mk_ini("ini_first", InitiativeStatus.DONE),
            mk_ini("ini_last", InitiativeStatus.DONE),
        ]
    )
    service.missions[0].created_at = 2.0
    service.missions[1].created_at = 1.0
    router, replies = build_router(store, identity, service=service)
    await router.dispatch(ev("看一下最后一个任务详情", raw="r_last"))
    assert "ini_last" in replies.last
