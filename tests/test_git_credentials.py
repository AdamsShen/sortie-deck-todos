"""两级 Git 凭据存储：加密落盘、脱敏视图、scope 隔离与冲突。

覆盖设计文档 §4.4 / §5：
- 明文只在写入时经过一次，库里只存密文；
- 对外视图（to_public）永不包含 secret；
- 同一 (scope, owner, host, scheme, username) 唯一，避免解析歧义。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sortie_deck.git_cred import (
    SCOPE_USER,
    SCOPE_WORKSPACE,
    GitCredentialConflict,
    GitCredentialStore,
    SecretKeyMissing,
    new_secret_key,
)

TOKEN = "glpat-abcdefghijklmnop1234"


def _store(tmp_path: Path, key: str | None = None) -> GitCredentialStore:
    return GitCredentialStore(
        tmp_path / "git_registry.sqlite",
        secret_key=key if key is not None else new_secret_key(),
    )


def test_encrypt_roundtrip(tmp_path: Path):
    store = _store(tmp_path)
    cred = store.create(
        scope=SCOPE_USER,
        owner_id="usr_1",
        label="个人 GitLab",
        host="gitlab.example.com",
        scheme="https",
        username="alice",
        secret=TOKEN,
    )

    assert store.decrypt(cred.id) == TOKEN
    # 落盘内容不得出现明文（库文件 + WAL 一起检查）
    for suffix in ("", "-wal", "-shm"):
        db_file = tmp_path / f"git_registry.sqlite{suffix}"
        if db_file.exists():
            assert TOKEN.encode() not in db_file.read_bytes()


def test_missing_secret_key_refuses_write(tmp_path: Path):
    store = GitCredentialStore(tmp_path / "git_registry.sqlite", secret_key=None)
    with pytest.raises(SecretKeyMissing):
        store.create(
            scope=SCOPE_WORKSPACE,
            owner_id="workspace",
            label="服务账号",
            host="gitlab.example.com",
            scheme="https",
            username="",
            secret=TOKEN,
        )
    assert store.list_for_owner(SCOPE_WORKSPACE, "workspace") == []


def test_fingerprint_masks_secret(tmp_path: Path):
    store = _store(tmp_path)
    cred = store.create(
        scope=SCOPE_USER,
        owner_id="usr_1",
        label="token",
        host="github.com",
        scheme="https",
        username="",
        secret=TOKEN,
    )

    assert TOKEN not in cred.fingerprint
    assert cred.fingerprint.startswith("…")
    assert len(cred.fingerprint) <= 8


def test_list_never_returns_secret(tmp_path: Path):
    store = _store(tmp_path)
    store.create(
        scope=SCOPE_USER,
        owner_id="usr_1",
        label="token",
        host="github.com",
        scheme="https",
        username="",
        secret=TOKEN,
    )

    rows = store.list_for_owner(SCOPE_USER, "usr_1")
    assert len(rows) == 1
    payload = json.dumps([r.to_public() for r in rows], ensure_ascii=False)
    assert TOKEN not in payload
    assert "secret" not in payload


def test_unique_host_constraint(tmp_path: Path):
    store = _store(tmp_path)
    kwargs = {
        "scope": SCOPE_WORKSPACE,
        "owner_id": "workspace",
        "label": "服务账号",
        "host": "gitlab.example.com",
        "scheme": "https",
        "username": "svc",
    }
    store.create(**kwargs, secret=TOKEN)
    with pytest.raises(GitCredentialConflict):
        store.create(**kwargs, secret=TOKEN + "x")


def test_scope_isolation(tmp_path: Path):
    store = _store(tmp_path)
    workspace_cred = store.create(
        scope=SCOPE_WORKSPACE,
        owner_id="workspace",
        label="服务账号",
        host="gitlab.example.com",
        scheme="https",
        username="svc",
        secret="svc-token",
    )
    user_cred = store.create(
        scope=SCOPE_USER,
        owner_id="usr_1",
        label="我的凭据",
        host="gitlab.example.com",
        scheme="https",
        username="alice",
        secret="alice-token",
    )

    assert workspace_cred.id != user_cred.id
    found = store.find_for_host(owner_id="usr_1", host="gitlab.example.com", scheme="https")
    assert found is not None
    assert found.id == user_cred.id
    # 个人解析不会串到工作区凭据
    assert store.find_for_host(owner_id="usr_2", host="gitlab.example.com", scheme="https") is None


def test_disabled_credential_not_resolved(tmp_path: Path):
    store = _store(tmp_path)
    cred = store.create(
        scope=SCOPE_USER,
        owner_id="usr_1",
        label="token",
        host="github.com",
        scheme="https",
        username="",
        secret=TOKEN,
    )

    assert store.find_for_host(owner_id="usr_1", host="github.com", scheme="https") is not None
    assert store.disable(cred.id) is True
    assert store.find_for_host(owner_id="usr_1", host="github.com", scheme="https") is None
    assert store.get(cred.id).is_active() is False
