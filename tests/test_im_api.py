"""IM 管理页 API：渠道表、凭据 CRUD、探活、健康度、入站 webhook、签名动作链接。

沿用项目既有风格：直接调用路由函数 + 把 settings 指向 tmp_path，
不启动 TestClient（避免真实 data/ 被写脏、也不打真实平台接口）。
"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from sortie_deck.git_cred import new_secret_key
from sortie_deck.im import ImConnectionManager, ImConnectionStore
from sortie_deck.im.base import InboundEvent
from sortie_deck.im.cards import signature_for
from sortie_deck.im.router import ImDedupe, ImRouter
from sortie_deck.im.service import ImStack, set_stack
from sortie_deck.settings import settings

app_mod = import_module("sortie_deck.api.app")


class FakeProvider:
    """只实现管理页用到的两个方法；不碰网络。"""

    def __init__(self, *, ok: bool = True, error: str = "") -> None:
        self.ok = ok
        self.error = error
        self.parsed: InboundEvent | None = None

    async def probe(self, conn):
        return self.ok, self.error

    def parse_inbound(self, payload):
        return self.parsed


def _user(uid: str = "usr_admin", role: str = "admin") -> SimpleNamespace:
    return SimpleNamespace(id=uid, username=uid, display_name=uid, role=role)


@pytest.fixture()
def im_env(tmp_path, monkeypatch):
    """把 IM 存储与主密钥都指向临时目录，并重置全局栈。"""
    monkeypatch.setattr(settings, "im_db_path", tmp_path / "im.sqlite")
    monkeypatch.setattr(settings, "secret_key", new_secret_key())
    monkeypatch.setattr(settings, "public_base_url", "https://sortie.example.com")
    monkeypatch.setattr(settings, "token_secret", "test-token-secret")
    set_stack(None)
    yield tmp_path
    set_stack(None)


def _install_stack(tmp_path, *, providers=None, orchestrator=None) -> ImStack:
    from sortie_deck.im.identity import ImIdentityStore, LinkCodeService
    from sortie_deck.im.qr_login import QrLoginService
    from sortie_deck.im.runner import ImRunner

    store = ImConnectionStore(tmp_path / "im.sqlite", secret_key=settings.resolved_secret_key())
    manager = ImConnectionManager(store=store, providers=providers or {})
    identity = ImIdentityStore(tmp_path / "im_identities.sqlite")
    router = ImRouter(
        store=store,
        dedupe=ImDedupe(tmp_path / "im_events.sqlite"),
        identity=identity,
        link_codes=LinkCodeService(),
        orchestrator_factory=lambda: orchestrator,
    )
    stack = ImStack(
        store=store,
        manager=manager,
        router=router,
        dedupe=router.dedupe,
        qr=QrLoginService(store=store),
        runner=ImRunner(store=store, router=router, providers=providers or {}),
        identity=identity,
        link_codes=LinkCodeService(),
    )
    set_stack(stack)
    return stack


async def test_provider_catalog_lists_all_channels(im_env):
    result = await app_mod.im_providers(_user("usr_member", "member"))
    providers = {p["provider"]: p for p in result["providers"]}
    assert set(providers) == {"wechat", "wecom", "feishu", "dingtalk", "slack"}
    # 微信只能扫码，且明确标 BETA（协议非稳定契约）
    assert providers["wechat"]["modes"] == ["qr"]
    assert providers["wechat"]["beta"] is True
    # 表单字段由后端给出，前端不硬编码
    assert "app_id" in providers["feishu"]["fields"]


async def test_create_probes_and_marks_connected(im_env):
    _install_stack(im_env, providers={"feishu": FakeProvider(ok=True)})
    result = await app_mod.im_create(
        app_mod.ImConnectionBody(
            provider="feishu", mode="stream", app_id="cli_1", secret="s", label="主飞书"
        ),
        _user(),
    )
    conn = result["connection"]
    assert conn["status"] == "connected"
    assert conn["has_secret"] is True
    # 明文永远不出现在响应里
    assert "secret" not in conn
    assert "secret_cipher" not in conn


async def test_create_records_platform_error(im_env):
    _install_stack(
        im_env, providers={"feishu": FakeProvider(ok=False, error="code=10014 secret 错误")}
    )
    result = await app_mod.im_create(
        app_mod.ImConnectionBody(provider="feishu", mode="stream", app_id="cli_1", secret="bad"),
        _user(),
    )
    conn = result["connection"]
    assert conn["status"] == "error"
    # 平台原始错误要能让用户看到并自行修正
    assert "10014" in conn["last_error"]


async def test_duplicate_provider_is_rejected(im_env):
    _install_stack(im_env, providers={"feishu": FakeProvider()})
    body = app_mod.ImConnectionBody(provider="feishu", mode="stream", app_id="cli", secret="s")
    await app_mod.im_create(body, _user())
    with pytest.raises(HTTPException) as exc:
        await app_mod.im_create(body, _user())
    assert exc.value.status_code == 400
    assert "每渠道仅一条" in exc.value.detail


async def test_wechat_qr_skips_probe(im_env):
    """扫码渠道建记录时还没有 token，不该去探活（必然失败）。"""
    _install_stack(im_env)
    result = await app_mod.im_create(
        app_mod.ImConnectionBody(provider="wechat", mode="qr"), _user()
    )
    conn = result["connection"]
    assert conn["status"] == "unconfigured"
    assert conn["mode"] == "qr"
    assert conn["has_secret"] is False


async def test_patch_mode_resets_status(im_env):
    """切换「长连接 / 回调」必须先把状态打回未配置（平台侧互斥）。"""
    _install_stack(im_env, providers={"feishu": FakeProvider()})
    created = await app_mod.im_create(
        app_mod.ImConnectionBody(provider="feishu", mode="stream", app_id="cli", secret="s"),
        _user(),
    )
    assert created["connection"]["status"] == "connected"

    updated = await app_mod.im_update(
        created["connection"]["id"],
        app_mod.ImConnectionPatch(mode="webhook"),
        _user(),
    )
    assert updated["connection"]["mode"] == "webhook"
    assert updated["connection"]["status"] == "unconfigured"


async def test_delete_and_health(im_env):
    _install_stack(im_env, providers={"feishu": FakeProvider()})
    created = await app_mod.im_create(
        app_mod.ImConnectionBody(provider="feishu", mode="stream", app_id="cli", secret="s"),
        _user(),
    )
    conn_id = created["connection"]["id"]

    health = await app_mod.im_health(conn_id, _user())
    assert health["status"] == "connected"
    # 刚连上还没收到过事件：应提示订阅方式可能不匹配
    assert "订阅方式" in health["hint"]

    assert (await app_mod.im_delete(conn_id, _user()))["ok"] is True
    assert (await app_mod.im_list(_user()))["connections"] == []


async def test_webhook_acks_and_schedules(im_env):
    """回调必须立刻 ACK；事件处理交给后台（平台窗口 1.5–5 秒）。"""
    provider = FakeProvider()
    provider.parsed = InboundEvent(
        provider="feishu", channel_id="oc_1", text="/approve", raw_id="om_1"
    )
    _install_stack(im_env, providers={"feishu": provider})

    scheduled: list = []
    stack = app_mod._im()
    stack.router.schedule = lambda ev: scheduled.append(ev)  # type: ignore[method-assign]

    class FakeRequest:
        headers = {"content-type": "application/json"}

        async def body(self):
            return b'{"header":{"event_type":"im.message.receive_v1"}}'

    result = await app_mod.im_webhook("feishu", FakeRequest())
    assert result == {"ok": True}
    assert scheduled and scheduled[0].text == "/approve"


async def test_webhook_ignores_non_message_events(im_env):
    provider = FakeProvider()
    provider.parsed = None  # 成员变更 / 已处理过的握手
    _install_stack(im_env, providers={"feishu": provider})

    class FakeRequest:
        headers = {}

        async def body(self):
            return b"{}"

    result = await app_mod.im_webhook("feishu", FakeRequest())
    assert result == {"ok": True, "ignored": True}


async def test_webhook_unknown_provider(im_env):
    _install_stack(im_env, providers={})

    class FakeRequest:
        headers = {}

        async def body(self):
            return b"{}"

    with pytest.raises(HTTPException) as exc:
        await app_mod.im_webhook("telegram", FakeRequest())
    assert exc.value.status_code == 404


async def test_action_link_rejects_bad_signature(im_env):
    _install_stack(im_env)
    response = await app_mod.im_action(d="dc_1", a="approve", sig="forged")
    assert response.status_code == 401
    assert "链接无效" in response.body.decode()


async def test_action_link_rejects_expired_signature(im_env):
    """链接可被转发，必须限时（默认 24h）。"""
    _install_stack(im_env)
    old_ts = int(__import__("time").time()) - 90000
    sig = signature_for(settings.resolved_token_secret(), "dc_1", "approve", ts=old_ts)
    response = await app_mod.im_action(d="dc_1", a="approve", sig=sig)
    assert response.status_code == 401


async def test_action_link_executes_when_signature_valid(im_env, monkeypatch):
    _install_stack(im_env)
    resolved: dict = {}

    async def fake_resolve(decision_id, *, answer, by, channel="", is_admin=False, action=""):
        resolved.update(
            {
                "decision_id": decision_id,
                "answer": answer,
                "by": by,
                "channel": channel,
                "action": action,
            }
        )
        return SimpleNamespace(accepted=True, duplicate=False, reason="")

    monkeypatch.setattr(app_mod, "orch", lambda: SimpleNamespace(resolve_decision=fake_resolve))
    sig = signature_for(settings.resolved_token_secret(), "dc_1", "approve")
    response = await app_mod.im_action(d="dc_1", a="approve", sig=sig)
    assert response.status_code == 200
    assert resolved["decision_id"] == "dc_1"
    assert resolved["action"] == "approve"
    assert resolved["channel"] == "im:dingtalk"


async def test_action_link_reports_rejection(im_env, monkeypatch):
    """非绑定人点钉钉按钮：链接本身合法，但决策中心拒绝，要回 403 + 原因。"""
    _install_stack(im_env)

    async def fake_resolve(decision_id, *, answer, by, channel="", is_admin=False, action=""):
        return SimpleNamespace(accepted=False, duplicate=False, reason="不是该审批的绑定决策人")

    monkeypatch.setattr(app_mod, "orch", lambda: SimpleNamespace(resolve_decision=fake_resolve))
    sig = signature_for(settings.resolved_token_secret(), "dc_1", "approve")
    response = await app_mod.im_action(d="dc_1", a="approve", sig=sig)
    assert response.status_code == 403
    assert "绑定决策人" in response.body.decode()
