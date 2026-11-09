"""Slack 适配器：两种入站形态、机器人消息过滤、凭据折叠与验签。"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace

from sortie_deck.im.slack import (
    SlackProvider,
    bot_token_of,
    fold_credentials,
    parse_inbound,
    signing_secret_of,
    split_credentials,
    verify_slack_signature,
)

SECRET = "s3cr3t-signing"


def sign(secret: str, ts: str, body: bytes) -> str:
    base = f"v0:{ts}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def conn() -> SimpleNamespace:
    return SimpleNamespace(id="imc_slack", app_id="", mode="webhook")


# --- 验签 ---------------------------------------------------------------


def test_signature_ok():
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    assert verify_slack_signature(
        signing_secret=SECRET, timestamp=ts, body=body, signature=sign(SECRET, ts, body)
    )


def test_signature_rejects_tampered_body():
    """签名覆盖原始 body：改一个字节就必须失败。"""
    ts = str(int(time.time()))
    sig = sign(SECRET, ts, b'{"a":1}')
    assert not verify_slack_signature(
        signing_secret=SECRET, timestamp=ts, body=b'{"a":2}', signature=sig
    )


def test_signature_rejects_replay():
    """5 分钟外的时间戳一律拒绝，防止旧请求被重放。"""
    old = str(int(time.time()) - 3600)
    body = b"{}"
    assert not verify_slack_signature(
        signing_secret=SECRET, timestamp=old, body=body, signature=sign(SECRET, old, body)
    )


def test_signature_requires_secret():
    ts = str(int(time.time()))
    assert not verify_slack_signature(
        signing_secret="", timestamp=ts, body=b"{}", signature="v0=x"
    )


# --- 凭据 ---------------------------------------------------------------


def test_fold_keeps_bot_token_out_of_app_id():
    """Bot Token 本身就是凭据，不能明文落在可回显的 app_id 列。"""
    app_id, secret = fold_credentials("xoxb-123", SECRET)
    assert app_id == ""
    assert bot_token_of(secret) == "xoxb-123"
    assert signing_secret_of(secret) == SECRET
    assert "xoxb-123" not in app_id


def test_fold_requires_both_parts():
    """缺一半就原样返回，由 store 的必填校验报错。"""
    assert fold_credentials("xoxb-1", "") == ("xoxb-1", "")


def test_legacy_plain_secret_still_parses():
    """早期部署可能直接存了一个 token。"""
    assert split_credentials("xoxb-legacy") == ("xoxb-legacy", "")
    assert bot_token_of("xoxb-legacy") == "xoxb-legacy"
    assert signing_secret_of("xoxb-legacy") == ""


def test_secret_json_roundtrip():
    _, secret = fold_credentials("xoxb-9", SECRET)
    assert json.loads(secret) == {"bot_token": "xoxb-9", "signing_secret": SECRET}


# --- 入站解析 -----------------------------------------------------------


def test_message_event():
    event = parse_inbound(
        {
            "type": "event_callback",
            "event_id": "Ev123",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": "/list",
                "ts": "1700000000.0001",
                "channel_type": "channel",
            },
        }
    )
    assert event is not None
    assert event.provider == "slack"
    assert event.channel_id == "C1"
    assert event.text == "/list"
    assert event.raw_id == "Ev123"
    assert event.chat_type == "group"


def test_bot_message_is_dropped():
    """不过滤就会自己触发自己，形成无限循环。"""
    assert (
        parse_inbound(
            {
                "type": "event_callback",
                "event": {
                    "type": "message",
                    "bot_id": "B1",
                    "channel": "C1",
                    "text": "/list",
                },
            }
        )
        is None
    )
    assert (
        parse_inbound(
            {
                "type": "event_callback",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C1",
                    "text": "/list",
                },
            }
        )
        is None
    )


def test_app_mention_strips_placeholder():
    event = parse_inbound(
        {
            "type": "event_callback",
            "event_id": "Ev2",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": "<@U999> 目前有什么任务",
            },
        }
    )
    assert event is not None
    assert event.text == "目前有什么任务"


def test_dm_is_single_chat():
    event = parse_inbound(
        {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "D1",
                "user": "U1",
                "text": "hi",
                "channel_type": "im",
            },
        }
    )
    assert event is not None
    assert event.chat_type == "single"


def test_block_actions():
    event = parse_inbound(
        {
            "type": "block_actions",
            "trigger_id": "tr1",
            "user": {"id": "U1", "username": "alice"},
            "channel": {"id": "C1"},
            "actions": [{"action_id": "approve", "value": "dec_abc:approve"}],
        }
    )
    assert event is not None
    assert event.is_action
    assert event.action.decision_id == "dec_abc"
    assert event.action.action == "approve"
    assert event.action.operator_name == "alice"


def test_block_actions_falls_back_to_action_id():
    """按钮只带 action_id 时也要能解析出动作。"""
    event = parse_inbound(
        {
            "type": "block_actions",
            "trigger_id": "tr2",
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
            "actions": [{"action_id": "reject", "value": ""}],
        }
    )
    assert event is not None
    assert event.action.action == "reject"
    assert event.action.decision_id == ""


def test_url_verification_is_ignored():
    """challenge 由 API 层直接回，不走业务路由。"""
    assert parse_inbound({"type": "url_verification", "challenge": "abc"}) is None


def test_slash_command():
    event = parse_inbound(
        {"command": "/sortie", "text": "list", "channel_id": "C1", "user_id": "U1"}
    )
    assert event is not None
    assert event.text == "list"


def test_slash_command_without_wrapper():
    event = parse_inbound(
        {"command": "/approve", "text": "ini_x", "channel_id": "C1", "user_id": "U1"}
    )
    assert event is not None
    assert event.text == "approve ini_x"


def test_unknown_payload():
    assert parse_inbound({"type": "reaction_added"}) is None
    assert parse_inbound({}) is None


# --- 探活 ---------------------------------------------------------------


async def test_probe_requires_bot_token():
    provider = SlackProvider(lambda c: "")
    ok, err = await provider.probe(conn())
    assert ok is False
    assert "Bot Token" in err


async def test_probe_requires_signing_secret():
    """没有 Signing Secret 就无法验签，URL 回调等于对任何人开放。"""
    provider = SlackProvider(
        lambda c: json.dumps({"bot_token": "xoxb-1", "signing_secret": ""})
    )
    ok, err = await provider.probe(conn())
    assert ok is False
    assert "Signing Secret" in err
