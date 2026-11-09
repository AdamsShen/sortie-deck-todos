from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Providers that can carry an inbound human decision back into a mission.
# wecom 同时覆盖「企业微信智能机器人」与「微信客服」，二者会话 id 语义不同，
# 故映射表用 chat_type 区分 single / group，避免私聊和群聊 id 撞车。
PROVIDERS: tuple[str, ...] = ("slack", "feishu", "dingtalk", "wecom")


class BridgeMapping(BaseModel):
    channel_id: str
    initiative_id: str
    provider: str  # slack | feishu | dingtalk | wecom
    label: str = ""
    # single（单聊）| group（群聊）；旧记录无此字段时按 group 处理，
    # 因为历史接入的都是群机器人回调。
    chat_type: str = "group"
    # 绑定操作人（工作区级绑定：谁把这个会话接到任务上的，便于审计）
    created_by: str = ""


class BridgeStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _load(self) -> list[dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8") or "[]")

    def _save(self, rows: list[dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    def upsert(self, mapping: BridgeMapping) -> BridgeMapping:
        rows = self._load()
        rows = [
            r
            for r in rows
            if not (
                r.get("provider") == mapping.provider and r.get("channel_id") == mapping.channel_id
            )
        ]
        rows.append(mapping.model_dump())
        self._save(rows)
        return mapping

    def resolve(self, provider: str, channel_id: str) -> BridgeMapping | None:
        for r in self._load():
            if r.get("provider") == provider and r.get("channel_id") == channel_id:
                return BridgeMapping.model_validate(r)
        return None

    def list(self) -> list[BridgeMapping]:
        return [BridgeMapping.model_validate(r) for r in self._load()]


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    max_skew_seconds: int = 60 * 5,
) -> bool:
    if not signing_secret or not signature or not timestamp:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > max_skew_seconds:
        return False
    base = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def slack_text_from_payload(payload: dict[str, Any]) -> tuple[str | None, str, str]:
    """Return (channel_id, author_name, text) for slash/event payloads."""
    # Slash command form
    if payload.get("command") or payload.get("text") is not None and payload.get("channel_id"):
        channel = str(payload.get("channel_id") or "")
        user = str(payload.get("user_name") or payload.get("user_id") or "slack")
        text = str(payload.get("text") or "").strip()
        cmd = str(payload.get("command") or "").strip()
        if cmd and not text.startswith("/"):
            text = f"{cmd} {text}".strip()
        return channel, user, text
    # Event API
    event = payload.get("event") or {}
    if event.get("type") == "app_mention" or event.get("type") == "message":
        channel = str(event.get("channel") or "")
        user = str(event.get("user") or "slack")
        text = str(event.get("text") or "").strip()
        return channel, user, text
    return None, "slack", ""


def feishu_text_from_payload(payload: dict[str, Any]) -> tuple[str | None, str, str]:
    """Return (chat_id, author_name, text) for Feishu bot events."""
    header = payload.get("header") or {}
    event = payload.get("event") or payload
    # URL verification challenge handled by caller
    message = event.get("message") or {}
    chat_id = str(message.get("chat_id") or event.get("chat_id") or "")
    sender = event.get("sender") or {}
    author = str(
        (sender.get("sender_id") or {}).get("open_id") or sender.get("open_id") or "feishu"
    )
    content_raw = message.get("content") or "{}"
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
    except json.JSONDecodeError:
        content = {"text": str(content_raw)}
    text = str(content.get("text") or "").strip()
    # Strip @bot mention placeholders
    if "mention" in text.lower():
        text = text.split(" ", 1)[-1].strip() if " " in text else text
    _ = header
    return (chat_id or None), author, text


def dingtalk_text_from_payload(payload: dict[str, Any]) -> tuple[str | None, str, str]:
    """Return (conversation_id, author_name, text) for DingTalk bot callbacks.

    Handles both the outgoing robot callback shape (``conversationId`` +
    ``text.content``) and the plain ``{"text": {"content": ...}}`` shape used by
    simple webhook relays.
    """
    text_block = payload.get("text")
    if isinstance(text_block, dict):
        text = str(text_block.get("content") or "")
    else:
        text = str(payload.get("content") or text_block or "")
    channel = str(
        payload.get("conversationId")
        or payload.get("conversation_id")
        or payload.get("chatbotUserId")
        or ""
    )
    author = str(payload.get("senderNick") or payload.get("senderId") or "dingtalk")
    return (channel or None), author, text.strip()


def wecom_text_from_payload(payload: dict[str, Any]) -> tuple[str | None, str, str]:
    """Return (chat_id, author_name, text) for WeCom AI-bot callbacks.

    长连接帧形如 ``{"cmd": "aibot_msg_callback", "body": {...}}``：``body`` 内
    ``chatid`` 是会话标识、``chattype`` 为 single/group、``from.userid`` 是发送人。
    为兼容「URL 回调解密后直接拿到 body」的场景，也接受不带 ``cmd`` 外壳的扁平结构。
    """
    body = payload.get("body") if isinstance(payload.get("body"), dict) else payload
    text_block = body.get("text")
    if isinstance(text_block, dict):
        text = str(text_block.get("content") or "")
    else:
        text = str(body.get("content") or text_block or "")
    channel = str(body.get("chatid") or body.get("chat_id") or body.get("conversation_id") or "")
    sender = body.get("from") or {}
    author = str(
        (sender.get("userid") if isinstance(sender, dict) else "") or body.get("sender") or "wecom"
    )
    return (channel or None), author, text.strip()


def wecom_chat_type(payload: dict[str, Any]) -> str:
    """返回 ``single`` 或 ``group``，写入 BridgeMapping.chat_type。

    企微私聊 chatid 与群聊 chatid 位于不同命名空间，混存会让同一 id 的
    映射互相覆盖，因此绑定与解析两侧都必须带上这个维度。
    """
    body = payload.get("body") if isinstance(payload.get("body"), dict) else payload
    return "single" if str(body.get("chattype") or "").lower() == "single" else "group"


class MapBridgeRequest(BaseModel):
    provider: str
    channel_id: str
    initiative_id: str
    label: str = ""
