"""Slack 适配器。

Slack 与飞书/钉钉有三处结构性差异，不能照抄：

1. **入站有两种完全不同的 Content-Type**：Events API 是 JSON，按钮回调
   （Interactivity）是 ``application/x-www-form-urlencoded`` 且真正的 JSON 在
   ``payload`` 字段里。直接 ``json.loads(body)`` 会 400。
2. **凭据不是「App ID + Secret」**：实际需要 **Bot Token**（``xoxb-``）与
   **Signing Secret**，前者本身就是凭据。因此 Bot Token 必须落在密文里，
   ``app_id`` 列只留空或放 App ID 用于展示——否则等于明文存储密钥。
3. **必须过滤机器人自己发的消息**：Slack 会把 bot 的回复也推成 ``message``
   事件，不过滤就会自己触发自己，形成无限循环。

平台硬约束：Slack 要求 **3 秒内**响应，超时按 ``X-Slack-Retry-Num`` 重推；
去重交给 :class:`sortie_deck.im.router.ImDedupe`（键为 ``event_id``）。

接入方式只支持 **Events API URL 回调**：Socket Mode 需要第三个凭据
（app-level token ``xapp-``），当前两字段表单放不下，且官方同样推荐 Events API。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from sortie_deck.bridges import verify_slack_signature
from sortie_deck.im.base import ApprovalCard, CardAction, InboundEvent, SendResult
from sortie_deck.im.cards import slack_card

logger = logging.getLogger(__name__)

API_BASE = "https://slack.com/api"

# app_mention 的文本里 <@U123> 是提及占位符，剥掉才是命令本体
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")

# 这些 subtype 不是人在说话，处理后会造成回环
_IGNORED_SUBTYPES = frozenset(
    {"bot_message", "message_changed", "message_deleted", "channel_join", "channel_leave"}
)


def signing_secret_of(raw_secret: str) -> str:
    """从存储的 secret 里取 Signing Secret（兼容旧格式）。"""
    _, signing = split_credentials(raw_secret)
    return signing


def bot_token_of(raw_secret: str) -> str:
    """从存储的 secret 里取 Bot Token（兼容旧格式）。"""
    token, _ = split_credentials(raw_secret)
    return token


def split_credentials(raw_secret: str) -> tuple[str, str]:
    """解析 ``{"bot_token": ..., "signing_secret": ...}``。

    兼容旧格式：整串就是 bot token（早期直接存一个 token 的部署）。
    """
    raw = (raw_secret or "").strip()
    if not raw:
        return "", ""
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw, ""
        if isinstance(data, dict):
            return (
                str(data.get("bot_token") or "").strip(),
                str(data.get("signing_secret") or "").strip(),
            )
    return raw, ""


def fold_credentials(app_id: str, secret: str) -> tuple[str, str]:
    """把表单的「Bot Token + Signing Secret」折成统一的 ``(app_id, secret)``。

    Bot Token 等价于凭据本身，不能明文写进 ``app_id`` 列——那列是有意设计成
    「可回显的非敏感标识」。所以两者一起进密文，``app_id`` 置空。
    """
    token = (app_id or "").strip()
    signing = (secret or "").strip()
    if not token or not signing:
        return app_id, secret
    return "", json.dumps({"bot_token": token, "signing_secret": signing}, ensure_ascii=False)


def parse_inbound(payload: dict[str, Any]) -> InboundEvent | None:
    """归一化 Slack 事件。

    ``url_verification`` 返回 None（由 API 层直接回 challenge）。
    """
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("type") or "")

    if kind == "url_verification":
        return None

    if kind == "block_actions":
        return _parse_action(payload)

    if kind == "event_callback":
        return _parse_event(payload)

    # Slash command（/sortie ...）走 form，字段是扁平的
    if payload.get("command") or payload.get("channel_id"):
        return _parse_slash(payload)

    return None


def _parse_slash(payload: dict[str, Any]) -> InboundEvent | None:
    channel = str(payload.get("channel_id") or "")
    if not channel:
        return None
    command = str(payload.get("command") or "").strip()
    rest = str(payload.get("text") or "").strip()
    # /sortie list → "list"；命令本体是子命令而不是 /sortie 本身
    text = f"{command.lstrip('/')} {rest}".strip() if command else rest
    if command and command.lstrip("/") in {"sortie", "deck"}:
        text = rest
    return InboundEvent(
        provider="slack",
        channel_id=channel,
        sender_id=str(payload.get("user_id") or ""),
        sender_name=str(payload.get("user_name") or payload.get("user_id") or "slack"),
        text=text,
        raw_id=str(payload.get("trigger_id") or payload.get("response_url") or text)[:120],
    )


def _parse_event(payload: dict[str, Any]) -> InboundEvent | None:
    event = payload.get("event") or {}
    if not isinstance(event, dict):
        return None
    etype = str(event.get("type") or "")
    if etype not in {"message", "app_mention"}:
        return None

    # 机器人自己的消息必须丢弃：Slack 会把 bot 发言也推成 message 事件，
    # 不过滤就是自己触发自己。
    if event.get("bot_id") or event.get("subtype") in _IGNORED_SUBTYPES:
        return None

    channel = str(event.get("channel") or "")
    if not channel:
        return None

    text = _MENTION_RE.sub("", str(event.get("text") or "")).strip()
    if not text:
        return None

    # 会话类型：channel / group / mpim 是群，im 是单聊
    chat_type = "single" if str(event.get("channel_type") or "") == "im" else "group"

    return InboundEvent(
        provider="slack",
        channel_id=channel,
        chat_type=chat_type,
        sender_id=str(event.get("user") or ""),
        sender_name=str(event.get("username") or event.get("user") or "slack"),
        text=text,
        raw_id=str(payload.get("event_id") or event.get("client_msg_id") or event.get("ts") or ""),
    )


def _parse_action(payload: dict[str, Any]) -> InboundEvent | None:
    actions = payload.get("actions") or []
    if not actions:
        return None
    action = actions[0] if isinstance(actions[0], dict) else {}
    channel = str((payload.get("channel") or {}).get("id") or "")
    if not channel:
        return None

    # cards.slack_card 写入的 value 形如 "dec_xxx:approve"
    value = str(action.get("value") or "")
    decision_id, _, act = value.partition(":")
    act = (act or str(action.get("action_id") or "")).strip()

    user = payload.get("user") or {}
    container = payload.get("container") or {}
    return InboundEvent(
        provider="slack",
        channel_id=channel,
        chat_type="single" if str(payload.get("channel_name") or "") == "directmessage" else "group",
        sender_id=str(user.get("id") or ""),
        sender_name=str(user.get("username") or user.get("name") or user.get("id") or "slack"),
        raw_id=str(payload.get("trigger_id") or container.get("message_ts") or ""),
        action=CardAction(
            action=act,
            decision_id=decision_id,
            operator_id=str(user.get("id") or ""),
            operator_name=str(user.get("username") or user.get("name") or ""),
            raw=action,
        ),
    )


class SlackProvider:
    """Slack 适配器。凭据取解密后的 JSON（bot_token + signing_secret）。"""

    name = "slack"

    def __init__(self, secret_resolver) -> None:
        self._secret = secret_resolver

    def _token(self, conn) -> str:
        return bot_token_of(self._secret(conn))

    async def probe(self, conn) -> tuple[bool, str]:
        token = self._token(conn)
        if not token:
            return False, "未填写 Bot Token"
        if not signing_secret_of(self._secret(conn)):
            return False, "未填写 Signing Secret：URL 回调无法验证请求来源"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{API_BASE}/auth.test",
                    headers={"Authorization": f"Bearer {token}"},
                )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return False, f"连接 Slack 失败：{exc}"
        if not data.get("ok"):
            return False, f"Slack 鉴权失败：{data.get('error') or 'unknown_error'}"
        return True, ""

    async def send_text(self, conn, chat_id: str, text: str) -> SendResult:
        return await self._post(conn, {"channel": chat_id, "text": text})

    async def send_card(self, conn, chat_id: str, card: ApprovalCard) -> SendResult:
        raw = slack_card(card)
        return await self._post(conn, {"channel": chat_id, **raw})

    async def _post(self, conn, payload: dict[str, Any]) -> SendResult:
        token = self._token(conn)
        if not token:
            return SendResult(ok=False, provider="slack", error="未填写 Bot Token")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{API_BASE}/chat.postMessage",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — 出站失败不能中断任务
            logger.warning(
                "Slack 发送失败 conn=%s chat=%s: %s", conn.id, payload.get("channel"), exc
            )
            return SendResult(ok=False, provider="slack", error=str(exc))
        if not data.get("ok"):
            return SendResult(
                ok=False, provider="slack", error=f"Slack 返回错误：{data.get('error')}"
            )
        return SendResult(ok=True, provider="slack", message_id=str(data.get("ts") or ""))

    async def update_card(self, conn, chat_id: str, message_id: str, card: ApprovalCard) -> bool:
        """就地更新已发送卡片。Slack 的 ``ts`` 即消息 ID。"""
        if not message_id:
            return False
        token = self._token(conn)
        if not token:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{API_BASE}/chat.update",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"channel": chat_id, "ts": message_id, **slack_card(card)},
                )
            return bool(resp.json().get("ok"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack 更新卡片失败 ts=%s: %s", message_id, exc)
            return False

    def parse_inbound(self, payload: dict[str, Any]) -> InboundEvent | None:
        return parse_inbound(payload)


__all__ = [
    "SlackProvider",
    "bot_token_of",
    "fold_credentials",
    "parse_inbound",
    "signing_secret_of",
    "split_credentials",
    "verify_slack_signature",
]
