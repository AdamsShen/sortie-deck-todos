"""IM 接入记录：加密存储、脱敏、状态机、工作区级唯一性。"""

from __future__ import annotations

import pytest

from sortie_deck.git_cred import SecretKeyMissing, new_secret_key
from sortie_deck.im import (
    ImConnectionConflict,
    ImConnectionError,
    ImConnectionManager,
    ImConnectionNotFound,
    ImConnectionStore,
    InvalidTransition,
)


@pytest.fixture()
def key() -> str:
    return new_secret_key()


@pytest.fixture()
def store(tmp_path, key) -> ImConnectionStore:
    return ImConnectionStore(tmp_path / "im.sqlite", secret_key=key)


def test_create_requires_master_key(tmp_path):
    """未配置主密钥必须拒绝写入，绝不降级明文（与 git 凭据同一约定）。"""
    s = ImConnectionStore(tmp_path / "im.sqlite", secret_key=None)
    with pytest.raises(SecretKeyMissing):
        s.create(provider="feishu", mode="stream", app_id="cli_x", secret="s3cr3t")


def test_secret_is_encrypted_and_never_public(store):
    conn = store.create(
        provider="feishu",
        mode="stream",
        app_id="cli_a92360a836b89bb4",
        secret="very-secret-value",
        created_by="admin",
    )
    assert conn.has_secret is True
    assert conn.secret_fingerprint
    # 明文可解出来（仅建连时用）
    assert store.decrypt_secret(conn.id) == "very-secret-value"
    # 对外视图不能出现明文，也不能出现密文
    public = conn.to_public()
    assert "very-secret-value" not in str(public)
    assert "secret_cipher" not in public
    assert public["app_id"] == "cli_a92360a836b89bb4"


def test_secret_fingerprint_is_stable_and_not_reversible(store):
    a = store.create(provider="dingtalk", mode="stream", app_id="k1", secret="abc")
    b = store.create(provider="wecom", mode="stream", app_id="k2", secret="abc")
    # 同一个 secret 指纹一致，便于识别"两个渠道填了同一把密钥"
    assert a.secret_fingerprint == b.secret_fingerprint
    assert "abc" not in a.secret_fingerprint


def test_workspace_level_uniqueness(store):
    """工作区级绑定：一个渠道只允许一条连接。"""
    store.create(provider="feishu", mode="stream", app_id="cli_1", secret="s")
    with pytest.raises(ImConnectionConflict):
        store.create(provider="feishu", mode="webhook", app_id="cli_2", secret="s")


def test_mode_validation_per_provider(store):
    """微信个人号只能扫码；不能用长连接。"""
    with pytest.raises(ImConnectionError):
        store.create(provider="wechat", mode="stream")
    conn = store.create(provider="wechat", mode="qr")
    assert conn.mode == "qr"
    assert conn.has_secret is False  # 扫码成功后才回填 token


def test_unknown_provider_rejected(store):
    with pytest.raises(ImConnectionError):
        store.create(provider="telegram", mode="stream")


def test_status_transitions(store):
    conn = store.create(provider="feishu", mode="stream", app_id="cli", secret="s")
    assert conn.status == "unconfigured"

    assert store.set_status(conn.id, "connecting").status == "connecting"
    assert store.set_status(conn.id, "connected").status == "connected"
    assert store.get(conn.id).last_connected_at is not None

    # 连接态不能直接回到 unconfigured（必须显式 disconnect/删除）
    with pytest.raises(InvalidTransition):
        store.set_status(conn.id, "unconfigured")

    assert store.set_status(conn.id, "disabled").status == "disabled"


def test_status_is_idempotent(store):
    conn = store.create(provider="feishu", mode="stream", app_id="cli", secret="s")
    store.set_status(conn.id, "connecting")
    # 重复置同一状态不该抛错（重连风暴下会重复上报）
    assert store.set_status(conn.id, "connecting").status == "connecting"


def test_error_records_provider_message(store):
    conn = store.create(provider="feishu", mode="stream", app_id="cli", secret="bad")
    store.set_status(conn.id, "connecting")
    updated = store.set_status(conn.id, "error", error="invalid app_secret (code=10014)")
    assert updated.status == "error"
    # 平台原始错误要能直接展示给用户，否则排障只能靠猜
    assert "10014" in updated.last_error


def test_update_secret_rotates_fingerprint(store):
    conn = store.create(provider="slack", mode="webhook", app_id="app", secret="old")
    old_fp = conn.secret_fingerprint
    updated = store.update(conn.id, secret="new")
    assert updated.secret_fingerprint != old_fp
    assert store.decrypt_secret(conn.id) == "new"


def test_touch_event_and_health_hint(store):
    conn = store.create(provider="feishu", mode="stream", app_id="cli", secret="s")
    store.set_status(conn.id, "connecting")
    store.set_status(conn.id, "connected")
    store.touch_event(conn.id)

    manager = ImConnectionManager(store=store, providers={})
    health = manager.health(conn.id)
    assert health["events_seen_at"] is not None
    assert health["hint"] == ""


def test_health_flags_stale_connected(store):
    """连上了但长时间没有事件：提示订阅方式可能不匹配。"""
    conn = store.create(provider="feishu", mode="stream", app_id="cli", secret="s")
    store.set_status(conn.id, "connecting")
    store.set_status(conn.id, "connected")

    manager = ImConnectionManager(store=store, providers={})
    health = manager.health(conn.id)
    assert "订阅方式" in health["hint"]


def test_delete(store):
    conn = store.create(provider="wecom", mode="stream", app_id="bot", secret="s")
    store.delete(conn.id)
    assert store.list() == []
    with pytest.raises(ImConnectionNotFound):
        store.get(conn.id)
