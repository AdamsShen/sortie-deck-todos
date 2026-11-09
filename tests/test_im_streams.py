"""长连接桥：SDK 线程 → 主循环的投递、payload 形状、以及"收到事件才 connected"。"""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from sortie_deck.im import streams
from sortie_deck.im.dingtalk import DingTalkProvider
from sortie_deck.im.runner import ImRunner

# --- payload 形状 --------------------------------------------------------


def test_wrap_dingtalk_payload_shape():
    """包成 Stream 帧的形状，才能复用 HTTP 回调那份解析器。"""
    wrapped = streams.wrap_dingtalk_payload({"conversationId": "cid", "msgId": "m1"})
    assert wrapped["headers"]["topic"] == streams.DINGTALK_CHATBOT_TOPIC
    assert wrapped["data"]["conversationId"] == "cid"


def test_dingtalk_topic_matches_sdk():
    """字面量必须和 SDK 常量一致，否则注册的 handler 永远收不到消息。"""
    dingtalk_stream = pytest.importorskip("dingtalk_stream")
    assert streams.DINGTALK_CHATBOT_TOPIC == dingtalk_stream.ChatbotMessage.TOPIC


def test_wrapped_payload_parses_through_existing_parser():
    """端到端对齐：包出来的帧喂给 parse_inbound 能拿到正确的命令。"""
    provider = DingTalkProvider(secret_resolver=lambda c: "s")
    event = provider.parse_inbound(
        streams.wrap_dingtalk_payload(
            {
                "conversationId": "cid_1",
                "msgId": "m1",
                "senderStaffId": "u1",
                "senderNick": "张三",
                "text": {"content": "/list"},
            }
        )
    )
    assert event is not None
    assert event.channel_id == "cid_1"
    assert event.text == "/list"
    assert event.sender_name == "张三"


# --- 线程句柄 -----------------------------------------------------------


def test_handle_records_thread_error():
    """线程里的异常不能静默，要落进 error 供重连日志使用。"""

    def boom() -> None:
        raise RuntimeError("握手失败")

    handle = streams.StreamHandle(name="t", target=boom)
    handle.start()
    handle._thread.join(timeout=2)
    assert handle.alive is False
    assert "握手失败" in handle.error


def test_handle_dies_after_normal_return():
    handle = streams.StreamHandle(name="t2", target=lambda: None)
    handle.start()
    handle._thread.join(timeout=2)
    assert handle.alive is False
    assert handle.error == ""


def test_stop_is_cooperative():
    """SDK 没有 shutdown：stop 只置标志，线程自己退出。"""
    release = threading.Event()
    handle = streams.StreamHandle(name="t3", target=lambda: release.wait(1))
    handle.start()
    handle.stop()
    assert handle.stopped.is_set()
    release.set()
    handle._thread.join(timeout=2)


# --- 飞书 marshal -------------------------------------------------------


def test_feishu_publish_marshals_to_callback_shape():
    """飞书 SDK 对象 marshal 后必须与 HTTP 回调的 payload 同构。"""
    lark = pytest.importorskip("lark_oapi")
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    got: list[dict] = []
    stream = streams.FeishuStream(app_id="a", app_secret="s", emit=got.append)
    data = P2ImMessageReceiveV1(
        d={
            "header": {"event_id": "e1", "event_type": "im.message.receive_v1"},
            "event": {
                "message": {
                    "chat_id": "oc_1",
                    "message_id": "om_1",
                    "chat_type": "group",
                    "content": '{"text":"/list"}',
                },
                "sender": {"sender_id": {"open_id": "ou_1"}},
            },
        }
    )
    stream._publish(data)

    assert len(got) == 1
    assert got[0]["header"]["event_type"] == "im.message.receive_v1"
    # 直接喂给已有的解析器，不需要为长连接另写一份
    from sortie_deck.im.feishu import FeishuProvider

    event = FeishuProvider(secret_resolver=lambda c: "s").parse_inbound(got[0])
    assert event is not None
    assert event.channel_id == "oc_1"
    assert event.text == "/list"
    assert lark is not None


