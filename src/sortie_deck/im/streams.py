"""飞书 / 钉钉长连接桥。

两个 SDK 都是**同步阻塞 + 自带线程/循环**，和我们的 asyncio 服务不在一个世界，
桥接逻辑集中在这里，:mod:`sortie_deck.im.runner` 只负责生命周期与退避。

三条硬约束：

* **阻塞**：``lark.ws.Client.start()`` 与 ``DingTalkStreamClient.start_forever()``
  都不返回，直接跑在事件循环里会卡死整个 API，必须放进 daemon 线程；
* **跨线程**：事件回调发生在 SDK 自己的线程，只能 ``run_coroutine_threadsafe``
  投回主循环，不能在那里直接碰 store / router；
* **没有 shutdown API**：停止只能置标志位。SDK 自带 ``auto_reconnect``，
  断线重连由它负责，我们不重复建连。

**"已连接"这件事 SDK 不给证据。** 握手成功没有回调，失败也只在内部重试并打日志。
因此连接状态不能靠"线程起来了"来判定，唯一可信的信号是**收到第一条事件**
（``store.events_seen_at``）——这也正是 ``health`` 里 ``stale`` 的判断依据。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# 与 dingtalk_stream.ChatbotMessage.TOPIC 一致。这里写字面量是为了让本模块在
# 未安装 SDK 时也能 import（SDK 是可选依赖），测试里会断言两者相等。
DINGTALK_CHATBOT_TOPIC = "/v1.0/im/bot/messages/get"


class StreamHandle:
    """一条长连接的句柄：线程 + 停止标志 + 最后一个错误。"""

    def __init__(self, *, name: str, target: Callable[[], None]) -> None:
        self.name = name
        self.error = ""
        self.stopped = threading.Event()
        self._target = target
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"im-{name}")

    def _run(self) -> None:
        try:
            self._target()
        except Exception as exc:  # noqa: BLE001 — 线程内的异常必须落进状态里
            self.error = f"{type(exc).__name__}: {exc}"
            logger.warning("%s 长连接线程退出: %s", self.name, self.error)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """协作式停止：只置标志。SDK 没有 shutdown，线程随进程退出。"""
        self.stopped.set()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()


def wrap_dingtalk_payload(data: dict[str, Any]) -> dict[str, Any]:
    """把钉钉的 ``ChatbotMessage.to_dict()`` 包成 Stream 帧形状。

    这样 :func:`sortie_deck.im.dingtalk.parse_inbound` 一份解析逻辑同时服务
    HTTP 回调与长连接，不必为接入方式分叉。
    """
    return {"headers": {"topic": DINGTALK_CHATBOT_TOPIC}, "data": data}


class FeishuStream(StreamHandle):
    """飞书 WebSocket 长连接。需要 ``lark-oapi``。"""

    def __init__(self, *, app_id: str, app_secret: str, emit: Callable[[dict], None]) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._emit = emit
        super().__init__(name="feishu", target=self._run_client)

    def _run_client(self) -> None:
        import lark_oapi as lark

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_card_action_trigger(self._on_card)
            .build()
        )
        client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        # 阻塞；内部 auto_reconnect 保持连接
        client.start()

    def _on_message(self, data: Any) -> None:
        self._publish(data)
        return None

    def _on_card(self, data: Any) -> Any:
        self._publish(data)
        # 飞书要求卡片回调返回一个响应体，否则客户端会提示交互失败。
        # 真正的业务处理在后台，这里只回一个空响应让客户端立刻恢复。
        try:
            from lark_oapi.api.card.v1 import P2CardActionTriggerResponse

            return P2CardActionTriggerResponse({})
        except Exception:  # noqa: BLE001 — 响应体构造失败不应影响事件处理
            return None

    def _publish(self, data: Any) -> None:
        if self.stopped.is_set():
            return
        try:
            import lark_oapi as lark

            # marshal 出来的结构是 {"header": {...}, "event": {...}}，
            # 与 HTTP 回调完全一致，所以 parse_inbound 可以直接复用。
            payload = json.loads(lark.JSON.marshal(data))
        except Exception as exc:  # noqa: BLE001
            logger.warning("飞书长连接事件序列化失败: %s", exc)
            return
        self._emit(payload)


class DingTalkStream(StreamHandle):
    """钉钉 Stream 长连接。需要 ``dingtalk-stream``。"""

    def __init__(self, *, app_key: str, app_secret: str, emit: Callable[[dict], None]) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._emit = emit
        super().__init__(name="dingtalk", target=self._run_client)

    def _run_client(self) -> None:
        import dingtalk_stream
        from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage

        stream = self

        class Handler(ChatbotHandler):
            async def process(self, callback):  # noqa: ANN001, ANN201 — SDK 签名固定
                if not stream.stopped.is_set():
                    try:
                        data = callback.to_dict()
                    except Exception:  # noqa: BLE001
                        data = {}
                    stream._emit(wrap_dingtalk_payload(data))
                # 必须立刻 ACK：钉钉按超时重推，处理放后台
                return AckMessage.STATUS_OK, "OK"

        client = dingtalk_stream.DingTalkStreamClient(
            dingtalk_stream.Credential(self._app_key, self._app_secret)
        )
        client.register_callback_handler(ChatbotMessage.TOPIC, Handler())
        # 阻塞；内部自动重连
        client.start_forever()


def build_stream(
    provider: str,
    *,
    app_id: str,
    app_secret: str,
    emit: Callable[[dict], None],
) -> StreamHandle:
    """按渠道建句柄。未安装对应 SDK 时抛 ImportError，由调用方转成可读错误。"""
    if provider == "feishu":
        return FeishuStream(app_id=app_id, app_secret=app_secret, emit=emit)
    if provider == "dingtalk":
        return DingTalkStream(app_key=app_id, app_secret=app_secret, emit=emit)
    raise ValueError(f"不支持长连接的渠道: {provider}")


async def emit_into(
    loop: asyncio.AbstractEventLoop, handler: Callable[[dict], Any], payload: dict
) -> None:
    """线程安全的事件投递（SDK 线程 → 主循环）。"""
    asyncio.run_coroutine_threadsafe(handler(payload), loop)
