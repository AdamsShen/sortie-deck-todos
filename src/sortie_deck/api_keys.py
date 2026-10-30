"""用户级 API Key：给远程 MCP / CLI 用的长期凭据。

设计要点：
- 明文只在创建时返回一次，库里只存 SHA-256（Key 是 32 字节随机，熵足够，
  不需要慢哈希）；比较用 ``hmac.compare_digest`` 防时序侧信道。
- 前缀单独存一列，列表页只显示前缀，日志也只打前缀。
- ``last_used_at`` 做 60s 节流：MCP 客户端会高频轮询，每次请求都写库没必要。
- 存储走 sqlite（与事件表同风格：WAL + busy_timeout + 线程锁），
  因为多进程（api / worker）都可能校验同一个 Key。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

KEY_PREFIX = "sk_sortie_"
_PREFIX_DISPLAY_LEN = len(KEY_PREFIX) + 8
_TOUCH_INTERVAL_SECONDS = 60.0


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ApiKey(BaseModel):
    """一条 API Key 记录（永不包含明文）。"""

    id: str
    user_id: str
    name: str
    prefix: str
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    created_at: float
    last_used_at: float | None = None
    revoked_at: float | None = None

    def is_active(self) -> bool:
        return self.revoked_at is None


class ApiKeyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                prefix TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                scopes TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_used_at REAL,
                revoked_at REAL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)"
        )
        self._conn.commit()

    # --- 写 ---------------------------------------------------------------

    def create(self, *, user_id: str, name: str) -> tuple[ApiKey, str]:
        """创建 Key，返回 (记录, 明文)。明文此后无法再取回。"""
        raw = KEY_PREFIX + secrets.token_urlsafe(32)
        now = time.time()
        key = ApiKey(
            id=f"key_{secrets.token_hex(6)}",
            user_id=user_id,
            name=(name or "").strip() or "未命名",
            prefix=raw[:_PREFIX_DISPLAY_LEN],
            created_at=now,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO api_keys (id, user_id, name, prefix, key_hash, scopes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    key.id,
                    key.user_id,
                    key.name,
                    key.prefix,
                    _hash_key(raw),
                    ",".join(key.scopes),
                    now,
                ),
            )
            self._conn.commit()
        logger.info("API Key 已创建: id=%s user=%s prefix=%s", key.id, user_id, key.prefix)
        return key, raw

    def revoke(self, *, user_id: str, key_id: str) -> bool:
        """吊销：返回是否真的改动了一行（不存在或非本人 → False）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_keys SET revoked_at=? "
                "WHERE id=? AND user_id=? AND revoked_at IS NULL",
                (time.time(), key_id, user_id),
            )
            self._conn.commit()
            changed = cur.rowcount > 0
        if changed:
            logger.info("API Key 已吊销: id=%s user=%s", key_id, user_id)
        return changed

    # --- 读 ---------------------------------------------------------------

    def verify(self, raw: str) -> ApiKey | None:
        """校验明文 Key；命中则刷新 last_used_at 并返回记录。"""
        token = (raw or "").strip()
        if not token.startswith(KEY_PREFIX):
            return None
        digest = _hash_key(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT id, user_id, name, prefix, key_hash, scopes, created_at, "
                "last_used_at, revoked_at FROM api_keys WHERE key_hash = ?",
                (digest,),
            ).fetchone()
            if not row:
                return None
            # 命中哈希本身已是等值比较，再做一次常量时间比较兜住实现变更
            if not hmac.compare_digest(str(row[4]), digest):
                return None
            key = _row_to_key(row)
            if not key.is_active():
                return None
            now = time.time()
            if key.last_used_at is None or now - key.last_used_at >= _TOUCH_INTERVAL_SECONDS:
                self._conn.execute(
                    "UPDATE api_keys SET last_used_at=? WHERE id=?", (now, key.id)
                )
                self._conn.commit()
                key.last_used_at = now
        return key

    def list_for_user(self, user_id: str) -> list[ApiKey]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, user_id, name, prefix, key_hash, scopes, created_at, "
                "last_used_at, revoked_at FROM api_keys WHERE user_id=? "
                "ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [_row_to_key(row) for row in rows]

    def get(self, key_id: str) -> ApiKey | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, user_id, name, prefix, key_hash, scopes, created_at, "
                "last_used_at, revoked_at FROM api_keys WHERE id=?",
                (key_id,),
            ).fetchone()
        return _row_to_key(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_key(row: tuple) -> ApiKey:
    return ApiKey(
        id=row[0],
        user_id=row[1],
        name=row[2],
        prefix=row[3],
        scopes=[s for s in str(row[5] or "").split(",") if s] or ["*"],
        created_at=row[6],
        last_used_at=row[7],
        revoked_at=row[8],
    )