def test_feishu_publish_skipped_after_stop():
    """停止后不再往主循环投递，避免重连期间重复投递。"""
    got: list[dict] = []
    stream = streams.FeishuStream(app_id="a", app_secret="s", emit=got.append)
    stream.stop()
    stream._publish({"header": {}, "event": {}})
    assert got == []


def test_feishu_publish_survives_bad_data():
    """序列化失败只记日志，不能让 SDK 线程崩掉。"""
    got: list[dict] = []
    stream = streams.FeishuStream(app_id="a", app_secret="s", emit=got.append)
    stream._publish(object())
    assert got == []


# --- 工厂 ---------------------------------------------------------------


def test_build_stream_rejects_unknown_provider():
    with pytest.raises(ValueError):
        streams.build_stream("wechat", app_id="a", app_secret="s", emit=lambda p: None)


def test_build_stream_returns_correct_handle():
    assert isinstance(
        streams.build_stream("feishu", app_id="a", app_secret="s", emit=lambda p: None),
        streams.FeishuStream,
    )
    assert isinstance(
        streams.build_stream("dingtalk", app_id="a", app_secret="s", emit=lambda p: None),
        streams.DingTalkStream,
    )


# --- runner 的事件处理 ---------------------------------------------------


class FakeStore:
    def __init__(self, status: str = "connecting") -> None:
        self.status = status
        self.touched: list[str] = []
        self.statuses: list[str] = []

    def get(self, conn_id: str):
        return SimpleNamespace(id=conn_id, status=self.status, enabled=True)

    def touch_event(self, conn_id: str, at=None) -> None:
        self.touched.append(conn_id)

    def set_status(self, conn_id: str, status: str, **kw) -> None:
        self.statuses.append(status)
        self.status = status


class FakeRouter:
    def __init__(self) -> None:
        self.events: list = []

    def schedule(self, event) -> None:
        self.events.append(event)


async def test_handle_stream_event_marks_connected_on_first_event():
    """「线程活着」不算连上；收到第一条事件才是证据。"""
    store = FakeStore(status="connecting")
    router = FakeRouter()
    runner = ImRunner(
        store=store,
        router=router,
        providers={"dingtalk": DingTalkProvider(secret_resolver=lambda c: "s")},
    )
    await runner._handle_stream_event(
        "imc_1",
        "dingtalk",
        streams.wrap_dingtalk_payload(
            {"conversationId": "cid", "msgId": "m1", "text": {"content": "/list"}}
        ),
    )
    assert store.touched == ["imc_1"]
    assert store.statuses == ["connected"]
    assert len(router.events) == 1
    assert router.events[0].text == "/list"


async def test_handle_stream_event_skips_status_write_when_already_connected():
    """稳态下每条消息都写库没必要。"""
    store = FakeStore(status="connected")
    runner = ImRunner(
        store=store,
        router=FakeRouter(),
        providers={"dingtalk": DingTalkProvider(secret_resolver=lambda c: "s")},
    )
    await runner._handle_stream_event(
        "imc_1",
        "dingtalk",
        streams.wrap_dingtalk_payload(
            {"conversationId": "cid", "msgId": "m2", "text": {"content": "/list"}}
        ),
    )
    assert store.statuses == []


async def test_handle_stream_event_ignores_unparsable():
    store = FakeStore()
    router = FakeRouter()
    runner = ImRunner(
        store=store,
        router=router,
        providers={"dingtalk": DingTalkProvider(secret_resolver=lambda c: "s")},
    )
    await runner._handle_stream_event("imc_1", "dingtalk", {"unexpected": True})
    assert router.events == []
    assert store.touched == []


def test_runner_spawn_rejects_unknown_provider():
    """早先 else 分支会把未知渠道当企微去连 WS，必须显式拒绝。"""
    runner = ImRunner(store=FakeStore(), router=FakeRouter(), providers={})
    runner.spawn("imc_x", "telegram")
    assert runner._tasks == {}

    assert sys is not None
