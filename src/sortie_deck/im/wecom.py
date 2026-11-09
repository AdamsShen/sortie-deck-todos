"""企业微信智能机器人适配器（WebSocket 长连接 + URL 回调）。

长连接协议要点（官方文档 path/101463）：

* 地址 ``wss://openws.work.weixin.qq.com``；
* 连上后必须发 ``aibot_subscribe``（``bot_id`` + ``secret``）做身份校验；
* 心跳建议 30 秒一次；
* **同一机器人同时只允许一条有效长连接**，新连接会踢掉旧连接 →
  多副本必须主备，不能多活；
* 长连接与「设置接收消息回调地址」互斥，切换会让另一种模式失效。

``websockets`` 已随 ``uvicorn[standard]`` 安装，因此不引入额外的企微 SDK，
直接实现 JSON 帧协议。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from sortie_deck.im.base import ApprovalCard, CardAction, InboundEvent, SendResult
from sortie_deck.im.cards import wecom_card

logger = logging.getLogger(__name__)

WS_URL = "wss://openws.work.weixin.qq.com"
PING_INTERVAL = 30.0
# 探活：连上后等多久没收到订阅结果就判定失败
PROBE_TIMEOUT = 8.0

CMD_SUBSCRIBE = "aibot_subscribe"
CMD_SEND = "aibot_send_msg"
CMD_UPDATE = "aibot_respond_update_msg"
CMD_EVENT_CALLBACK = "aibot_event_callback"


def _req_id(prefix: str = "req") -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{id(object()) % 10000}"


def parse_inbound(payload: dict[str, Any]) -> InboundEvent | None:
    """归一化企微长连接 / 回调帧。

    ``aibot_msg_callback`` 是用户消息；``aibot_event_callback`` 里可能是
    模板卡片按钮点击（``event.template_card_event``）。
    """
    cmd = str(payload.get("cmd") or "")
    body = payload.get("body") or payload

    if cmd == CMD_EVENT_CALLBACK:
        event = body.get("event") or {}
        card_event = event.get("template_card_event") or {}
        if not card_event:
            return None
        key = str(card_event.get("key") or "")
        task_id = str(card_event.get("task_id") or "")
        from_user = str((card_event.get("from") or {}).get("userid") or "")
        chat_id = str(card_event.get("chatid") or "")
        if not chat_id:
            return None
        return InboundEvent(
            provider="wecom",
            channel_id=chat_id,
            chat_type="single" if card_event.get("chattype") == "single" else "group",
            sender_id=from_user,
            sender_name=from_user or "wecom",
            raw_id=str(card_event.get("event_id") or card_event.get("msgid") or ""),
            action=CardAction(
                action=key,
                decision_id=task_id,
                operator_id=from_user,
                operator_name=from_user,
                raw=card_event,
            ),
        )

    text_block = body.get("text")
    if isinstance(text_block, dict):
        text = str(text_block.get("content") or "")
    else:
        text = str(body.get("content") or text_block or "")
    chat_id = str(body.get("chatid") or body.get("chat_id") or "")
    if not chat_id:
        return None
    sender = body.get("from") or {}
    sender_id = str((sender.get("userid") if isinstance(sender, dict) else "") or "")
    return InboundEvent(
        provider="wecom",
        channel_id=chat_id,
        chat_type="single" if str(body.get("chattype") or "") == "single" else "group",
        sender_id=sender_id,
        sender_name=sender_id or "wecom",
        text=text.strip(),
        raw_id=str(body.get("msgid") or body.get("event_id") or ""),
    )


class WeComProvider:
    """企微适配器。凭据取 ``conn.app_id``（= BotID）+ 解密后的 Secret。"""

    name = "wecom"

    def __init__(self, secret_resolver) -> None:
        self._secret = secret_resolver
        # conn_id → 已建立的长连接（由 runner 注册；URL 回调模式为空）
        self._sockets: dict[str, Any] = {}

    # --- 连接注册（runner 建连后调用）------------------------------------

    def attach(self, conn_id: str, socket: Any) -> None:
        self._sockets[conn_id] = socket

    def detach(self, conn_id: str) -> None:
        self._sockets.pop(conn_id, None)

    # --- 探活 -------------------------------------------------------------

    async def probe(self, conn) -> tuple[bool, str]:
        """企微没有 REST 探活接口，只能真的连一次 WS 并等订阅结果。

        URL 回调模式无法从服务端发起探活，只校验配置完整性并提示以实际
        回调为准。
        """
        secret = self._secret(conn)
        if not conn.app_id or not secret:
            return False, "缺少 Bot ID 或 Secret"
        if conn.mode == "webhook":
            return True, ""
        try:
            import websockets
        except ImportError:  # pragma: no cover - websockets 随 uvicorn[standard] 安装
            return False, "未安装 websockets，无法使用企微长连接"
        try:
            return await asyncio.wait_for(
                self._probe_stream(conn, secret, websockets), timeout=PROBE_TIMEOUT
            )
        except TimeoutError:
            return False, f"连接 {WS_URL} 超时（{PROBE_TIMEOUT:.0f}s）"

    async def _probe_stream(self, conn, secret: str, websockets_mod) -> tuple[bool, str]:
        try:
            async with websockets_mod.connect(WS_URL, open_timeout=5, close_timeout=2) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "cmd": CMD_SUBSCRIBE,
                            "headers": {"req_id": _req_id("probe")},
                            "body": {"bot_id": conn.app_id, "secret": secret},
                        }
                    )
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            body = data.get("body") or {}
            errcode = body.get("errcode", data.get("errcode", 0))
            if int(errcode or 0) != 0:
                return False, (
                    f"订阅被拒绝 errcode={errcode} "
                    f"errmsg={body.get('errmsg', data.get('errmsg', ''))}"
                )
            return True, ""
        except Exception as exc:  # noqa: BLE001 — 探活失败原因要原样展示
            return False, f"长连接失败: {exc}"

    # --- 出站 -------------------------------------------------------------

    async def send_text(self, conn, chat_id: str, text: str) -> SendResult:
        return await self._send(
            conn, {"chatid": chat_id, "msgtype": "text", "text": {"content": text}}
        )

    async def send_card(self, conn, chat_id: str, card: ApprovalCard) -> SendResult:
        return await self._send(
            conn,
            {
                "chatid": chat_id,
                "msgtype": "template_card",
                "template_card": wecom_card(card),
            },
        )

    async def _send(self, conn, body: dict[str, Any]) -> SendResult:
        socket = self._sockets.get(conn.id)
        if socket is None:
            # URL 回调模式没有长连接；主动推送需要 access_token，这里明确失败，
            # 避免"以为发了其实没发"。
            return SendResult(
                ok=False,
                provider="wecom",
                error="企微长连接未建立（URL 回调模式下请先建立长连接再主动推送）",
            )
        try:
            await socket.send(
                json.dumps(
                    {"cmd": CMD_SEND, "headers": {"req_id": _req_id("send")}, "body": body},
                    ensure_ascii=False,
                )
            )
            return SendResult(ok=True, provider="wecom")
        except Exception as exc:  # noqa: BLE001
            logger.warning("企微发送失败 conn=%s chat=%s: %s", conn.id, body.get("chatid"), exc)
            return SendResult(ok=False, provider="wecom", error=str(exc))

    async def update_card(self, conn, chat_id: str, message_id: str, card: ApprovalCard) -> bool:
        """企微要求 5 秒内用回调帧的 req_id 更新卡片；没有帧上下文时只能补发。"""
        socket = self._sockets.get(conn.id)
        if socket is None or not message_id:
            return False
        try:
            await socket.send(
                json.dumps(
                    {
                        "cmd": CMD_UPDATE,
                        "headers": {"req_id": message_id},
                        "body": {"chatid": chat_id, "template_card": wecom_card(card)},
                    },
                    ensure_ascii=False,
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("企微更新卡片失败 conn=%s: %s", conn.id, exc)
            return False

    def parse_inbound(self, payload: dict[str, Any]) -> InboundEvent | None:
        return parse_inbound(payload)
