"""用户级 API Key：给远程 MCP / CLI 用的长期凭据。

设计要点：
- 明文只在创建时返回一次，库里只存 SHA-256（Key 是 32 字节随机，熵足够，
  不需要慢哈希）；比较用 ``hmac.compare_digest`` 防时序侧信道。
- 前缀单独存一列，列表页只显示前缀，日志也只打前缀。
- ``last_used_at`` 做 60s 节流：MCP 客户端会高频轮询，每次请求都写库没必要。
- 有效期按**日历月**推进（1/3/6/12 个月），默认 1 个月；到期后 ``verify`` 直接
  拒绝，列表仍保留记录，由用户手动删除。
- 删除是物理删除（``DELETE``），不是吊销；``revoked_at`` 仅为历史数据保留。
- 存储走 sqlite（与事件表同风格：WAL + busy_timeout + 线程锁），
  因为多进程（api / worker）都可能校验同一个 Key。
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

KEY_PREFIX = "sk_sortie_"
_PREFIX_DISPLAY_LEN = len(KEY_PREFIX) + 8
_TOUCH_INTERVAL_SECONDS = 60.0

#: 允许的有效期（月）。前端只提供这四个选项，后端再兜一层。
ALLOWED_TTL_MONTHS = (1, 3, 6, 12)
DEFAULT_TTL_MONTHS = 1


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _add_months(ts: float, months: int) -> float:
    """按日历月推进时间戳。

    不能用 ``months * 30`` 天：1 月 31 日签发的 1 个月应该是 2 月最后一天，
    而不是 3 月 2 日。月末对齐用 ``calendar.monthrange`` 处理。
    """
    dt = datetime.fromtimestamp(ts, tz=UTC)
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day).timestamp()


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
    expires_at: float | None = None

    def is_active(self, now: float | None = None) -> bool:
        """未吊销且未过期才算可用；历史数据 expires_at 为空视为长期有效。"""
        if self.revoked_at is not None:
            return False
        return not self.is_expired(now)

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_at


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
                revoked_at REAL,
                expires_at REAL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)"
        )
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """幂等迁移：老库只有 revoked_at，没有 expires_at。

        历史行 ``expires_at`` 为 NULL，视为长期有效——不能让升级把旧 Key 全废掉。
        """
        columns = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(api_keys)").fetchall()
        }
        if "expires_at" not in columns:
            self._conn.execute("ALTER TABLE api_keys ADD COLUMN expires_at REAL")

    # --- 写 ---------------------------------------------------------------

    def create(
        self,
        *,
        user_id: str,
        name: str,
        expires_in_months: int = DEFAULT_TTL_MONTHS,
    ) -> tuple[ApiKey, str]:
        """创建 Key，返回 (记录, 明文)。明文此后无法再取回。"""
        if expires_in_months not in ALLOWED_TTL_MONTHS:
            raise ValueError(f"expires_in_months must be one of {ALLOWED_TTL_MONTHS}")

        raw = KEY_PREFIX + secrets.token_urlsafe(32)
        now = time.time()
        key = ApiKey(
            id=f"key_{secrets.token_hex(6)}",
            user_id=user_id,
            name=(name or "").strip() or "未命名",
            prefix=raw[:_PREFIX_DISPLAY_LEN],
            created_at=now,
            expires_at=_add_months(now, expires_in_months),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO api_keys "
                "(id, user_id, name, prefix, key_hash, scopes, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key.id,
                    key.user_id,
                    key.name,
                    key.prefix,
                    _hash_key(raw),
                    ",".join(key.scopes),
                    now,
                    key.expires_at,
                ),
            )
            self._conn.commit()
        logger.info(
            "API Key 已创建: id=%s user=%s prefix=%s expires_at=%s ttl_months=%s",
            key.id,
            user_id,
            key.prefix,
            key.expires_at,
            expires_in_months,
        )
        return key, raw

    def delete(self, *, user_id: str, key_id: str) -> bool:
        """物理删除：返回是否真的删掉一行（不存在或非本人 → False）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT prefix FROM api_keys WHERE id=? AND user_id=?",
                (key_id, user_id),
            ).fetchone()
            if not row:
                return False
            self._conn.execute(
                "DELETE FROM api_keys WHERE id=? AND user_id=?", (key_id, user_id)
            )
            self._conn.commit()
        # 只打前缀，不打印明文与哈希
        logger.info("API Key 已删除: id=%s user=%s prefix=%s", key_id, user_id, row[0])
        return True

    # --- 读 ---------------------------------------------------------------

    def verify(self, raw: str) -> ApiKey | None:
        """校验明文 Key；命中且未过期则刷新 last_used_at 并返回记录。"""
        token = (raw or "").strip()
        if not token.startswith(KEY_PREFIX):
            return None
        digest = _hash_key(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT id, user_id, name, prefix, key_hash, scopes, created_at, "
                "last_used_at, revoked_at, expires_at FROM api_keys WHERE key_hash = ?",
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
                "last_used_at, revoked_at, expires_at FROM api_keys WHERE user_id=? "
                "ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [_row_to_key(row) for row in rows]

    def get(self, key_id: str) -> ApiKey | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, user_id, name, prefix, key_hash, scopes, created_at, "
                "last_used_at, revoked_at, expires_at FROM api_keys WHERE id=?",
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
        expires_at=row[9],
    )
