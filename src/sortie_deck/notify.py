"""Outbound notifications with redaction and multi-channel routing.

One place is responsible for reaching a human: the generic webhook (existing
behaviour), Feishu, and DingTalk (robot webhook + signed URL).

Two rules apply everywhere:

* secrets and tokens never leave the process — every payload is redacted;
* a channel that fails degrades (returns ``ok=False``) instead of raising, so a
  dead IM endpoint can never stall a mission.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import time
import urllib.parse
from typing import Any, Literal

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

Provider = Literal["webhook", "feishu", "dingtalk"]

DEFAULT_SUMMARY_LIMIT = 800

_SECRET_RE = re.compile(
    r"("
    r"sk-[A-Za-z0-9_\-]{6,}"
    r"|Bearer\s+[A-Za-z0-9._\-]+"
    r"|(?:api[_-]?key|secret|token|password|passwd|signature|appId|appSecret)\s*[:=]\s*\S+"
    r")",
    re.IGNORECASE,
)


def redact(text: str, *, limit: int = DEFAULT_SUMMARY_LIMIT) -> str:
    """Strip obvious secrets and truncate before anything leaves the process."""
    cleaned = _SECRET_RE.sub("[REDACTED]", str(text or ""))
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def dingtalk_sign(secret: str, timestamp_ms: int | None = None) -> tuple[str, int]:
    """Return ``(sign, timestamp)`` for a DingTalk robot webhook."""
    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    payload = f"{ts}\n{secret}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest).decode()), ts


def signed_url(url: str, secret: str) -> str:
    """Append DingTalk `timestamp`/`sign` query params when a secret is configured."""
    if not url or not secret:
        return url
    sign, ts = dingtalk_sign(secret)
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}timestamp={ts}&sign={sign}"


class NotificationChannel(BaseModel):
    provider: Provider = "webhook"
    url: str = ""
    secret: str = ""
    label: str = ""
    enabled: bool = True


class NotificationResult(BaseModel):
    provider: str
    label: str = ""
    ok: bool = False
    status_code: int | None = None
    error: str = ""


def build_payload(
    provider: Provider,
    *,
    title: str,
    body: str,
    meta: dict[str, Any] | None = None,
    level: str = "summary",
) -> dict[str, Any]:
    text = f"{title}\n{body}".strip()
    if provider == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    if provider == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    return {"event": "decision", "title": title, "body": body, "level": level, **(meta or {})}


class Notifier:
    """Fan-out sender. Every channel is independent and best-effort."""

    def __init__(
        self,
        *,
        channels: list[NotificationChannel] | None = None,
        webhook_url: str | None = None,
        timeout: float = 5.0,
        enabled: bool = True,
    ) -> None:
        self.channels: list[NotificationChannel] = list(channels or [])
        if webhook_url:
            self.channels.append(NotificationChannel(provider="webhook", url=webhook_url, label="hook"))
        self.timeout = float(timeout)
        self.enabled = bool(enabled)

    def active_channels(self) -> list[NotificationChannel]:
        return [c for c in self.channels if c.enabled and c.url]

    async def send(
        self,
        *,
        title: str,
        body: str,
        meta: dict[str, Any] | None = None,
        level: str = "summary",
        limit: int = DEFAULT_SUMMARY_LIMIT,
    ) -> list[NotificationResult]:
        """Send to every configured channel. Never raises."""
        results: list[NotificationResult] = []
        if not self.enabled:
            return results
        safe_body = redact(body, limit=limit)
        for channel in self.active_channels():
            payload = build_payload(
                channel.provider, title=redact(title, limit=200), body=safe_body, meta=meta, level=level
            )
            url = (
                signed_url(channel.url, channel.secret)
                if channel.provider == "dingtalk"
                else channel.url
            )
            results.append(await self._post(channel, url, payload))
        if not results:
            logger.info("notify: no active channel for %r", title)
        return results

    async def _post(
        self, channel: NotificationChannel, url: str, payload: dict[str, Any]
    ) -> NotificationResult:
        result = NotificationResult(provider=channel.provider, label=channel.label)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload)
            result.status_code = response.status_code
            result.ok = 200 <= response.status_code < 300
            if not result.ok:
                result.error = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001 — a dead channel must not stall the mission
            result.error = str(exc)
            logger.warning("notify %s failed: %s", channel.provider, exc)
        return result
