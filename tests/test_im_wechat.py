"""微信 iLink Bot：消息解析、context_token、扫码登录落库、无卡片降级。"""

from __future__ import annotations

import pytest

from sortie_deck.git_cred import new_secret_key
from sortie_deck.im import ImConnectionStore, wechat
from sortie_deck.im.base import ApprovalCard
from sortie_deck.im.cards import default_buttons
from sortie_deck.im.qr_login import QrLoginService


@pytest.fixture()
def store(tmp_path) -> ImConnectionStore:
    return ImConnectionStore(tmp_path / "im.sqlite", secret_key=new_secret_key())


@pytest.fixture(autouse=True)
def _clear_context_tokens():
    wechat._CONTEXT_TOKENS.clear()
    yield
    wechat._CONTEXT_TOKENS.clear()


def msg(text: str = "/approve", message_type: int = 1, **kwargs) -> dict:
    base = {
        "message_id": 1001,
        "from_user_id": "wxid_alice",
        "to_user_id": "bot",
        "message_type": message_type,
        "context_token": "ctx_1",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    base.update(kwargs)
    return base


def test_parse_user_text_message():
    event = wechat.parse_inbound(msg("/approve"))
    assert event is not None
    assert event.provider == "wechat"
    assert event.channel_id == "wxid_alice"
    assert event.chat_type == "single"  # 微信只有单聊
    assert event.text == "/approve"
    assert event.raw_id == "1001"


def test_parse_skips_bot_own_messages():
    """机器人自己发的消息（message_type=2）不能回流，否则会自己回自己。"""
    assert wechat.parse_inbound(msg("我是机器人", message_type=2)) is None


def test_parse_stores_context_token_for_reply():
    wechat.parse_inbound(msg(context_token="ctx_abc"))
    # 出站回复必须回传上下文令牌，否则微信拒绝发送
    assert wechat._CONTEXT_TOKENS["wxid_alice"] == "ctx_abc"


def test_parse_ignores_non_text_items():
    payload = msg()
    payload["item_list"] = [{"type": 2, "image_item": {"url": "x"}}]
    event = wechat.parse_inbound(payload)
    assert event is not None
    assert event.text == ""


class FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


class FakeClient:
    """替身 httpx.AsyncClient：记录请求并返回预置响应。"""

    captured: list[dict] = []
    response: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        FakeClient.captured.append({"method": "POST", "url": url, **kwargs})
        return FakeResponse(FakeClient.response)

    async def get(self, url, **kwargs):
        FakeClient.captured.append({"method": "GET", "url": url, **kwargs})
        return FakeResponse(FakeClient.response)


@pytest.fixture()
def fake_http(monkeypatch):
    FakeClient.captured = []
    FakeClient.response = {}
    monkeypatch.setattr(wechat.httpx, "AsyncClient", FakeClient)
    return FakeClient


async def test_send_without_context_token_fails_loudly(store, fake_http):
    """微信不允许冷启动私聊：没有 context_token 时必须明确失败。"""
    conn = store.create(provider="wechat", mode="qr")
    store.set_secret(conn.id, "tok")
    provider = wechat.WeChatProvider(lambda c: "tok")
    result = await provider.send_text(conn, "wxid_alice", "hi")
    assert result.ok is False
    assert "context_token" in result.error


async def test_send_text_posts_to_sendmessage(store, fake_http):
    conn = store.create(provider="wechat", mode="qr")
    store.set_secret(conn.id, "tok")
    wechat._CONTEXT_TOKENS["wxid_alice"] = "ctx_1"
    fake_http.response = {"ret": 0}

    provider = wechat.WeChatProvider(lambda c: "tok")
    result = await provider.send_text(conn, "wxid_alice", "任务已完成")
    assert result.ok is True
    call = fake_http.captured[0]
    assert call["url"].endswith("/ilink/bot/sendmessage")
    body = call["json"]
    assert body["msg"]["to_user_id"] == "wxid_alice"
    assert body["msg"]["context_token"] == "ctx_1"
    assert body["msg"]["item_list"][0]["text_item"]["text"] == "任务已完成"
    # 鉴权头必须带 token
    assert call["headers"]["Authorization"] == "Bearer tok"


async def test_send_card_degrades_to_text_with_slash_commands(store, fake_http):
    """微信没有卡片：审批按钮必须降级成可复制的斜杠命令。"""
    conn = store.create(provider="wechat", mode="qr")
    store.set_secret(conn.id, "tok")
    wechat._CONTEXT_TOKENS["wxid_alice"] = "ctx_1"
    fake_http.response = {"ret": 0}

    card = ApprovalCard(
        title="【门禁】任务 A",
        stage="eng_build",
        mission_id="ini_a",
        decision_id="dc_a",
        summary="确认放行",
        buttons=default_buttons(),
        fallback_url="https://s.example.com/missions/ini_a",
    )
    provider = wechat.WeChatProvider(lambda c: "tok")
    result = await provider.send_card(conn, "wxid_alice", card)
    assert result.ok is True
    text = fake_http.captured[0]["json"]["msg"]["item_list"][0]["text_item"]["text"]
    assert "/approve" in text
    assert "https://s.example.com/missions/ini_a" in text


async def test_probe_without_token_reports_reason(store):
    conn = store.create(provider="wechat", mode="qr")
    provider = wechat.WeChatProvider(lambda c: "")
    ok, error = await provider.probe(conn)
    assert ok is False
    assert "扫码" in error


# --- 扫码登录 ---------------------------------------------------------------


async def test_qr_start_and_confirm(store, monkeypatch):
    conn = store.create(provider="wechat", mode="qr")

    async def fake_qr(**kwargs):
        return {"qrcode": "qr_token_1", "qrcode_img_content": "https://qr.example/1"}

    async def fake_status(qrcode, **kwargs):
        assert qrcode == "qr_token_1"
        return {
            "status": "confirmed",
            "bot_token": "bot_tok_1",
            "ilink_bot_id": "ilink_bot_9",
            "ilink_user_id": "wxid_alice",
        }

    monkeypatch.setattr(wechat, "get_bot_qrcode", fake_qr)
    monkeypatch.setattr(wechat, "get_qrcode_status", fake_status)

    service = QrLoginService(store=store)
    session = await service.start(conn.id)
    assert session.status == "pending"
    assert session.qr_content == "https://qr.example/1"
    assert session.to_public()["expires_in"] > 0

    polled = await service.poll(session.ticket)
    assert polled.status == "confirmed"

    updated = store.get(conn.id)
    assert updated.status == "connected"
    assert updated.app_id == "ilink_bot_9"
    assert updated.has_secret is True
    assert store.decrypt_secret(conn.id) == "bot_tok_1"
    # 二维码过期时间清掉，卡片不再显示倒计时
    assert updated.qr_expires_at is None


async def test_qr_wait_then_scaned_then_confirmed(store, monkeypatch):
    conn = store.create(provider="wechat", mode="qr")
    states = iter(["wait", "scaned", "confirmed"])

    async def fake_qr(**kwargs):
        return {"qrcode": "q1", "qrcode_img_content": "x"}

    async def fake_status(qrcode, **kwargs):
        status = next(states)
        if status == "confirmed":
            return {"status": status, "bot_token": "t", "ilink_bot_id": "b"}
        return {"status": status}

    monkeypatch.setattr(wechat, "get_bot_qrcode", fake_qr)
    monkeypatch.setattr(wechat, "get_qrcode_status", fake_status)

    service = QrLoginService(store=store)
    session = await service.start(conn.id)
    assert (await service.poll(session.ticket)).status == "pending"
    assert (await service.poll(session.ticket)).status == "scaned"
    assert (await service.poll(session.ticket)).status == "confirmed"


async def test_qr_expires_locally(store, monkeypatch):
    conn = store.create(provider="wechat", mode="qr")

    async def fake_qr(**kwargs):
        return {"qrcode": "q1", "qrcode_img_content": "x"}

    monkeypatch.setattr(wechat, "get_bot_qrcode", fake_qr)
    service = QrLoginService(store=store, ttl=-1)  # 立即过期
    session = await service.start(conn.id)
    assert (await service.poll(session.ticket)).status == "expired"


async def test_qr_unknown_ticket_raises(store):
    service = QrLoginService(store=store)
    with pytest.raises(Exception) as exc:
        await service.poll("qrt_missing")
    assert "扫码会话" in str(exc.value)


async def test_qr_rejects_non_wechat_provider(store):
    conn = store.create(provider="feishu", mode="stream", app_id="cli", secret="s")
    service = QrLoginService(store=store)
    with pytest.raises(Exception) as exc:
        await service.start(conn.id)
    assert "微信" in str(exc.value)


async def test_qr_confirm_requires_bot_token(store, monkeypatch):
    """确认了但没给 token：不能假装成功，否则会写入空凭据。"""
    conn = store.create(provider="wechat", mode="qr")

    async def fake_qr(**kwargs):
        return {"qrcode": "q1", "qrcode_img_content": "x"}

    async def fake_status(qrcode, **kwargs):
        return {"status": "confirmed"}

    monkeypatch.setattr(wechat, "get_bot_qrcode", fake_qr)
    monkeypatch.setattr(wechat, "get_qrcode_status", fake_status)

    service = QrLoginService(store=store)
    session = await service.start(conn.id)
    polled = await service.poll(session.ticket)
    assert polled.status == "error"
    assert "bot_token" in polled.error
    assert store.get(conn.id).has_secret is False
