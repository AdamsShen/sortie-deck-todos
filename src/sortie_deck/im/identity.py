"""IM 身份绑定：把平台发送人映射到 Sortie 用户。

为什么必须有这一层：决策中心用 ``assignee`` 做「只有绑定决策人能批」的鉴权，
而 IM 侧回调带的是 ``open_id`` / ``userid`` / ``wxid``。不做映射，鉴权只能：
要么拒绝所有人（体验崩溃），要么放行所有人（等于没有鉴权）。

绑定方式是一条 6 位短码：用户在**工作台**里生成（已登录，身份可信），
再在 IM 里发 ``/bind ABC123`` 兑换。反过来（IM 里点一下就把自己绑成某人）
不可行——那等于任何人点一下就能冒充审批人。

短码是内存态、5 分钟有效、一次性；跨进程多副本时前端需粘到同一台 API
（与扫码会话同样的约束）。
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# 短码有效期：够用户在 IM 里发一条消息，又不至于长时间可被撞
LINK_CODE_TTL = 300.0
# 短码长度：6 位大写字母数字（去掉易混的 0/O/1/I）
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6


class ImIdentity(BaseModel):
    provider: str
    sender_id: str
    user_id: str
    username: str
    bound_at: float


class ImIdentityStore:
    """``(provider, sender_id)`` → ``(user_id, username)`` 映射。

    一个 Sortie 用户可以在多个渠道各绑一次（飞书 + 钉钉），
    但同一渠道的一个平台账号只能绑一个用户。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS im_identities (
                provider TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                bound_at REAL NOT NULL,
                PRIMARY KEY (provider, sender_id)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_im_identities_user ON im_identities(user_id)"
        )
        self._conn.commit()

    def bind(self, *, provider: str, sender_id: str, user_id: str, username: str) -> ImIdentity:
        if not sender_id:
            raise ValueError("sender_id 不能为空")
        if not user_id:
            raise ValueError("user_id 不能为空")
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO im_identities (provider, sender_id, user_id, username, bound_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, sender_id) DO UPDATE SET "
                "user_id = excluded.user_id, username = excluded.username, "
                "bound_at = excluded.bound_at",
                (provider, sender_id, user_id, username, now),
            )
            self._conn.commit()
        return ImIdentity(
            provider=provider,
            sender_id=sender_id,
            user_id=user_id,
            username=username,
            bound_at=now,
        )

    def resolve(self, provider: str, sender_id: str) -> ImIdentity | None:
        if not sender_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT provider, sender_id, user_id, username, bound_at "
                "FROM im_identities WHERE provider = ? AND sender_id = ?",
                (provider, sender_id),
            ).fetchone()
        if row is None:
            return None
        return ImIdentity(
            provider=row[0], sender_id=row[1], user_id=row[2], username=row[3], bound_at=row[4]
        )

    def list(self, provider: str | None = None) -> list[ImIdentity]:
        query = "SELECT provider, sender_id, user_id, username, bound_at FROM im_identities"
        params: tuple[Any, ...] = ()
        if provider:
            query += " WHERE provider = ?"
            params = (provider,)
        query += " ORDER BY bound_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            ImIdentity(
                provider=r[0],
                sender_id=r[1],
                user_id=r[2],
                username=r[3],
                bound_at=r[4],
            )
            for r in rows
        ]

    def unbind(self, provider: str, sender_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM im_identities WHERE provider = ? AND sender_id = ?",
                (provider, sender_id),
            )
            self._conn.commit()
        return cur.rowcount > 0


class LinkCodeService:
    """一次性绑定码（内存态）。"""

    def __init__(self, ttl: float = LINK_CODE_TTL) -> None:
        self.ttl = float(ttl)
        # code → (user_id, username, expires_at)
        self._codes: dict[str, tuple[str, str, float]] = {}
        self._lock = threading.Lock()

    def issue(self, *, user_id: str, username: str) -> tuple[str, float]:
        """生成短码。同一用户重复生成时旧码仍然有效（避免并发场景互相失效）。"""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        expires_at = time.time() + self.ttl
        with self._lock:
            self._prune_locked()
            self._codes[code] = (user_id, username, expires_at)
        return code, expires_at

    def redeem(self, code: str) -> tuple[str, str] | None:
        """兑换并立即作废（一次性）。返回 ``(user_id, username)``。"""
        key = (code or "").strip().upper()
        now = time.time()
        with self._lock:
            entry = self._codes.pop(key, None)
            if entry is None:
                return None
            user_id, username, expires_at = entry
            if expires_at < now:
                return None
        return user_id, username

    def _prune_locked(self) -> None:
        now = time.time()
        for key in [k for k, v in self._codes.items() if v[2] < now]:
            self._codes.pop(key, None)


def parse_bind_command(text: str) -> str | None:
    """识别 ``/bind ABC123``（容忍大小写与多余空格）。"""
    stripped = (text or "").strip()
    if not stripped.lower().startswith("/bind"):
        return None
    parts = stripped.split()
    if len(parts) < 2:
        return ""
    return parts[1].strip().upper()
