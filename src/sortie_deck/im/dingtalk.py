"""钉钉适配器。

接入方式：

* **Stream 模式（推荐）** —— 需要 ``dingtalk-stream``（``pip install 'sortie-deck[im]'``）。
  官方称"五零"：零加解密 / 验签 / 证书 / 白名单 / 内网穿透，无需公网 IP；
* **HTTP 回调** —— 需要公网 URL，且要在 1500ms 内返回加密的 ``success``。

能力边界（重要）：群机器人 webhook 的 ActionCard **按钮只能跳 URL，不能回传
回调**。要真回调必须用企业应用的互动卡片（卡片平台建模板 + ``outTrackId``）。
因此本项目对钉钉采用"签名链接"路线：按钮跳 ``/api/im/action``，效果等价，
少一层卡片模板依赖（见 :mod:`sortie_deck.im.cards`）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from sortie_deck.im.base import ApprovalCard, CardAction, InboundEvent, SendResult
from sortie_deck.im.cards import dingtalk_card

logger = logging.getLogger(__name__)

API_BASE = "https://api.dingtalk.com"
_TOKEN_SAFETY_SECONDS = 300

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

# 群消息模板：按按钮数量选 sampleActionCard{N}，超出 4 个退回文本
_CARD_MSG_KEYS = {
    0: "sampleText",
    1: "sampleActionCard",
    2: "sampleActionCard2",
    3: "sampleActionCard3",
    4: "sampleActionCard4",
}


async def access_token(app_key: str, app_secret: str, *, force: bool = False) -> str:
    cached = _TOKEN_CACHE.get(app_key)
    if not force and cached and cached[1] > time.time() + _TOKEN_SAFETY_SECONDS:
        return cached[0]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{API_BASE}/v1.0/oauth2/accessToken",
            json={"appKey": app_key, "appSecret": app_secret},
        )
    data = resp.json()
    if "accessToken" not in data:
        raise RuntimeError(f"钉钉鉴权失败 code={data.get('code')} message={data.get('message')}")
    token = str(data["accessToken"])
    _TOKEN_CACHE[app_key] = (token, time.time() + float(data.get("expireIn") or 7200))
    return token


def parse_inbound(payload: dict[str, Any]) -> InboundEvent | None:
    """归一化钉钉回调。

    Stream 帧形如::

        {"type": "CALLBACK",
         "headers": {"topic": "/v1.0/im/bot/messages/get"},
         "data": {"conversationId": "cid", "msgId": "m1",
                  "senderNick": "张三", "senderStaffId": "u1",
                  "text": {"content": "hi"}}}

    卡片回调 topic 为 ``/v1.0/card/instances/callback``，``outTrackId`` 即
    我们发卡时写入的 ``decision_id``（钉钉没有 button value 通道）。
    """
    headers = payload.get("headers") or {}
    topic = str(headers.get("topic") or "")
    data = payload.get("data") or {}

    if topic.endswith("/card/instances/callback"):
        out_track_id = str(data.get("outTrackId") or "")
        content = data.get("content") or {}
        private = content.get("cardPrivateData") or {}
        action_ids = private.get("actionIds") or []
        return InboundEvent(
            provider="dingtalk",
            channel_id=str(data.get("conversationId") or data.get("chatId") or ""),
            sender_id=str(data.get("userId") or data.get("senderStaffId") or ""),
            sender_name=str(data.get("userId") or "dingtalk"),
            raw_id=str(data.get("eventId") or out_track_id or ""),
            action=CardAction(
                action=str(action_ids[0]) if action_ids else "",
                decision_id=out_track_id,
                operator_id=str(data.get("userId") or ""),
                raw=data,
            ),
        )

    text_block = data.get("text")
    if isinstance(text_block, dict):
        text = str(text_block.get("content") or "")
    else:
        text = str(data.get("content") or text_block or "")
    conversation = str(data.get("conversationId") or data.get("conversation_id") or "")
    if not conversation:
        return None
    return InboundEvent(
        provider="dingtalk",
        channel_id=conversation,
        sender_id=str(data.get("senderStaffId") or data.get("senderId") or ""),
        sender_name=str(data.get("senderNick") or data.get("senderStaffId") or "dingtalk"),
        text=text.strip(),
        raw_id=str(data.get("msgId") or data.get("eventId") or ""),
    )


class DingTalkProvider:
    """钉钉适配器。凭据取 ``conn.app_id``（= AppKey）+ 解密后的 AppSecret。"""

    name = "dingtalk"

    def __init__(self, secret_resolver, action_url_builder=None) -> None:
        self._secret = secret_resolver
        # 钉钉按钮只能跳 URL：由 manager 注入「带签名」的链接生成器 (card, button) -> str。
        # 没有注入时 send_card 会退化为纯文本（避免死按钮）。
        self._action_url_builder = action_url_builder

    async def probe(self, conn) -> tuple[bool, str]:
        if conn.mode == "stream":
            try:
                import dingtalk_stream  # noqa: F401
            except ImportError:
                return False, (
                    "未安装钉钉 Stream SDK：请 `pip install 'sortie-deck[im]'`，"
                    "或在表单中改用「使用 URL 回调」"
                )
        try:
            await access_token(conn.app_id, self._secret(conn), force=True)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return True, ""

    async def send_text(self, conn, chat_id: str, text: str) -> SendResult:
        return await self._send_group(
            conn, chat_id, "sampleText", json.dumps({"content": text}, ensure_ascii=False)
        )

    async def send_card(self, conn, chat_id: str, card: ApprovalCard) -> SendResult:
        builder = None
        if self._action_url_builder is not None:
            builder = lambda button: self._action_url_builder(card, button)  # noqa: E731
        raw = dingtalk_card(card, url_builder=builder)
        action_card = raw["actionCard"]
        buttons = action_card.get("btns") or []
        msg_key = _CARD_MSG_KEYS.get(len(buttons), "sampleText")
        if msg_key == "sampleText":
            # 没有签名链接（或按钮过多）时退回纯文本，避免死按钮
            return await self.send_text(conn, chat_id, card.to_text())
        msg_param: dict[str, Any] = {
            "title": action_card["title"],
            "text": action_card["text"],
        }
        for index, btn in enumerate(buttons, start=1):
            msg_param[f"buttonTitle{index}"] = btn["title"]
            msg_param[f"buttonUrl{index}"] = btn["actionURL"]
        return await self._send_group(
            conn, chat_id, msg_key, json.dumps(msg_param, ensure_ascii=False)
        )

    async def _send_group(self, conn, chat_id: str, msg_key: str, msg_param: str) -> SendResult:
        """向群会话发消息（``/v1.0/robot/groupMessages/send``）。"""
        try:
            token = await access_token(conn.app_id, self._secret(conn))
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{API_BASE}/v1.0/robot/groupMessages/send",
                    headers={"x-acs-dingtalk-access-token": token},
                    json={
                        "robotCode": conn.app_id,
                        "openConversationId": chat_id,
                        "msgKey": msg_key,
                        "msgParam": msg_param,
                    },
                )
            data = resp.json()
            if resp.status_code >= 300:
                return SendResult(
                    ok=False,
                    provider="dingtalk",
                    error=str(data.get("message") or f"HTTP {resp.status_code}"),
                )
            return SendResult(
                ok=True,
                provider="dingtalk",
                message_id=str(data.get("processQueryKey") or ""),
            )
        except Exception as exc:  # noqa: BLE001 — 出站失败不能中断任务
            logger.warning("钉钉发送失败 conn=%s chat=%s: %s", conn.id, chat_id, exc)
            return SendResult(ok=False, provider="dingtalk", error=str(exc))

    async def update_card(self, conn, chat_id: str, message_id: str, card: ApprovalCard) -> bool:
        """钉钉群消息不支持就地更新（互动卡片才支持），由调用方补发文本。"""
        return False

    def parse_inbound(self, payload: dict[str, Any]) -> InboundEvent | None:
        return parse_inbound(payload)
