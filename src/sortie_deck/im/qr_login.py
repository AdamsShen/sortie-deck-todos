"""扫码登录会话（当前仅微信 iLink Bot）。

为什么单独抽一个模块：扫码是**有状态的一次性流程**，而连接记录是长期状态。
把「这次扫码进行到哪一步」塞进 ``im_connections`` 会让表结构被临时态污染；
这里用进程内短会话（默认 3 分钟）承载，确认后只把 ``bot_token`` 落库。

多副本注意：扫码会话不跨进程，所以**发起扫码的实例和轮询状态的实例必须是同一个**
（前端固定打同一台 API 即可；若走负载均衡，需要会话粘性）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from sortie_deck.im import wechat
from sortie_deck.im.connection import ImConnectionError, ImConnectionStore

logger = logging.getLogger(__name__)

# 二维码默认有效期（服务端未返回有效期时的兜底）
DEFAULT_QR_TTL = 180.0
# 会话垃圾回收阈值：过期后再留一段时间便于前端读到 expired 状态
SESSION_KEEP_ALIVE = 600.0


class QrSession(BaseModel):
    ticket: str
    conn_id: str
    provider: str = "wechat"
    qrcode: str = ""
    qr_content: str = ""
    # 已渲染好的二维码（SVG data URI）。扫码内容不变，无需每次轮询重算。
    qr_image: str = ""
    status: str = "pending"  # pending | scaned | confirmed | expired | need_verifycode | error
    error: str = ""
    created_at: float = Field(default_factory=time.time)
    expires_at: float = Field(default_factory=lambda: time.time() + DEFAULT_QR_TTL)
    confirmed_at: float | None = None

    def to_public(self) -> dict[str, Any]:
        """前端契约：``qr_content`` 用于渲染二维码，``expires_in`` 用于倒计时。"""
        remaining = max(0, int(self.expires_at - time.time()))
        return {
            "ticket": self.ticket,
            "status": self.status,
            "qr_content": self.qr_content,
            "qr_image": self.qr_image,
            "expires_in": remaining,
            "error": self.error,
        }


class QrLoginService:
    """扫码会话管理。确认后回填 bot_token 并建连。"""

    def __init__(self, *, store: ImConnectionStore, ttl: float = DEFAULT_QR_TTL) -> None:
        self.store = store
        self.ttl = float(ttl)
        self._sessions: dict[str, QrSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, conn_id: str) -> QrSession:
        """申请二维码。要求该记录是 wechat + qr 模式且尚未拿到 token。"""
        conn = self.store.get(conn_id)
        if conn.provider != "wechat":
            raise ImConnectionError("只有微信渠道支持扫码登录")
        if conn.mode != "qr":
            raise ImConnectionError("请先把接入方式设为扫码")

        data = await wechat.get_bot_qrcode()
        session = QrSession(
            ticket=f"qrt_{int(time.time() * 1000)}",
            conn_id=conn_id,
            qrcode=str(data.get("qrcode") or ""),
            qr_content=str(data.get("qrcode_img_content") or data.get("qrcode") or ""),
            expires_at=time.time() + self.ttl,
        )
        session.qr_image = wechat.qr_data_uri(session.qr_content)
        async with self._lock:
            self._sessions[session.ticket] = session
            self._prune_locked()
        # 记录二维码过期时间，管理页卡片可据此显示"已过期，请重新获取"
        self.store.set_qr(conn_id, session.expires_at)
        return session

    async def poll(self, ticket: str) -> QrSession:
        """轮询扫码状态；``confirmed`` 时落库 token 并把连接置为已连接。"""
        session = self._sessions.get(ticket)
        if session is None:
            raise ImConnectionError(f"扫码会话不存在或已清理: {ticket}")
        if session.status in {"confirmed", "expired", "error"}:
            return session

        if time.time() > session.expires_at:
            session.status = "expired"
            return session

        try:
            data = await wechat.get_qrcode_status(session.qrcode)
        except Exception as exc:  # noqa: BLE001 — 轮询失败不终结会话，让前端重试
            session.error = str(exc)
            return session

        status = str(data.get("status") or "wait")
        if status == "wait":
            return session
        if status == "scaned":
            session.status = "scaned"
            return session
        if status == "need_verifycode":
            session.status = "need_verifycode"
            session.error = "该账号需要输入验证码（暂不支持，请在微信里完成验证后重新扫码）"
            return session
        if status == "expired":
            session.status = "expired"
            return session
        if status != "confirmed":
            session.error = f"未知扫码状态: {status}"
            return session

        bot_token = str(data.get("bot_token") or "")
        if not bot_token:
            session.status = "error"
            session.error = "扫码已确认但未返回 bot_token"
            return session

        # 回填凭据：token 加密落库；ilink_bot_id 存 app_id 便于人工核对是哪个号
        session.status = "confirmed"
        session.confirmed_at = time.time()
        self.store.update(
            session.conn_id,
            app_id=str(data.get("ilink_bot_id") or ""),
            secret=bot_token,
        )
        try:
            if self.store.get(session.conn_id).status != "connected":
                self.store.set_status(session.conn_id, "connecting")
                self.store.set_status(session.conn_id, "connected")
        except Exception as exc:  # noqa: BLE001 — 状态迁移失败不影响 token 已落库
            logger.warning("扫码成功后置连接状态失败 conn=%s: %s", session.conn_id, exc)
        self.store.set_qr(session.conn_id, None)
        logger.info(
            "微信扫码登录成功 conn=%s ilink_bot_id=%s",
            session.conn_id,
            data.get("ilink_bot_id"),
        )
        return session

    def _prune_locked(self) -> None:
        cutoff = time.time() - SESSION_KEEP_ALIVE
        for ticket in [k for k, v in self._sessions.items() if v.created_at < cutoff]:
            self._sessions.pop(ticket, None)

    def get(self, ticket: str) -> QrSession | None:
        return self._sessions.get(ticket)
