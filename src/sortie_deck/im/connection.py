"""IM 接入记录与连接生命周期。

一个「接入记录」= 一条工作区级的渠道绑定（用户已确认按工作区级，不是按人）。
本模块只管三件事：

1. 凭据怎么存 —— 明文只在 :meth:`ImConnectionStore.create` / ``update``
   时经过一次，库里只存 Fernet 密文，接口永不回显（复用 git 凭据那套主密钥）；
2. 状态怎么变 —— ``unconfigured → connecting → connected``，失败进 ``error``，
   凭据失效进 ``expired``；非法迁移直接抛错，避免"半死连接"静默存在；
3. 连接怎么管 —— :class:`ImConnectionManager` 持有各平台适配器，负责
   probe 与状态广播。

平台侧硬约束（决定了这里的设计）：

* 飞书长连接是集群模式**不广播**，多副本只有一个实例收得到消息 → 只允许一个
  进程建连（``TDT_IM_CONSUMER``），其余实例只读状态；
* 企微同一机器人**同时只允许一条**有效长连接，新连接会踢掉旧连接 → 多副本
  必须主备，且被踢后进 ``error`` 而不是无限重连打转。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from sortie_deck.git_cred import SecretKeyMissing, build_fernet
from sortie_deck.models import new_id

logger = logging.getLogger(__name__)

# 渠道（注意 wechat 指个人微信 iLink Bot，与 wecom 企业微信是两个产品）
PROVIDERS: tuple[str, ...] = ("wechat", "wecom", "feishu", "dingtalk", "slack")
# qr：扫码登录（微信）；stream：WebSocket 长连接；webhook：公网回调
MODES: tuple[str, ...] = ("qr", "stream", "webhook")
# 状态机
STATUSES: tuple[str, ...] = (
    "unconfigured",
    "connecting",
    "connected",
    "error",
    "expired",
    "disabled",
)
# 允许的状态迁移（只列显式允许项；同状态重复设置视为幂等）
_TRANSITIONS: dict[str, set[str]] = {
    "unconfigured": {"connecting", "disabled", "error"},
    "connecting": {"connected", "error", "expired", "disabled", "connecting"},
    "connected": {"error", "expired", "disabled", "connecting"},
    "error": {"connecting", "disabled", "unconfigured"},
    "expired": {"connecting", "disabled", "unconfigured"},
    "disabled": {"connecting", "unconfigured"},
}

# 每个渠道允许的接入方式：微信只能扫码，其余可长连接或回调
_ALLOWED_MODES: dict[str, set[str]] = {
    "wechat": {"qr"},
    "wecom": {"stream", "webhook"},
    "feishu": {"stream", "webhook"},
    "dingtalk": {"stream", "webhook"},
    "slack": {"stream", "webhook"},
}


class ImConnectionError(RuntimeError):
    """接入记录相关的业务错误（可直接展示给用户）。"""


class ImConnectionConflict(ImConnectionError):
    """同一渠道已存在工作区级连接。"""


class ImConnectionNotFound(ImConnectionError):
    pass


class InvalidTransition(ImConnectionError):
    pass


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def secret_fingerprint(secret: str) -> str:
    """不可逆指纹，用于人工核对"密钥换没换"，不泄露明文。"""
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()[:16]


class ImConnection(BaseModel):
    """一条接入记录（永不包含明文凭据）。"""

    id: str = Field(default_factory=lambda: new_id("imc_"))
    provider: str
    mode: str = "stream"
    label: str = ""
    status: str = "unconfigured"
    # 非敏感标识：飞书 App ID / 钉钉 AppKey / 企微 BotID / Slack App ID
    app_id: str = ""
    has_secret: bool = False
    secret_fingerprint: str = ""
    # 扫码登录用：二维码过期时间
    qr_expires_at: float | None = None
    last_error: str = ""
    last_connected_at: float | None = None
    # 最近一次收到平台事件的时间：用于诊断"连上了但订阅方式不匹配"
    events_seen_at: float | None = None
    enabled: bool = True
    created_by: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def to_public(self) -> dict[str, Any]:
        """对外视图。本身不含明文，这里只做 json 序列化。"""
        return self.model_dump(mode="json")

    def validate_config(self) -> None:
        """渠道与接入方式的组合校验（建连前调用）。"""
        if self.provider not in PROVIDERS:
            raise ImConnectionError(f"未知渠道: {self.provider}")
        if self.mode not in _ALLOWED_MODES.get(self.provider, set()):
            allowed = ", ".join(sorted(_ALLOWED_MODES.get(self.provider, set())))
            raise ImConnectionError(f"{self.provider} 不支持 {self.mode} 方式（可用: {allowed}）")


class ImConnectionStore:
    """接入记录的 sqlite 存储（与 git 凭据共用主密钥，各自独立表）。"""

    def __init__(self, path: Path, secret_key: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._secret_key = secret_key
        self._lock = threading.RLock()
        self._conn = _connect(self.path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS im_connections (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                mode TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unconfigured',
                app_id TEXT NOT NULL DEFAULT '',
                secret_cipher BLOB,
                secret_fingerprint TEXT NOT NULL DEFAULT '',
                qr_expires_at REAL,
                last_error TEXT NOT NULL DEFAULT '',
                last_connected_at REAL,
                events_seen_at REAL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        # 工作区级：一个渠道只允许一条连接，避免"两个飞书应用抢同一个工作区"
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_im_conn_provider ON im_connections(provider)"
        )
        self._conn.commit()

    # --- 内部 -------------------------------------------------------------

    def _fernet(self):
        if not self._secret_key:
            raise SecretKeyMissing(
                "未配置主密钥（TDT_SECRET_KEY / TDT_GIT_SECRET_KEY）："
                "拒绝保存 IM 凭据，不会降级为明文"
            )
        return build_fernet(self._secret_key)

    @staticmethod
    def _row_to_conn(row: tuple[Any, ...]) -> ImConnection:
        return ImConnection(
            id=row[0],
            provider=row[1],
            mode=row[2],
            label=row[3],
            status=row[4],
            app_id=row[5],
            has_secret=row[6] is not None,
            secret_fingerprint=row[7],
            qr_expires_at=row[8],
            last_error=row[9],
            last_connected_at=row[10],
            events_seen_at=row[11],
            enabled=bool(row[12]),
            created_by=row[13],
            created_at=row[14],
            updated_at=row[15],
        )

    _SELECT = (
        "SELECT id, provider, mode, label, status, app_id, secret_cipher, "
        "secret_fingerprint, qr_expires_at, last_error, last_connected_at, "
        "events_seen_at, enabled, created_by, created_at, updated_at FROM im_connections"
    )

    # --- 读 ---------------------------------------------------------------

    def list(self) -> list[ImConnection]:
        with self._lock:
            rows = self._conn.execute(f"{self._SELECT} ORDER BY created_at ASC").fetchall()
        return [self._row_to_conn(r) for r in rows]

    def get(self, conn_id: str) -> ImConnection:
        with self._lock:
            row = self._conn.execute(f"{self._SELECT} WHERE id = ?", (conn_id,)).fetchone()
        if row is None:
            raise ImConnectionNotFound(f"接入记录不存在: {conn_id}")
        return self._row_to_conn(row)

    def get_by_provider(self, provider: str) -> ImConnection | None:
        with self._lock:
            row = self._conn.execute(f"{self._SELECT} WHERE provider = ?", (provider,)).fetchone()
        return self._row_to_conn(row) if row else None

    def decrypt_secret(self, conn_id: str) -> str:
        """仅在建连/探活时调用；调用方不得把结果写日志或返回给前端。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT secret_cipher FROM im_connections WHERE id = ?", (conn_id,)
            ).fetchone()
        if row is None or row[0] is None:
            return ""
        try:
            return self._fernet().decrypt(row[0]).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 — 解密失败要给出可行动提示
            raise ImConnectionError(
                f"凭据解密失败（主密钥是否更换过？）: {conn_id} — {exc}"
            ) from exc

    # --- 写 ---------------------------------------------------------------

    def create(
        self,
        *,
        provider: str,
        mode: str,
        app_id: str = "",
        secret: str | None = None,
        label: str = "",
        created_by: str = "",
    ) -> ImConnection:
        """新建接入记录。``secret`` 可为空（微信扫码先建 pending 记录，扫码后回填）。"""
        conn = ImConnection(
            provider=provider,
            mode=mode,
            app_id=(app_id or "").strip(),
            label=(label or provider).strip(),
            status="unconfigured",
            created_by=created_by,
        )
        conn.validate_config()
        cipher = self._fernet().encrypt(secret.encode("utf-8")) if secret else None
        fingerprint = secret_fingerprint(secret) if secret else ""
        now = time.time()
        conn.has_secret = cipher is not None
        conn.secret_fingerprint = fingerprint
        conn.created_at = now
        conn.updated_at = now
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM im_connections WHERE provider = ?", (provider,)
            ).fetchone()
            if existing:
                raise ImConnectionConflict(
                    f"{provider} 已存在接入记录（{existing[0]}）；工作区级绑定每渠道仅一条，"
                    "请修改或删除后再试"
                )
            self._conn.execute(
                "INSERT INTO im_connections (id, provider, mode, label, status, app_id, "
                "secret_cipher, secret_fingerprint, enabled, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    conn.id,
                    conn.provider,
                    conn.mode,
                    conn.label,
                    conn.status,
                    conn.app_id,
                    cipher,
                    fingerprint,
                    conn.created_by,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return conn

    def update(
        self,
        conn_id: str,
        *,
        label: str | None = None,
        mode: str | None = None,
        app_id: str | None = None,
        secret: str | None = None,
        enabled: bool | None = None,
    ) -> ImConnection:
        conn = self.get(conn_id)
        if label is not None:
            conn.label = label.strip() or conn.label
        if mode is not None:
            conn.mode = mode
        if app_id is not None:
            conn.app_id = app_id.strip()
        if enabled is not None:
            conn.enabled = bool(enabled)
        conn.validate_config()
        sets: list[str] = ["label = ?", "mode = ?", "app_id = ?", "enabled = ?"]
        params: list[Any] = [conn.label, conn.mode, conn.app_id, int(conn.enabled)]
        if secret is not None:
            cipher = self._fernet().encrypt(secret.encode("utf-8"))
            sets += ["secret_cipher = ?", "secret_fingerprint = ?"]
            params += [cipher, secret_fingerprint(secret)]
            conn.has_secret = True
            conn.secret_fingerprint = secret_fingerprint(secret)
        params += [time.time(), conn_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE im_connections SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                params,
            )
            self._conn.commit()
        return self.get(conn_id)

    def set_secret(self, conn_id: str, secret: str) -> ImConnection:
        """扫码成功后回填 token。"""
        return self.update(conn_id, secret=secret)

    def set_status(
        self, conn_id: str, status: str, *, error: str = "", allow_force: bool = False
    ) -> ImConnection:
        """状态迁移。非法迁移抛 :class:`InvalidTransition`（同状态视为幂等）。"""
        if status not in STATUSES:
            raise ImConnectionError(f"未知状态: {status}")
        conn = self.get(conn_id)
        if (
            status != conn.status
            and not allow_force
            and status not in _TRANSITIONS.get(conn.status, set())
        ):
            raise InvalidTransition(f"不允许的状态迁移: {conn.status} → {status}")
        now = time.time()
        last_connected = now if status == "connected" else conn.last_connected_at
        with self._lock:
            self._conn.execute(
                "UPDATE im_connections SET status = ?, last_error = ?, last_connected_at = ?, "
                "updated_at = ? WHERE id = ?",
                (status, error, last_connected, now, conn_id),
            )
            self._conn.commit()
        return self.get(conn_id)

    def set_qr(self, conn_id: str, expires_at: float | None) -> ImConnection:
        with self._lock:
            self._conn.execute(
                "UPDATE im_connections SET qr_expires_at = ?, updated_at = ? WHERE id = ?",
                (expires_at, time.time(), conn_id),
            )
            self._conn.commit()
        return self.get(conn_id)

    def touch_event(self, conn_id: str, at: float | None = None) -> None:
        """记录"最近一次真的收到平台事件"，用于识别订阅方式不匹配。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE im_connections SET events_seen_at = ?, updated_at = ? WHERE id = ?",
                (at or now, now, conn_id),
            )
            self._conn.commit()

    def delete(self, conn_id: str) -> None:
        with self._lock:
            cur = self._conn.execute("DELETE FROM im_connections WHERE id = ?", (conn_id,))
            self._conn.commit()
        if cur.rowcount == 0:
            raise ImConnectionNotFound(f"接入记录不存在: {conn_id}")


class StatusSink(Protocol):
    """状态变更回调：API 层用它把 im.status 推到 SSE。"""

    def __call__(self, conn: ImConnection) -> None: ...


class ImConnectionManager:
    """连接生命周期管理。

    适配器实现 :class:`sortie_deck.im.base.ImProvider`；本类不关心具体协议，
    只负责探活、状态迁移与广播。
    """

    def __init__(
        self,
        *,
        store: ImConnectionStore,
        providers: dict[str, Any] | None = None,
        consumer: bool = True,
        on_status: StatusSink | None = None,
    ) -> None:
        self.store = store
        self.providers = dict(providers or {})
        # 只允许一个进程建连（飞书/钉钉集群不广播、企微会踢掉旧连接）
        self.consumer = bool(consumer)
        self.on_status = on_status

    def provider_for(self, conn: ImConnection) -> Any:
        provider = self.providers.get(conn.provider)
        if provider is None:
            raise ImConnectionError(f"渠道适配器未注册: {conn.provider}")
        return provider

    def _publish(self, conn: ImConnection) -> None:
        if self.on_status is not None:
            try:
                self.on_status(conn)
            except Exception as exc:  # noqa: BLE001 — 广播失败不能影响连接本身
                logger.warning("IM 状态广播失败 conn=%s: %s", conn.id, exc)

    def list_public(self) -> list[dict[str, Any]]:
        return [c.to_public() for c in self.store.list()]

    async def probe(self, conn_id: str) -> ImConnection:
        """用当前凭据探活；成功置 connected，失败置 error 并保留平台原始错误。"""
        conn = self.store.get(conn_id)
        conn.validate_config()
        provider = self.provider_for(conn)
        self._publish(self.store.set_status(conn_id, "connecting"))
        try:
            ok, error = await provider.probe(conn)
        except Exception as exc:  # noqa: BLE001 — 适配器异常一律表现为探活失败
            ok, error = False, str(exc)
        if ok and conn.mode == "stream" and conn.provider in {"feishu", "dingtalk"}:
            # 探活只能证明凭据可用，真正的长连接由 runner 建立。停在 connecting，
            # 收到首条事件才升 connected —— 否则用户会看到"已连接"却永远收不到消息。
            updated = self.store.set_status(conn_id, "connecting", allow_force=True)
        else:
            updated = (
                self.store.set_status(conn_id, "connected")
                if ok
                else self.store.set_status(conn_id, "error", error=error or "探活失败")
            )
        self._publish(updated)
        return updated

    def health(self, conn_id: str) -> dict[str, Any]:
        """连接健康度。

        长连接的可信信号是「收到过事件」，不是「线程起来了」：
        ``connecting`` 且从未收到事件 = 等待首条；``connected`` 但长时间无事件
        = 订阅方式可能不匹配。两者提示不同，避免把正常的握手中状态说成故障。
        """
        conn = self.store.get(conn_id)
        now = time.time()
        awaiting_first = bool(
            conn.status == "connecting"
            and conn.events_seen_at is None
            and conn.provider in {"feishu", "dingtalk"}
        )
        stale = bool(
            conn.status == "connected"
            and (conn.events_seen_at is None or now - conn.events_seen_at > 6 * 3600)
        )
        hint = ""
        if awaiting_first:
            hint = "长连接已建立，等待首条事件：在群里 @ 一次机器人即可确认"
        elif stale:
            hint = "已连接但长时间未收到事件：请确认平台后台的订阅方式与本地选择一致"
        return {
            "id": conn.id,
            "status": conn.status,
            "last_error": conn.last_error,
            "last_connected_at": conn.last_connected_at,
            "events_seen_at": conn.events_seen_at,
            "awaiting_first_event": awaiting_first,
            "hint": hint,
        }
