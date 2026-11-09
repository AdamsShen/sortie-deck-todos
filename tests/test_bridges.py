from __future__ import annotations

import hashlib
import hmac
import time

from sortie_deck.bridges import (
    PROVIDERS,
    BridgeMapping,
    BridgeStore,
    feishu_text_from_payload,
    slack_text_from_payload,
    verify_slack_signature,
    wecom_chat_type,
    wecom_text_from_payload,
)


def test_bridge_store_roundtrip(tmp_path):
    store = BridgeStore(tmp_path / "bridges.json")
    m = store.upsert(
        BridgeMapping(provider="slack", channel_id="C1", initiative_id="ini_1", label="main")
    )
    assert store.resolve("slack", "C1").initiative_id == "ini_1"
    assert m.label == "main"


def test_slack_signature_and_payload():
    secret = "sigsec"
    ts = str(int(time.time()))
    body = b"command=%2Fapprove&text=&channel_id=C9&user_name=alice"
    base = f"v0:{ts}:".encode() + body
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    assert verify_slack_signature(signing_secret=secret, timestamp=ts, body=body, signature=sig)
    channel, author, text = slack_text_from_payload(
        {"command": "/approve", "text": "", "channel_id": "C9", "user_name": "alice"}
    )
    assert channel == "C9"
    assert author == "alice"
    assert text.startswith("/approve")


def test_feishu_payload():
    payload = {
        "event": {
            "message": {
                "chat_id": "oc_1",
                "content": '{"text":"/start"}',
            },
            "sender": {"sender_id": {"open_id": "ou_x"}},
        }
    }
    channel, author, text = feishu_text_from_payload(payload)
    assert channel == "oc_1"
    assert author == "ou_x"
    assert text == "/start"


def test_providers_cover_all_platforms():
    """映射白名单必须覆盖已实现入站的每个平台。

    历史 bug：/api/bridges/map 只放行 slack/feishu，dingtalk 的入站消息
    永远 resolve 不到映射，表现为"配了但机器人不回"。
    """
    assert {"slack", "feishu", "dingtalk", "wecom"} == set(PROVIDERS)


def test_wecom_long_connection_frame():
    """企微智能机器人长连接帧：body 内是真正的消息体。"""
    payload = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r1"},
        "body": {
            "msgid": "m1",
            "chatid": "chat_group_1",
            "chattype": "group",
            "from": {"userid": "zhangsan"},
            "text": {"content": "  /approve  "},
        },
    }
    channel, author, text = wecom_text_from_payload(payload)
    assert channel == "chat_group_1"
    assert author == "zhangsan"
    assert text == "/approve"
    assert wecom_chat_type(payload) == "group"


def test_wecom_flat_body_and_single_chat():
    """URL 回调解密后拿到的是扁平 body，且要能区分单聊。"""
    payload = {
        "chatid": "single_1",
        "chattype": "single",
        "from": {"userid": "lisi"},
        "text": {"content": "/start"},
    }
    channel, author, text = wecom_text_from_payload(payload)
    assert channel == "single_1"
    assert author == "lisi"
    assert text == "/start"
    assert wecom_chat_type(payload) == "single"


def test_bridge_mapping_backward_compatible(tmp_path):
    """旧映射 json 没有 chat_type/created_by，反序列化必须仍成立。"""
    path = tmp_path / "bridges.json"
    path.write_text(
        '[{"provider":"feishu","channel_id":"oc_old","initiative_id":"ini_old"}]',
        encoding="utf-8",
    )
    mapping = BridgeStore(path).resolve("feishu", "oc_old")
    assert mapping is not None
    assert mapping.chat_type == "group"
    assert mapping.created_by == ""
