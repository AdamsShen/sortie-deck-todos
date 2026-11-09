"""平台适配器：入站解析、签名/解密、出站降级行为。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from types import SimpleNamespace

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from sortie_deck.im import feishu
from sortie_deck.im.base import ApprovalCard
from sortie_deck.im.cards import action_url, default_buttons
from sortie_deck.im.dingtalk import DingTalkProvider
from sortie_deck.im.dingtalk import parse_inbound as dingtalk_inbound
from sortie_deck.im.feishu import FeishuProvider
from sortie_deck.im.feishu import parse_inbound as feishu_inbound
from sortie_deck.im.wecom import WeComProvider
from sortie_deck.im.wecom import parse_inbound as wecom_inbound


def make_card(status: str = "pending") -> ApprovalCard:
    return ApprovalCard(
        title="【门禁】任务 A · eng_build",
        stage="eng_build",
        mission_id="ini_a",
        decision_id="dc_a",
        summary="确认放行",
        buttons=default_buttons() if status == "pending" else [],
        fallback_url="https://sortie.example.com/missions/ini_a",
        status=status,
    )


def conn_stub(**kwargs) -> SimpleNamespace:
    base = {"id": "imc_x", "provider": "feishu", "app_id": "cli_x", "mode": "stream"}
    base.update(kwargs)
    return SimpleNamespace(**base)


# --- 飞书 -------------------------------------------------------------------


def test_feishu_parse_text_message():
    payload = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "evt_1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_a", "user_id": "u_a"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "group",
                "content": json.dumps({"text": "/approve"}),
            },
        },
    }
    event = feishu_inbound(payload)
    assert event is not None
    assert event.provider == "feishu"
    assert event.channel_id == "oc_1"
    assert event.text == "/approve"
    assert event.sender_name == "u_a"
    assert event.raw_id == "om_1"


def test_feishu_parse_strips_mention_placeholder():
    """群聊 @机器人 会带 @_user_1，剥掉后才是能识别的命令。"""
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_b"}},
            "message": {
                "message_id": "om_2",
                "chat_id": "oc_1",
                "content": json.dumps({"text": "@_user_1 /reject 方案不对"}),
            },
        },
    }
    event = feishu_inbound(payload)
    assert event is not None
    assert event.text == "/reject 方案不对"


def test_feishu_parse_card_action():
    payload = {
        "header": {"event_type": "card.action.trigger", "event_id": "evt_card"},
        "event": {
            "operator": {"open_id": "ou_a", "user_id": "u_a"},
            "action": {"value": {"decision_id": "dc_a", "action": "approve"}},
            "context": {"open_chat_id": "oc_1", "open_message_id": "om_9"},
        },
    }
    event = feishu_inbound(payload)
    assert event is not None
    assert event.is_action
    assert event.action.decision_id == "dc_a"
    assert event.action.action == "approve"
    assert event.channel_id == "oc_1"


def test_feishu_decrypt_roundtrip():
    """webhook 模式配了 Encrypt Key 时，body 只有 {"encrypt": ...}。"""
    encrypt_key = "test-encrypt-key"
    original = {"schema": "2.0", "header": {"event_type": "im.message.receive_v1"}}
    plain = json.dumps(original).encode()
    pad = 16 - len(plain) % 16
    padded = plain + bytes([pad]) * pad
    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = os.urandom(16)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    cipher = encryptor.update(padded) + encryptor.finalize()
    encrypted = base64.b64encode(iv + cipher).decode()

    assert feishu.decrypt_payload(encrypt_key, encrypted) == original


def test_feishu_signature():
    key = "ek"
    ts, nonce, body = "1700000000", "n1", b'{"a":1}'
    sig = hashlib.sha256((ts + nonce + key).encode() + body).hexdigest()
    assert feishu.verify_signature(key, ts, nonce, body, sig)
    assert not feishu.verify_signature(key, ts, nonce, body, "bad")


async def test_feishu_probe_reports_missing_sdk(monkeypatch):
    """stream 模式缺 SDK 时要给出可操作提示，不能假装连上了。

    SDK 已装进环境，所以这里用 ``sys.modules`` 里的 None 模拟未安装
    （Python 对 ``import x`` 遇到 None 会抛 ImportError）。
    """
    import sys

    monkeypatch.setitem(sys.modules, "lark_oapi", None)
    provider = FeishuProvider(secret_resolver=lambda c: "s")
    ok, error = await provider.probe(conn_stub())
    assert ok is False
    assert "URL 回调" in error


# --- 钉钉 -------------------------------------------------------------------


def test_dingtalk_parse_text():
    payload = {
        "type": "CALLBACK",
        "headers": {"topic": "/v1.0/im/bot/messages/get"},
        "data": {
            "conversationId": "cid_1",
            "msgId": "m1",
            "senderNick": "张三",
            "senderStaffId": "u1",
            "text": {"content": "/approve"},
        },
    }
    event = dingtalk_inbound(payload)
    assert event is not None
    assert event.channel_id == "cid_1"
    assert event.text == "/approve"
    assert event.sender_name == "张三"
    assert event.raw_id == "m1"


def test_dingtalk_parse_card_callback_uses_out_track_id():
    """钉钉没有 button value 通道，decision_id 只能靠 outTrackId 带回来。"""
    payload = {
        "headers": {"topic": "/v1.0/card/instances/callback"},
        "data": {
            "outTrackId": "dc_a",
            "conversationId": "cid_1",
            "userId": "u1",
            "content": {"cardPrivateData": {"actionIds": ["approve"]}},
        },
    }
    event = dingtalk_inbound(payload)
    assert event is not None
    assert event.is_action
    assert event.action.decision_id == "dc_a"
    assert event.action.action == "approve"


async def test_dingtalk_card_falls_back_to_text_without_urls():
    """没有签名链接时不能发死按钮，必须降级为文本（可用 /approve）。"""
    provider = DingTalkProvider(secret_resolver=lambda c: "s")
    sent: list[tuple] = []

    async def fake_send_group(conn, chat_id, msg_key, msg_param):
        sent.append((msg_key, msg_param))
        return SimpleNamespace(ok=True)

    provider._send_group = fake_send_group  # type: ignore[method-assign]
    result = await provider.send_card(conn_stub(provider="dingtalk"), "cid_1", make_card())
    assert result.ok is True
    assert sent[0][0] == "sampleText"
    assert "/approve" in sent[0][1]


async def test_dingtalk_card_uses_sample_action_card_with_signed_urls():
    """注入签名链接生成器后走 sampleActionCard4（4 个按钮）。"""
    card = make_card()
    provider = DingTalkProvider(
        secret_resolver=lambda c: "s",
        action_url_builder=lambda c, b: action_url("https://s.example.com", "sec", c, b),
    )
    sent: list[tuple] = []

    async def fake_send_group(conn, chat_id, msg_key, msg_param):
        sent.append((msg_key, msg_param))
        return SimpleNamespace(ok=True)

    provider._send_group = fake_send_group  # type: ignore[method-assign]
    result = await provider.send_card(conn_stub(provider="dingtalk", mode="webhook"), "cid_1", card)
    assert result.ok is True
    msg_key, msg_param = sent[0]
    assert msg_key == "sampleActionCard4"
    params = json.loads(msg_param)
    assert params["buttonTitle1"] == "通过"
    assert "/api/im/action" in params["buttonUrl1"]
    assert "sig=" in params["buttonUrl1"]


# --- 企微 -------------------------------------------------------------------


def test_wecom_parse_text():
    payload = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r1"},
        "body": {
            "msgid": "m1",
            "chatid": "chat_1",
            "chattype": "group",
            "from": {"userid": "zhangsan"},
            "text": {"content": "/start"},
        },
    }
    event = wecom_inbound(payload)
    assert event is not None
    assert event.channel_id == "chat_1"
    assert event.chat_type == "group"
    assert event.text == "/start"
    assert event.sender_id == "zhangsan"


def test_wecom_parse_template_card_event():
    payload = {
        "cmd": "aibot_event_callback",
        "body": {
            "event": {
                "template_card_event": {
                    "event_id": "e1",
                    "chatid": "chat_1",
                    "chattype": "single",
                    "key": "reject",
                    "task_id": "dc_a",
                    "from": {"userid": "lisi"},
                }
            }
        },
    }
    event = wecom_inbound(payload)
    assert event is not None
    assert event.is_action
    assert event.action.action == "reject"
    assert event.action.decision_id == "dc_a"
    assert event.chat_type == "single"


async def test_wecom_send_without_socket_fails_loudly():
    """URL 回调模式下没有长连接，主动推送必须明确失败而不是静默丢消息。"""
    provider = WeComProvider(secret_resolver=lambda c: "s")
    result = await provider.send_text(conn_stub(provider="wecom"), "chat_1", "hi")
    assert result.ok is False
    assert "长连接未建立" in result.error


async def test_wecom_send_uses_attached_socket():
    provider = WeComProvider(secret_resolver=lambda c: "s")

    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

    socket = FakeSocket()
    provider.attach("imc_x", socket)
    result = await provider.send_card(conn_stub(provider="wecom"), "chat_1", make_card())
    assert result.ok is True
    frame = json.loads(socket.sent[0])
    assert frame["cmd"] == "aibot_send_msg"
    assert frame["body"]["msgtype"] == "template_card"
    assert frame["body"]["template_card"]["card_type"] == "button_interaction"


async def test_wecom_probe_requires_credentials():
    provider = WeComProvider(secret_resolver=lambda c: "")
    ok, error = await provider.probe(conn_stub(provider="wecom", app_id="", mode="stream"))
    assert ok is False
    assert "Bot ID" in error
