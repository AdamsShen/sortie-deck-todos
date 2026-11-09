"""飞书（Lark）适配器。

覆盖两种接入方式：

* **WebSocket 长连接** —— 需要 ``lark-oapi``（``pip install 'sortie-deck[im]'``）。
  未安装时 :meth:`probe` 返回可操作的错误，而不是假装连上了；
* **URL 回调** —— 纯 HTTP，无额外依赖，但要自己处理签名与 AES 解密。

平台硬约束：卡片回调必须 **3 秒内** 返回 HTTP 200；卡片发出后 30 天内可交互、
**14 天内可更新**，超期只能引导回工作台。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from typing import Any

import httpx

from sortie_deck.im.base import ApprovalCard, CardAction, InboundEvent, SendResult
from sortie_deck.im.cards import feishu_card

logger = logging.getLogger(__name__)

API_BASE = "https://open.feishu.cn/open-apis"
# tenant_access_token 有效期 2 小时，提前 5 分钟换新
_TOKEN_SAFETY_SECONDS = 300

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def decrypt_payload(encrypt_key: str, encrypt: str) -> dict[str, Any]:
    """解密飞书回调（配置了 Encrypt Key 时 body 只有 ``{"encrypt": ...}``）。

    算法：AES-256-CBC，key = sha256(encrypt_key)，iv 取密文前 16 字节。
    长连接模式由 SDK 内部完成，不走这里。
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not encrypt_key:
        raise ValueError("未配置 encrypt_key，无法解密飞书回调")
    raw = base64.b64decode(encrypt)
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv, data = raw[:16], raw[16:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    pad = plain[-1]
    if pad < 1 or pad > 32:
        raise ValueError("飞书回调解密后 padding 非法")
    return json.loads(plain[:-pad].decode("utf-8"))


def verify_signature(
    encrypt_key: str, timestamp: str, nonce: str, body: bytes, signature: str
) -> bool:
    """``X-Lark-Signature`` = sha256(timestamp + nonce + encrypt_key + body)。"""
    if not encrypt_key or not signature or not timestamp:
        return False
    base = (timestamp + nonce + encrypt_key).encode("utf-8") + body
    return hashlib.sha256(base).hexdigest() == signature


async def tenant_access_token(app_id: str, app_secret: str, *, force: bool = False) -> str:
    """取 tenant_access_token（进程内缓存，避免每条消息都换一次 token）。"""
    cache_key = f"{app_id}"
    cached = _TOKEN_CACHE.get(cache_key)
    if not force and cached and cached[1] > time.time() + _TOKEN_SAFETY_SECONDS:
        return cached[0]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书鉴权失败 code={data.get('code')} msg={data.get('msg')}")
    token = str(data.get("tenant_access_token") or "")
    _TOKEN_CACHE[cache_key] = (token, time.time() + float(data.get("expire") or 7200))
    return token


def parse_inbound(payload: dict[str, Any]) -> InboundEvent | None:
    """归一化飞书事件。

    支持 ``im.message.receive_v1``（文本）与 ``card.action.trigger``（按钮）。
    """
    header = payload.get("header") or {}
    event_type = str(header.get("event_type") or "")
    event = payload.get("event") or {}

    if event_type == "card.action.trigger":
        action = event.get("action") or {}
        value = action.get("value") or {}
        operator = event.get("operator") or {}
        context = event.get("context") or {}
        chat_id = str(context.get("open_chat_id") or operator.get("open_id") or "")
        if not chat_id:
            return None
        return InboundEvent(
            provider="feishu",
            channel_id=chat_id,
            sender_id=str(operator.get("open_id") or ""),
            sender_name=str(operator.get("user_id") or operator.get("open_id") or "feishu"),
            raw_id=str(header.get("event_id") or context.get("open_message_id") or ""),
            action=CardAction(
                action=str(value.get("action") or ""),
                decision_id=str(value.get("decision_id") or ""),
                operator_id=str(operator.get("open_id") or ""),
                operator_name=str(operator.get("user_id") or ""),
                raw=value,
            ),
        )

    if event.get("message"):
        message = event["message"]
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        content_raw = message.get("content") or "{}"
        try:
            content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
        except json.JSONDecodeError:
            content = {"text": str(content_raw)}
        text = str(content.get("text") or "").strip()
        # 群聊 @机器人 会带 @_user_1 占位符，剥掉后才是真正的命令
        if text.startswith("@_user_"):
            text = text.split(" ", 1)[-1].strip() if " " in text else ""
        chat_id = str(message.get("chat_id") or "")
        if not chat_id:
            return None
        return InboundEvent(
            provider="feishu",
            channel_id=chat_id,
            chat_type=str(message.get("chat_type") or "group"),
            sender_id=str(sender_id.get("open_id") or ""),
            sender_name=str(sender_id.get("user_id") or sender_id.get("open_id") or "feishu"),
            text=text,
            raw_id=str(message.get("message_id") or header.get("event_id") or ""),
        )
    return None


class FeishuProvider:
    """飞书适配器。凭据取 ``conn.app_id`` + 解密后的 secret。"""

    name = "feishu"

    def __init__(self, secret_resolver) -> None:
        # 适配器不碰存储：secret 由 manager 注入的解析器提供
        self._secret = secret_resolver

    async def _token(self, conn) -> str:
        return await tenant_access_token(conn.app_id, self._secret(conn))

    async def probe(self, conn) -> tuple[bool, str]:
        if conn.mode == "stream":
            try:
                import lark_oapi  # noqa: F401
            except ImportError:
                return False, (
                    "未安装飞书长连接 SDK：请 `pip install 'sortie-deck[im]'`，"
                    "或在表单中改用「使用 URL 回调」"
                )
        try:
            await tenant_access_token(conn.app_id, self._secret(conn), force=True)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return True, ""

    async def send_text(self, conn, chat_id: str, text: str) -> SendResult:
        return await self._send(conn, chat_id, "text", json.dumps({"text": text}))

    async def send_card(self, conn, chat_id: str, card: ApprovalCard) -> SendResult:
        return await self._send(
            conn, chat_id, "interactive", json.dumps(feishu_card(card), ensure_ascii=False)
        )

    async def _send(self, conn, chat_id: str, msg_type: str, content: str) -> SendResult:
        try:
            token = await self._token(conn)
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{API_BASE}/im/v1/messages",
                    params={"receive_id_type": "chat_id"},
                    headers={"Authorization": f"Bearer {token}"},
                    json={"receive_id": chat_id, "msg_type": msg_type, "content": content},
                )
            data = resp.json()
            if data.get("code") != 0:
                return SendResult(
                    ok=False,
                    provider="feishu",
                    error=f"code={data.get('code')} {data.get('msg')}",
                )
            message_id = (data.get("data") or {}).get("message_id") or ""
            return SendResult(ok=True, provider="feishu", message_id=str(message_id))
        except Exception as exc:  # noqa: BLE001 — 出站失败不能中断任务
            logger.warning("飞书发送失败 conn=%s chat=%s: %s", conn.id, chat_id, exc)
            return SendResult(ok=False, provider="feishu", error=str(exc))

    async def update_card(self, conn, chat_id: str, message_id: str, card: ApprovalCard) -> bool:
        """就地更新已发送卡片（14 天内有效）。

        没有 message_id 时只能返回 False，调用方会退化为补发一条纯文本结论。
        """
        if not message_id:
            return False
        try:
            token = await self._token(conn)
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.patch(
                    f"{API_BASE}/im/v1/messages/{message_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"content": json.dumps(feishu_card(card), ensure_ascii=False)},
                )
            return resp.json().get("code") == 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("飞书更新卡片失败 message=%s: %s", message_id, exc)
            return False

    def parse_inbound(self, payload: dict[str, Any]) -> InboundEvent | None:
        return parse_inbound(payload)
