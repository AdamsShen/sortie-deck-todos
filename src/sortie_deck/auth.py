from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from sortie_deck.models import new_id, utc_now

logger = logging.getLogger(__name__)

DEV_TOKEN_SECRET = "tdt-dev-secret"


class UserRole(str):
    ADMIN = "admin"
    MEMBER = "member"


class User(BaseModel):
    id: str = Field(default_factory=lambda: new_id("usr_"))
    username: str
    display_name: str
    role: Literal["admin", "member"] = "member"
    # 账号状态：注册接口创建的用户为 pending，管理员审核后才允许登录；
    # 默认 active 兼容存量 users.json（内置账号无需迁移）
    status: Literal["pending", "active", "rejected"] = "active"
    password_hash: str
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class PublicUser(BaseModel):
    id: str
    username: str
    display_name: str
    role: Literal["admin", "member"]
    status: Literal["pending", "active", "rejected"] = "active"


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    # 自助注册：仅允许创建 member 角色，status 固定 pending，待管理员审核
    username: str
    password: str
    display_name: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str
    role: Literal["admin", "member"] = "member"


class TokenPayload(BaseModel):
    token: str
    user: PublicUser


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _hex = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def _sign_token(raw: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def _unsign_token(token: str, secret: str) -> str | None:
    if "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    expected = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    return raw


class AuthStore:
    def __init__(self, path: Path, token_secret: str = DEV_TOKEN_SECRET) -> None:
        secret = (token_secret or "").strip()
        if not secret:
            raise ValueError("token_secret must not be empty")
        self.path = path
        self.token_secret = secret
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            admin = User(
                username="admin",
                display_name="Admin",
                role="admin",
                password_hash=hash_password("admin123"),
            )
            member = User(
                username="operator",
                display_name="Operator",
                role="member",
                password_hash=hash_password("operator123"),
            )
            self._save({"users": [admin.model_dump(), member.model_dump()], "tokens": {}})

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_users(self) -> list[PublicUser]:
        data = self._load()
        return [
            PublicUser(
                id=u["id"],
                username=u["username"],
                display_name=u["display_name"],
                role=u["role"],
                status=u.get("status", "active"),
            )
            for u in data.get("users", [])
        ]

    def get_by_username(self, username: str) -> User | None:
        data = self._load()
        for raw in data.get("users", []):
            if raw["username"] == username:
                return User.model_validate(raw)
        return None

    def get_by_id(self, user_id: str) -> User | None:
        data = self._load()
        for raw in data.get("users", []):
            if raw["id"] == user_id:
                return User.model_validate(raw)
        return None

    def create_user(self, req: CreateUserRequest) -> PublicUser:
        if self.get_by_username(req.username):
            raise ValueError("username already exists")
        user = User(
            username=req.username,
            display_name=req.display_name,
            role=req.role,
            password_hash=hash_password(req.password),
        )
        data = self._load()
        data.setdefault("users", []).append(user.model_dump())
        self._save(data)
        return PublicUser(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            status=user.status,
        )

    def register(self, req: RegisterRequest) -> PublicUser:
        """自助注册：只创建 pending 状态的 member 账号，需管理员审核通过后才能登录。

        输入：RegisterRequest(username, password, display_name)；
        输出：PublicUser（status=pending）；用户名重复抛 ValueError。
        """
        if self.get_by_username(req.username):
            raise ValueError("username already exists")
        user = User(
            username=req.username,
            display_name=req.display_name,
            role="member",
            status="pending",
            password_hash=hash_password(req.password),
        )
        data = self._load()
        data.setdefault("users", []).append(user.model_dump())
        self._save(data)
        logger.info("新用户注册（待审核）: username=%s id=%s", req.username, user.id)
        return PublicUser(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            status=user.status,
        )

    def set_status(self, user_id: str, status: Literal["active", "rejected"]) -> PublicUser:
        """管理员审核：pending → active（通过）或 rejected（拒绝）；也支持重新激活。"""
        data = self._load()
        target = next((u for u in data.get("users", []) if u["id"] == user_id), None)
        if not target:
            raise KeyError(user_id)
        target["status"] = status
        if status == "rejected":
            # 拒绝即失效：清除该用户全部登录 token
            self._purge_tokens(data, user_id)
        self._save(data)
        logger.info("账号审核状态变更: user=%s status=%s", user_id, status)
        user = User.model_validate(target)
        return PublicUser(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            status=user.status,
        )

    def update_role(self, user_id: str, role: Literal["admin", "member"]) -> PublicUser:
        """管理员修改用户角色；系统至少保留一个 admin，降级最后一个 admin 会被拒绝。"""
        data = self._load()
        users = data.get("users", [])
        target = next((u for u in users if u["id"] == user_id), None)
        if not target:
            raise KeyError(user_id)
        if target["role"] == "admin" and role == "member":
            admins = [u for u in users if u["role"] == "admin"]
            if len(admins) <= 1:
                raise ValueError("cannot demote the last admin")
        target["role"] = role
        self._save(data)
        logger.info("用户角色变更: user=%s role=%s", user_id, role)
        user = User.model_validate(target)
        return PublicUser(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            status=user.status,
        )

    def delete_self(self, user_id: str) -> None:
        """账号注销（用户自删）：删除账号并清除其全部登录 token；最后一个 admin 不可注销。"""
        data = self._load()
        users = data.get("users", [])
        target = next((u for u in users if u["id"] == user_id), None)
        if not target:
            raise KeyError(user_id)
        if target["role"] == "admin":
            admins = [u for u in users if u["role"] == "admin"]
            if len(admins) <= 1:
                raise ValueError("cannot delete the last admin")
        data["users"] = [u for u in users if u["id"] != user_id]
        self._purge_tokens(data, user_id)
        self._save(data)
        logger.info("用户已注销账号: user=%s username=%s", user_id, target.get("username"))

    def _purge_tokens(self, data: dict, user_id: str) -> None:
        # 删除用户时同步清除其全部登录 token，避免已删除账号继续持有有效会话
        tokens = data.get("tokens", {})
        data["tokens"] = {k: v for k, v in tokens.items() if v.get("user_id") != user_id}

    def delete_user(self, user_id: str) -> None:
        data = self._load()
        users = data.get("users", [])
        target = next((u for u in users if u["id"] == user_id), None)
        if not target:
            raise KeyError(user_id)
        if target["role"] == "admin":
            admins = [u for u in users if u["role"] == "admin"]
            if len(admins) <= 1:
                raise ValueError("cannot delete the last admin")
        data["users"] = [u for u in users if u["id"] != user_id]
        self._purge_tokens(data, user_id)
        self._save(data)

    def login(self, username: str, password: str) -> TokenPayload:
        user = self.get_by_username(username)
        if not user or not verify_password(password, user.password_hash):
            raise PermissionError("invalid credentials")
        if user.status != "active":
            # 密码正确但账号未激活：区分待审核 / 已拒绝，前端展示不同提示
            raise PermissionError(f"account {user.status}")
        raw = secrets.token_urlsafe(24)
        data = self._load()
        data.setdefault("tokens", {})[raw] = {
            "user_id": user.id,
            "exp": time.time() + 60 * 60 * 24 * 7,
        }
        self._save(data)
        return TokenPayload(
            token=_sign_token(raw, self.token_secret),
            user=PublicUser(
                id=user.id,
                username=user.username,
                display_name=user.display_name,
                role=user.role,
                status=user.status,
            ),
        )

    def logout(self, token: str) -> None:
        raw = _unsign_token(token, self.token_secret)
        if not raw:
            return
        data = self._load()
        data.get("tokens", {}).pop(raw, None)
        self._save(data)

    def resolve(self, token: str | None) -> PublicUser | None:
        if not token:
            return None
        raw = _unsign_token(token, self.token_secret)
        if not raw:
            return None
        data = self._load()
        meta = data.get("tokens", {}).get(raw)
        if not meta:
            return None
        if meta.get("exp", 0) < time.time():
            data["tokens"].pop(raw, None)
            self._save(data)
            return None
        user = self.get_by_id(meta["user_id"])
        if not user:
            return None
        if user.status != "active":
            # 账号被拒绝/待审核后，已签发 token 立即失效
            return None
        return PublicUser(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            status=user.status,
        )
