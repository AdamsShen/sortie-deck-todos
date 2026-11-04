"""工作区仓库与个人仓库注册表：CRUD、启用过滤、owner 隔离。

对应设计文档 §5 的 `workspace_repos` / `user_git_repos` 两张表。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortie_deck.git_cred import (
    SCOPE_WORKSPACE,
    GitCredentialError,
    GitCredentialStore,
    GitRepoConflict,
    UserRepoStore,
    WorkspaceRepoStore,
    new_secret_key,
)


def _stores(tmp_path: Path) -> tuple[GitCredentialStore, WorkspaceRepoStore, UserRepoStore]:
    db = tmp_path / "git_registry.sqlite"
    creds = GitCredentialStore(db, secret_key=new_secret_key())
    return creds, WorkspaceRepoStore(db, credentials=creds), UserRepoStore(db)


def _cred(creds: GitCredentialStore, host: str = "gitlab.example.com") -> str:
    return creds.create(
        scope=SCOPE_WORKSPACE,
        owner_id="workspace",
        label="服务账号",
        host=host,
        scheme="https",
        username="svc",
        secret="svc-token",
    ).id


def test_workspace_repo_crud(tmp_path: Path):
    creds, repos, _ = _stores(tmp_path)
    cred_id = _cred(creds)

    repo = repos.create(
        name="platform-server",
        url="https://gitlab.example.com/group/platform-server.git",
        default_branch="main",
        role_slots=["eng_backend"],
        credential_id=cred_id,
        created_by="usr_admin",
    )
    assert repos.get(repo.id) is not None
    assert [r.id for r in repos.list_enabled()] == [repo.id]

    repos.update(repo.id, default_branch="dev", enabled=False)
    assert repos.list_enabled() == []
    assert repos.list_all()[0].default_branch == "dev"

    assert repos.delete(repo.id) is True
    assert repos.get(repo.id) is None


def test_workspace_repo_requires_existing_credential(tmp_path: Path):
    _, repos, _ = _stores(tmp_path)
    with pytest.raises(GitCredentialError):
        repos.create(
            name="bad",
            url="https://gitlab.example.com/group/bad.git",
            default_branch="main",
            role_slots=[],
            credential_id="crd_not_exists",
            created_by="usr_admin",
        )


def test_workspace_repo_public_view_has_no_secret(tmp_path: Path):
    creds, repos, _ = _stores(tmp_path)
    repo = repos.create(
        name="p",
        url="https://gitlab.example.com/group/p.git",
        default_branch="main",
        role_slots=[],
        credential_id=_cred(creds),
        created_by="usr_admin",
    )
    payload = repo.to_public()
    assert "secret" not in str(payload)
    assert payload["credential_id"]


def test_user_repo_owner_isolation(tmp_path: Path):
    _, _, user_repos = _stores(tmp_path)
    repo = user_repos.create(
        owner_id="usr_a",
        name="my-tools",
        url="https://github.com/alice/my-tools.git",
        default_branch="main",
    )

    assert [r.id for r in user_repos.list_for_owner("usr_a")] == [repo.id]
    assert user_repos.list_for_owner("usr_b") == []
    # 他人拿不到详情，也删不掉
    assert user_repos.get_for_owner("usr_b", repo.id) is None
    assert user_repos.delete(repo.id, owner_id="usr_b") is False
    assert user_repos.delete(repo.id, owner_id="usr_a") is True


def test_user_repo_unique_url(tmp_path: Path):
    _, _, user_repos = _stores(tmp_path)
    kwargs = {
        "owner_id": "usr_a",
        "name": "dup",
        "url": "https://github.com/alice/dup.git",
        "default_branch": "main",
    }
    user_repos.create(**kwargs)
    with pytest.raises(GitRepoConflict):
        user_repos.create(**kwargs)
    # 另一个用户可以登记同一个 URL
    user_repos.create(**{**kwargs, "owner_id": "usr_b"})
