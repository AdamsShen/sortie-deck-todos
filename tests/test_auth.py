from __future__ import annotations

import pytest

from sortie_deck.auth import (
    AuthStore,
    CreateUserRequest,
    RegisterRequest,
    hash_password,
    verify_password,
)
from sortie_deck.settings import Settings


def test_password_hash_roundtrip():
    stored = hash_password("s3cret")
    assert verify_password("s3cret", stored)
    assert not verify_password("wrong", stored)


def test_auth_login_and_roles(tmp_path):
    store = AuthStore(tmp_path / "users.json", token_secret="test-secret")
    admin = store.login("admin", "admin123")
    assert admin.user.role == "admin"
    assert "." in admin.token
    member = store.login("operator", "operator123")
    assert member.user.role == "member"
    assert store.resolve(admin.token).username == "admin"
    created = store.create_user(
        CreateUserRequest(username="bob", password="bob123", display_name="Bob", role="member")
    )
    assert created.username == "bob"
    assert any(u.username == "bob" for u in store.list_users())


def test_bad_login_and_forged_token(tmp_path):
    store = AuthStore(tmp_path / "users.json", token_secret="test-secret")
    with pytest.raises(PermissionError):
        store.login("admin", "nope")
    assert store.resolve("not-a-token") is None
    assert store.resolve("raw.deadbeef") is None
    good = store.login("admin", "admin123")
    raw, _sig = good.token.rsplit(".", 1)
    assert store.resolve(f"{raw}.{'0' * 32}") is None
    forged = AuthStore(tmp_path / "users.json", token_secret="other-secret")
    assert forged.resolve(good.token) is None


def test_admin_gate_via_api(tmp_path):
    store = AuthStore(tmp_path / "users.json", token_secret="api-test-secret")
    member = store.login("operator", "operator123")
    admin = store.login("admin", "admin123")
    assert store.resolve(member.token).role == "member"
    assert store.resolve(admin.token).role == "admin"


def test_production_refuses_default_secret():
    s = Settings(env="production", token_secret="tdt-dev-secret")
    with pytest.raises(RuntimeError, match="TDT_TOKEN_SECRET"):
        s.resolved_token_secret()
    ok = Settings(env="production", token_secret="prod-strong-secret")
    assert ok.resolved_token_secret() == "prod-strong-secret"


def test_empty_token_secret_rejected(tmp_path):
    with pytest.raises(ValueError):
        AuthStore(tmp_path / "users.json", token_secret="  ")


def test_register_creates_pending_member(tmp_path):
    store = AuthStore(tmp_path / "users.json", token_secret="test-secret")
    created = store.register(RegisterRequest(username="carol", password="pw123", display_name="Carol"))
    assert created.status == "pending"
    assert created.role == "member"
    # 待审核账号即使密码正确也不允许登录
    with pytest.raises(PermissionError, match="pending"):
        store.login("carol", "pw123")
    with pytest.raises(ValueError, match="exists"):
        store.register(RegisterRequest(username="carol", password="x", display_name="C2"))


def test_admin_approve_and_reject_flow(tmp_path):
    store = AuthStore(tmp_path / "users.json", token_secret="test-secret")
    created = store.register(RegisterRequest(username="dave", password="pw123", display_name="Dave"))
    store.set_status(created.id, "active")
    session = store.login("dave", "pw123")
    assert session.user.status == "active"
    # 拒绝后登录被拒，已签发 token 立即失效
    store.set_status(created.id, "rejected")
    with pytest.raises(PermissionError, match="rejected"):
        store.login("dave", "pw123")
    assert store.resolve(session.token) is None
    # 重新激活后可再次登录
    store.set_status(created.id, "active")
    assert store.login("dave", "pw123").user.status == "active"


def test_update_role_and_last_admin_guard(tmp_path):
    store = AuthStore(tmp_path / "users.json", token_secret="test-secret")
    admin = store.login("admin", "admin123")
    # 唯一 admin 不可降级
    with pytest.raises(ValueError, match="last admin"):
        store.update_role(admin.user.id, "member")
    created = store.create_user(
        CreateUserRequest(username="erin", password="pw123", display_name="Erin", role="member")
    )
    updated = store.update_role(created.id, "admin")
    assert updated.role == "admin"
    # 有了第二个 admin 后，原 admin 可以降级
    demoted = store.update_role(admin.user.id, "member")
    assert demoted.role == "member"


def test_delete_self_purges_tokens(tmp_path):
    store = AuthStore(tmp_path / "users.json", token_secret="test-secret")
    member = store.login("operator", "operator123")
    store.delete_self(member.user.id)
    # 注销后 token 失效、账号记录被删除
    assert store.resolve(member.token) is None
    assert store.get_by_username("operator") is None
    # 唯一 admin 不可注销
    admin = store.login("admin", "admin123")
    with pytest.raises(ValueError, match="last admin"):
        store.delete_self(admin.user.id)
