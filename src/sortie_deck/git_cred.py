"""两级 Git 凭据：加密存储、脱敏视图、按 host 解析。

设计（见 docs/plans/2026-11-03-git-credentials-two-tier.md）：

- 作用域两级：``workspace``（管理员配置，全员可选用该仓库）/ ``user``（个人私有，仅本人可见）；
- 明文只在 :meth:`GitCredentialStore.create` 时经过一次，库里只存 Fernet 密文，
  主密钥来自 ``TDT_GIT_SECRET_KEY``；未配置时**拒绝写入**，绝不降级明文；
- 对外只暴露 :meth:`GitCredential.to_public`（host/scheme/fingerprint 等），永不返回 secret；
- 同一 ``(scope, owner_id, host, scheme, username)`` 唯一，避免解析时出现歧义；
- 存储走 sqlite（WAL + busy_timeout + 线程锁），api / worker 多进程共享同一个库文件。
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel

from sortie_deck.models import new_id

logger = logging.getLogger(__name__)

SCOPE_WORKSPACE = "workspace"
SCOPE_USER = "user"
_VALID_SCOPES = {SCOPE_WORKSPACE, SCOPE_USER}
_VALID_SCHEMES = {"https", "ssh"}


class GitCredentialError(RuntimeError):
    """凭据相关错误基类（写失败、解析失败统一归此）。"""


class SecretKeyMissing(GitCredentialError):
    """未配置 TDT_GIT_SECRET_KEY：拒绝保存凭据，避免明文落盘。"""


class GitCredentialConflict(GitCredentialError):
    """同一 host+scheme+username 已有凭据。"""


class CredentialMissing(GitCredentialError):
    """解析不到可用凭据（不存在 / 已禁用 / 已过期 / 不属于该用户）。"""


def new_secret_key() -> str:
    """生成 32 字节 urlsafe base64 主密钥（可直接写进 TDT_GIT_SECRET_KEY）。"""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _build_fernet(secret_key: str) -> Fernet:
    """构造 Fernet；支持逗号分隔多密钥（新钥在前）用于轮换。"""
    keys = [k.strip() for k in (secret_key or "").split(",") if k.strip()]
    if not keys:
        raise SecretKeyMissing(
            "TDT_GIT_SECRET_KEY 未配置：无法加密保存 git 凭据（不会降级为明文存储）"
        )
    try:
        return Fernet(keys[0].encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise SecretKeyMissing(f"TDT_GIT_SECRET_KEY 非法（需 urlsafe base64 32 字节）: {exc}") from exc


def _fingerprint(secret: str) -> str:
    """展示用指纹：只留末 4 位，长度上限 8，不足以泄露原值。"""
    if len(secret) < 8:
        return "…"
    return f"…{secret[-4:]}"


def _connect(path: Path) -> sqlite3.Connection:
    """共用 sqlite 连接配置（WAL + busy_timeout，多进程 api/worker 共享）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class GitCredential(BaseModel):
    """一条凭据记录（永不包含明文）。"""

    id: str
    scope: str
    owner_id: str
    label: str
    host: str
    scheme: str
    username: str = ""
    fingerprint: str
    expires_at: float | None = None
    disabled_at: float | None = None
    created_at: float
    last_used_at: float | None = None

    def is_active(self, now: float | None = None) -> bool:
        """未禁用且未过期才算可用。"""
        if self.disabled_at is not None:
            return False
        if self.expires_at is None:
            return True
        return (now if now is not None else time.time()) < self.expires_at

    def to_public(self) -> dict[str, Any]:
        """对外视图：不含 secret，也不含可逆信息。"""
        return {
            "id": self.id,
            "scope": self.scope,
            "label": self.label,
            "host": self.host,
            "scheme": self.scheme,
            "username": self.username,
            "fingerprint": self.fingerprint,
            "expires_at": self.expires_at,
            "disabled_at": self.disabled_at,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


class GitCredentialStore:
    """凭据表读写（与仓库注册表共用同一个 sqlite 文件）。"""

    def __init__(self, path: Path, secret_key: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._secret_key = secret_key
        self._lock = threading.RLock()
        self._conn = _connect(self.path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS git_credentials (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                label TEXT NOT NULL,
                host TEXT NOT NULL,
                scheme TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                secret_cipher BLOB NOT NULL,
                fingerprint TEXT NOT NULL,
                expires_at REAL,
                disabled_at REAL,
                created_at REAL NOT NULL,
                last_used_at REAL
            )
            """
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_git_cred_owner_host "
            "ON git_credentials(scope, owner_id, host, scheme, username)"
        )
        self._conn.commit()

    # --- 内部工具 ---------------------------------------------------------

    def _fernet(self) -> Fernet:
        if not self._secret_key:
            raise SecretKeyMissing(
                "TDT_GIT_SECRET_KEY 未配置：无法读写 git 凭据（不会降级为明文存储）"
            )
        return _build_fernet(self._secret_key)

    @staticmethod
    def _row_to_credential(row: tuple[Any, ...]) -> GitCredential:
        return GitCredential(
            id=row[0],
            scope=row[1],
            owner_id=row[2],
            label=row[3],
            host=row[4],
            scheme=row[5],
            username=row[6] or "",
            fingerprint=row[7],
            expires_at=row[8],
            disabled_at=row[9],
            created_at=row[10],
            last_used_at=row[11],
        )

    _SELECT = (
        "SELECT id, scope, owner_id, label, host, scheme, username, fingerprint, "
        "expires_at, disabled_at, created_at, last_used_at FROM git_credentials"
    )

    # --- 写 ---------------------------------------------------------------

    def create(
        self,
        *,
        scope: str,
        owner_id: str,
        label: str,
        host: str,
        scheme: str = "https",
        username: str = "",
        secret: str,
        expires_at: float | None = None,
    ) -> GitCredential:
        """写入一条凭据，返回脱敏记录（明文此后无法再读回，只能 decrypt 用于执行）。"""
        if scope not in _VALID_SCOPES:
            raise GitCredentialError(f"scope 非法: {scope}")
        if scheme not in _VALID_SCHEMES:
            raise GitCredentialError(f"scheme 非法: {scheme}")
        host = (host or "").strip().lower()
        if not host:
            raise GitCredentialError("host 不能为空")
        secret = (secret or "").strip()
        if not secret:
            raise GitCredentialError("secret 不能为空")

        cipher = self._fernet().encrypt(secret.encode("utf-8"))
        now = time.time()
        credential = GitCredential(
            id=new_id("crd_"),
            scope=scope,
            owner_id=owner_id,
            label=(label or host).strip(),
            host=host,
            scheme=scheme,
            username=(username or "").strip(),
            fingerprint=_fingerprint(secret),
            expires_at=expires_at,
            created_at=now,
        )
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO git_credentials "
                    "(id, scope, owner_id, label, host, scheme, username, secret_cipher, "
                    " fingerprint, expires_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        credential.id,
                        credential.scope,
                        credential.owner_id,
                        credential.label,
                        credential.host,
                        credential.scheme,
                        credential.username,
                        cipher,
                        credential.fingerprint,
                        credential.expires_at,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise GitCredentialConflict(
                    f"该 host 已存在凭据: {host} ({scheme}, username={credential.username})"
                ) from exc
            self._conn.commit()
        logger.info(
            "git 凭据已写入: id=%s scope=%s host=%s scheme=%s user=%s",
            credential.id,
            scope,
            host,
            scheme,
            owner_id,
        )
        return credential

    def upsert(
        self,
        *,
        scope: str,
        owner_id: str,
        label: str,
        host: str,
        scheme: str = "https",
        username: str = "",
        secret: str,
        expires_at: float | None = None,
    ) -> GitCredential:
        """同 host 已存在则覆盖密钥（管理员「重新注入凭据」的语义），否则新建。"""
        host = (host or "").strip().lower()
        username = (username or "").strip()
        row = self._conn.execute(
            f"{self._SELECT} WHERE scope = ? AND owner_id = ? AND host = ? AND scheme = ? "
            "AND username = ?",
            (scope, owner_id, host, scheme, username),
        ).fetchone()
        if row is None:
            return self.create(
                scope=scope,
                owner_id=owner_id,
                label=label,
                host=host,
                scheme=scheme,
                username=username,
                secret=secret,
                expires_at=expires_at,
            )
        credential = self._row_to_credential(row)
        cipher = self._fernet().encrypt(secret.encode("utf-8"))
        with self._lock:
            self._conn.execute(
                "UPDATE git_credentials SET secret_cipher = ?, fingerprint = ?, label = ?, "
                "expires_at = ?, disabled_at = NULL WHERE id = ?",
                (
                    cipher,
                    _fingerprint(secret),
                    (label or credential.label).strip(),
                    expires_at,
                    credential.id,
                ),
            )
            self._conn.commit()
        logger.info("git 凭据已覆盖: id=%s host=%s scope=%s", credential.id, host, scope)
        return self.get(credential.id) or credential

    def disable(self, cred_id: str) -> bool:
        """禁用（安全处置）：保留记录，解析时跳过。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE git_credentials SET disabled_at = ? WHERE id = ? AND disabled_at IS NULL",
                (time.time(), cred_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete(self, cred_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM git_credentials WHERE id = ?", (cred_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def touch(self, cred_id: str) -> None:
        """记录最近使用时间（审计用，失败不影响主流程）。"""
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE git_credentials SET last_used_at = ? WHERE id = ?",
                    (time.time(), cred_id),
                )
                self._conn.commit()
        except sqlite3.Error:  # pragma: no cover - 审计字段写失败不阻断执行
            logger.debug("更新凭据 last_used_at 失败: %s", cred_id, exc_info=True)

    # --- 读 ---------------------------------------------------------------

    def get(self, cred_id: str) -> GitCredential | None:
        row = self._conn.execute(f"{self._SELECT} WHERE id = ?", (cred_id,)).fetchone()
        return self._row_to_credential(row) if row else None

    def list_for_owner(self, scope: str, owner_id: str) -> list[GitCredential]:
        rows = self._conn.execute(
            f"{self._SELECT} WHERE scope = ? AND owner_id = ? ORDER BY created_at DESC",
            (scope, owner_id),
        ).fetchall()
        return [self._row_to_credential(r) for r in rows]

    def find_for_host(
        self, *, owner_id: str, host: str, scheme: str = "https"
    ) -> GitCredential | None:
        """按 host 找该用户当前可用的个人凭据（同 host 多条时取最新）。"""
        rows = self._conn.execute(
            f"{self._SELECT} WHERE scope = ? AND owner_id = ? AND host = ? AND scheme = ? "
            "AND disabled_at IS NULL ORDER BY created_at DESC",
            (SCOPE_USER, owner_id, (host or "").strip().lower(), scheme),
        ).fetchall()
        for row in rows:
            credential = self._row_to_credential(row)
            if credential.is_active():
                return credential
        return None

    def decrypt(self, cred_id: str) -> str:
        """取回明文（仅供执行器注入子进程 env，禁止用于任何响应体）。"""
        row = self._conn.execute(
            "SELECT secret_cipher FROM git_credentials WHERE id = ?", (cred_id,)
        ).fetchone()
        if not row:
            raise CredentialMissing(f"凭据不存在: {cred_id}")
        try:
            return self._fernet().decrypt(row[0]).decode("utf-8")
        except InvalidToken as exc:
            raise GitCredentialError(
                f"凭据解密失败（TDT_GIT_SECRET_KEY 是否已更换？）: {cred_id}"
            ) from exc


class GitRepoError(GitCredentialError):
    """仓库注册表错误基类。"""


class GitRepoConflict(GitRepoError):
    """同一用户重复登记同一仓库 URL。"""


class WorkspaceRepo(BaseModel):
    """工作区仓库条目（全员可见，凭据只存引用 id）。"""

    id: str
    name: str
    url: str
    default_branch: str
    role_slots: list[str] = []
    credential_id: str
    enabled: bool = True
    created_by: str
    created_at: float
    updated_at: float

    def to_public(self) -> dict[str, Any]:
        """对外视图：给所有登录用户看，不含任何凭据内容。"""
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "default_branch": self.default_branch,
            "role_slots": list(self.role_slots),
            "credential_id": self.credential_id,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class UserRepo(BaseModel):
    """个人登记的私有仓库（仅本人可见，凭据按 host 另外匹配）。"""

    id: str
    owner_id: str
    name: str
    url: str
    default_branch: str = ""
    created_at: float

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "default_branch": self.default_branch,
            "created_at": self.created_at,
        }


class WorkspaceRepoStore:
    """工作区仓库表：管理员维护，所有登录用户只读可选。"""

    _UPDATABLE = {"name", "url", "default_branch", "role_slots", "credential_id", "enabled"}

    def __init__(self, path: Path, *, credentials: GitCredentialStore) -> None:
        self._creds = credentials
        self._lock = threading.RLock()
        self._conn = _connect(Path(path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_repos (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                default_branch TEXT NOT NULL DEFAULT '',
                role_slots TEXT NOT NULL DEFAULT '[]',
                credential_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    _SELECT = (
        "SELECT id, name, url, default_branch, role_slots, credential_id, enabled, "
        "created_by, created_at, updated_at FROM workspace_repos"
    )

    @staticmethod
    def _row(row: tuple[Any, ...]) -> WorkspaceRepo:
        import json

        return WorkspaceRepo(
            id=row[0],
            name=row[1],
            url=row[2],
            default_branch=row[3] or "",
            role_slots=json.loads(row[4] or "[]"),
            credential_id=row[5],
            enabled=bool(row[6]),
            created_by=row[7],
            created_at=row[8],
            updated_at=row[9],
        )

    def create(
        self,
        *,
        name: str,
        url: str,
        default_branch: str,
        role_slots: list[str],
        credential_id: str,
        created_by: str,
    ) -> WorkspaceRepo:
        if not (url or "").strip():
            raise GitRepoError("仓库 URL 不能为空")
        if self._creds.get(credential_id) is None:
            raise GitRepoError(f"凭据不存在: {credential_id}")
        import json

        now = time.time()
        repo = WorkspaceRepo(
            id=new_id("wrp_"),
            name=(name or url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")).strip(),
            url=url.strip(),
            default_branch=(default_branch or "").strip(),
            role_slots=list(role_slots or []),
            credential_id=credential_id,
            enabled=True,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO workspace_repos "
                "(id, name, url, default_branch, role_slots, credential_id, enabled, "
                " created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    repo.id,
                    repo.name,
                    repo.url,
                    repo.default_branch,
                    json.dumps(repo.role_slots, ensure_ascii=False),
                    repo.credential_id,
                    repo.created_by,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        logger.info("工作区仓库已创建: id=%s name=%s", repo.id, repo.name)
        return repo

    def get(self, repo_id: str) -> WorkspaceRepo | None:
        row = self._conn.execute(f"{self._SELECT} WHERE id = ?", (repo_id,)).fetchone()
        return self._row(row) if row else None

    def list_all(self) -> list[WorkspaceRepo]:
        rows = self._conn.execute(f"{self._SELECT} ORDER BY created_at DESC").fetchall()
        return [self._row(r) for r in rows]

    def list_enabled(self) -> list[WorkspaceRepo]:
        rows = self._conn.execute(
            f"{self._SELECT} WHERE enabled = 1 ORDER BY created_at DESC"
        ).fetchall()
        return [self._row(r) for r in rows]

    def update(self, repo_id: str, **fields: Any) -> WorkspaceRepo | None:
        payload = {k: v for k, v in fields.items() if k in self._UPDATABLE}
        if not payload:
            return self.get(repo_id)
        if "credential_id" in payload and self._creds.get(payload["credential_id"]) is None:
            raise GitRepoError(f"凭据不存在: {payload['credential_id']}")
        if "role_slots" in payload:
            import json

            payload["role_slots"] = json.dumps(list(payload["role_slots"] or []), ensure_ascii=False)
        if "enabled" in payload:
            payload["enabled"] = 1 if payload["enabled"] else 0
        sets = ", ".join(f"{k} = ?" for k in payload)
        with self._lock:
            self._conn.execute(
                f"UPDATE workspace_repos SET {sets}, updated_at = ? WHERE id = ?",
                (*payload.values(), time.time(), repo_id),
            )
            self._conn.commit()
        return self.get(repo_id)

    def delete(self, repo_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM workspace_repos WHERE id = ?", (repo_id,))
            self._conn.commit()
        return cur.rowcount > 0


class UserRepoStore:
    """个人仓库登记表：只有本人可见可改。"""

    def __init__(self, path: Path) -> None:
        self._lock = threading.RLock()
        self._conn = _connect(Path(path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_git_repos (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                default_branch TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_repo ON user_git_repos(owner_id, url)"
        )
        self._conn.commit()

    _SELECT = "SELECT id, owner_id, name, url, default_branch, created_at FROM user_git_repos"

    @staticmethod
    def _row(row: tuple[Any, ...]) -> UserRepo:
        return UserRepo(
            id=row[0],
            owner_id=row[1],
            name=row[2],
            url=row[3],
            default_branch=row[4] or "",
            created_at=row[5],
        )

    def create(
        self, *, owner_id: str, name: str, url: str, default_branch: str = ""
    ) -> UserRepo:
        url = (url or "").strip()
        if not url:
            raise GitRepoError("仓库 URL 不能为空")
        repo = UserRepo(
            id=new_id("urp_"),
            owner_id=owner_id,
            name=(name or url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")).strip(),
            url=url,
            default_branch=(default_branch or "").strip(),
            created_at=time.time(),
        )
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO user_git_repos "
                    "(id, owner_id, name, url, default_branch, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        repo.id,
                        repo.owner_id,
                        repo.name,
                        repo.url,
                        repo.default_branch,
                        repo.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise GitRepoConflict(f"该仓库已登记: {url}") from exc
            self._conn.commit()
        return repo

    def list_for_owner(self, owner_id: str) -> list[UserRepo]:
        rows = self._conn.execute(
            f"{self._SELECT} WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def get_for_owner(self, owner_id: str, repo_id: str) -> UserRepo | None:
        row = self._conn.execute(
            f"{self._SELECT} WHERE owner_id = ? AND id = ?", (owner_id, repo_id)
        ).fetchone()
        return self._row(row) if row else None

    def delete(self, repo_id: str, *, owner_id: str | None = None) -> bool:
        """删除登记；传 owner_id 时校验归属（他人删除返回 False）。"""
        sql = "DELETE FROM user_git_repos WHERE id = ?"
        params: list[Any] = [repo_id]
        if owner_id is not None:
            sql += " AND owner_id = ?"
            params.append(owner_id)
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
        return cur.rowcount > 0


def host_of(url: str) -> str:
    """从 clone URL 解析 host，支持 https://host/... 与 git@host:group/repo.git。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        return (urlsplit(raw).hostname or "").lower()
    match = re.match(r"^(?:[^@/]+@)?([^:/]+):", raw)
    return match.group(1).lower() if match else ""


def scheme_of(url: str) -> str:
    """按 URL 形式判断协议：https 前缀 → https；其余（scp 形式）→ ssh。"""
    raw = (url or "").strip()
    if "://" in raw:
        return (urlsplit(raw).scheme or "https").lower()
    return "ssh"


@dataclass
class ResolvedCredential:
    """解析结果：只在执行器内存里流通，禁止进入任何响应体或日志。"""

    credential_id: str
    host: str
    scheme: str
    username: str
    secret: str

    def to_public(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "host": self.host,
            "scheme": self.scheme,
            "username": self.username,
        }


class GitCredentialResolver:
    """按「仓库来源」解析凭据（§4.2，不存在优先级回退）。"""

    def __init__(
        self, *, credentials: GitCredentialStore, workspace_repos: WorkspaceRepoStore
    ) -> None:
        self._creds = credentials
        self._repos = workspace_repos

    def resolve(self, repo: Any, *, actor_user_id: str) -> ResolvedCredential:
        """workspace → 条目绑定的凭据；custom → 操作人按 host 的个人凭据。"""
        source = getattr(repo, "source", "custom") or "custom"
        if source == "workspace":
            credential_id = getattr(repo, "credential_id", None)
            if not credential_id:
                raise CredentialMissing(
                    f"工作区仓库缺少凭据绑定: {getattr(repo, 'url', '')}"
                )
            credential = self._creds.get(credential_id)
            if credential is None or not credential.is_active():
                raise CredentialMissing(f"工作区仓库凭据不可用: {credential_id}")
        else:
            host = host_of(getattr(repo, "url", ""))
            scheme = scheme_of(getattr(repo, "url", ""))
            if not host:
                raise CredentialMissing(f"无法从 URL 解析 host: {getattr(repo, 'url', '')}")
            credential = self._creds.find_for_host(
                owner_id=actor_user_id, host=host, scheme=scheme
            )
            if credential is None:
                raise CredentialMissing(
                    f"未找到 {host} 的个人 git 凭据，请先在「设置 → Git 凭据」中配置"
                )

        secret = self._creds.decrypt(credential.id)
        self._creds.touch(credential.id)
        return ResolvedCredential(
            credential_id=credential.id,
            host=credential.host,
            scheme=credential.scheme,
            username=credential.username,
            secret=secret,
        )


# askpass 脚本只从环境变量取值：密钥不出现在命令行、也不写进脚本文件
_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  *[Uu]sername*) printf '%s\\n' "$SORTIE_GIT_USERNAME" ;;
  *) printf '%s\\n' "$SORTIE_GIT_PASSWORD" ;;
esac
"""


class GitEnv:
    """一次 git 子进程调用的凭据环境；退出上下文即删除临时文件。"""

    def __init__(self, env: dict[str, str], tmpdir: Path) -> None:
        self.env = env
        self._tmpdir = tmpdir

    def __enter__(self) -> GitEnv:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        return False


def build_askpass_env(
    *, username: str, secret: str, workdir: Path | None = None
) -> GitEnv:
    """生成 HTTPS 凭据注入环境：GIT_ASKPASS 脚本 + 环境变量承载密钥。

    - 脚本权限 0700，放在随机临时目录，内容不含密钥；
    - 密钥只通过 ``SORTIE_GIT_PASSWORD`` 传给子进程；
    - ``GIT_TERMINAL_PROMPT=0`` 保证无 TTY 时直接失败而不是挂起等待输入。
    """
    tmpdir = Path(
        tempfile.mkdtemp(prefix="sortie-git-", dir=str(workdir) if workdir else None)
    )
    script = tmpdir / "askpass.sh"
    script.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
    os.chmod(script, 0o700)
    env = {
        "GIT_ASKPASS": str(script),
        "GIT_TERMINAL_PROMPT": "0",
        "SORTIE_GIT_USERNAME": username or "",
        "SORTIE_GIT_PASSWORD": secret,
    }
    return GitEnv(env, tmpdir)


def _classify_git_error(text: str) -> str:
    """把 git 的 stderr 归类成可读原因（鉴权 / 网络 / 不存在），原始片段截断保留。"""
    low = (text or "").lower()
    snippet = (text or "").strip()[:200]
    if any(
        key in low
        for key in (
            "authentication failed",
            "could not read username",
            "invalid username or password",
            "permission denied (publickey)",
            "403",
            "401",
        )
    ):
        return f"鉴权失败（检查凭据权限）：{snippet}"
    if any(
        key in low
        for key in (
            "could not resolve host",
            "connection timed out",
            "network is unreachable",
            "connection refused",
            "unable to access",
        )
    ):
        return f"网络不可达：{snippet}"
    if any(
        key in low
        for key in ("repository not found", "does not appear to be a git repository", "not found")
    ):
        return f"仓库不存在或无访问权限：{snippet}"
    return f"git 探活失败：{snippet}"


async def preflight_git_access(
    repo: Any,
    *,
    resolver: GitCredentialResolver | None = None,
    actor_user_id: str = "",
    timeout: float | None = None,
) -> tuple[bool, str]:
    """`git ls-remote` 探活：返回 (是否可达, 可读失败原因)。

    绑定仓库时先探一次，避免凭据错/网络不通拖到 coding 阶段才暴露。
    git 不可用时返回 True（跳过预检，不阻断绑定）。
    """
    import asyncio

    from sortie_deck.settings import settings

    if shutil.which("git") is None:
        return True, "git 不可用，跳过预检"
    if timeout is None:
        timeout = float(getattr(settings, "git_preflight_timeout", 10.0) or 10.0)

    git_ctx = None
    extra_env: dict[str, str] = {}
    if resolver is not None:
        try:
            resolved = resolver.resolve(repo, actor_user_id=actor_user_id)
        except CredentialMissing as exc:
            return False, f"缺少凭据：{exc}"
        if resolved.scheme == "https":
            git_ctx = build_askpass_env(username=resolved.username, secret=resolved.secret)
            extra_env = git_ctx.env
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            "--heads",
            getattr(repo, "url", ""),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **extra_env},
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return False, f"网络不可达（ls-remote 超过 {timeout:.0f}s 未响应）"
    finally:
        if git_ctx is not None:
            git_ctx.__exit__()
    if (proc.returncode or 0) == 0:
        return True, ""
    text = (stderr_b or stdout_b).decode("utf-8", errors="replace")
    return False, _classify_git_error(text)


def store_from_settings(cfg: Any | None = None) -> GitCredentialStore:
    """按全局配置构造存储（api / worker 都从 TDT_DATA_DIR 取同一个库文件）。"""
    from sortie_deck.settings import settings

    config = cfg or settings
    path = Path(
        getattr(config, "git_registry_path", None)
        or Path(config.data_dir) / "git_registry.sqlite"
    )
    return GitCredentialStore(path, secret_key=getattr(config, "git_secret_key", None))


def stores_from_settings(
    cfg: Any | None = None,
) -> tuple[GitCredentialStore, WorkspaceRepoStore, UserRepoStore]:
    """一次构造三张表的 store（共用同一个 sqlite 文件）。"""
    creds = store_from_settings(cfg)
    return creds, WorkspaceRepoStore(creds.path, credentials=creds), UserRepoStore(creds.path)


def resolver_from_settings(cfg: Any | None = None) -> GitCredentialResolver:
    """执行链路用的解析器（clone / push 前按仓库来源取凭据）。"""
    creds, workspace_repos, _ = stores_from_settings(cfg)
    return GitCredentialResolver(credentials=creds, workspace_repos=workspace_repos)


__all__ = [
    "SCOPE_USER",
    "SCOPE_WORKSPACE",
    "CredentialMissing",
    "GitCredential",
    "GitCredentialConflict",
    "GitCredentialError",
    "GitCredentialStore",
    "GitRepoConflict",
    "GitRepoError",
    "SecretKeyMissing",
    "UserRepo",
    "UserRepoStore",
    "WorkspaceRepo",
    "WorkspaceRepoStore",
    "new_secret_key",
    "store_from_settings",
]
