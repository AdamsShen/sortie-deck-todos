"""仓库 ↔ 凭据绑定：host 强校验、repo_id 解析、分支必填。"""

from __future__ import annotations

import sqlite3

import pytest

from sortie_deck.git_cred import (
    SCOPE_USER,
    SCOPE_WORKSPACE,
    GitCredentialError,
    GitCredentialResolver,
    GitCredentialStore,
    UserRepoStore,
    WorkspaceRepoStore,
    new_secret_key,
    validate_repo_credential,
)
from sortie_deck.models import RepoConfig
from sortie_deck.service import ServiceError, _clean_role_repos


@pytest.fixture()
def stores(tmp_path):
    path = tmp_path / "git_registry.sqlite"
    creds = GitCredentialStore(path, secret_key=new_secret_key())
    return creds, WorkspaceRepoStore(path, credentials=creds), UserRepoStore(path)


def _cred(creds, *, scope, owner, host="github.com", scheme="https", user="u", secret="tok-123456"):
    return creds.upsert(
        scope=scope,
        owner_id=owner,
        label=f"{host} cred",
        host=host,
        scheme=scheme,
        username=user,
        secret=secret,
    )


def test_user_repo_table_migrates_old_schema(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE user_git_repos (id TEXT PRIMARY KEY, owner_id TEXT, name TEXT, "
        "url TEXT, default_branch TEXT, created_at REAL)"
    )
    conn.commit()
    conn.close()

    store = UserRepoStore(path)
    cols = {row[1] for row in store._conn.execute("PRAGMA table_info(user_git_repos)")}
    assert "credential_id" in cols


def test_host_mismatch_rejected(stores):
    creds, _, _ = stores
    cred = _cred(creds, scope=SCOPE_USER, owner="u1", host="github.com")
    with pytest.raises(GitCredentialError):
        validate_repo_credential(
            "https://evil.example/x.git", cred.id, credentials=creds, owner_id="u1"
        )


def test_private_repo_can_use_workspace_credential(stores):
    creds, _, _ = stores
    cred = _cred(creds, scope=SCOPE_WORKSPACE, owner="workspace")
    got = validate_repo_credential(
        "https://github.com/org/repo.git", cred.id, credentials=creds, owner_id="u1"
    )
    assert got.id == cred.id


def test_workspace_repo_cannot_use_user_credential(stores):
    creds, _, _ = stores
    cred = _cred(creds, scope=SCOPE_USER, owner="u1")
    with pytest.raises(GitCredentialError):
        validate_repo_credential(
            "https://github.com/org/repo.git",
            cred.id,
            credentials=creds,
            allow_user=False,
        )


def test_resolver_uses_repo_registry_and_current_credential(stores):
    creds, ws_repos, user_repos = stores
    first = _cred(creds, scope=SCOPE_USER, owner="u1", secret="tok-first-1")
    second = _cred(creds, scope=SCOPE_USER, owner="u1", user="u2", secret="tok-second")
    repo = user_repos.create(
        owner_id="u1",
        name="repo",
        url="https://github.com/org/repo.git",
        default_branch="main",
        credential_id=first.id,
    )
    resolver = GitCredentialResolver(
        credentials=creds, workspace_repos=ws_repos, user_repos=user_repos
    )
    got = resolver.resolve(
        RepoConfig(url=repo.url, branch="main", source="custom", repo_id=repo.id),
        actor_user_id="u1",
    )
    assert got.credential_id == first.id

    # 改绑后同一份任务 payload 自动使用新凭据（不再快照）
    user_repos.update(repo.id, owner_id="u1", credential_id=second.id)
    got2 = resolver.resolve(
        RepoConfig(url=repo.url, branch="main", source="custom", repo_id=repo.id),
        actor_user_id="u1",
    )
    assert got2.credential_id == second.id


def test_validate_by_repo_id_uses_registry_binding(stores, monkeypatch):
    from sortie_deck import git_cred as gc
    from sortie_deck.service import _validate_repo_credentials

    creds, _, user_repos = stores
    cred = _cred(creds, scope=SCOPE_USER, owner="u1")
    repo = user_repos.create(
        owner_id="u1",
        name="repo",
        url="https://github.com/org/repo.git",
        default_branch="main",
        credential_id=cred.id,
    )
    monkeypatch.setattr(gc, "stores_from_settings", lambda cfg=None: stores)
    clean = _clean_role_repos(
        {
            "eng": [
                {
                    "url": repo.url,
                    "branch": "main",
                    "source": "custom",
                    "repo_id": repo.id,
                }
            ]
        }
    )
    _validate_repo_credentials(clean, actor_user_id="u1")  # 不抛异常即通过


def test_branch_is_required():
    with pytest.raises(ServiceError):
        _clean_role_repos(
            {
                "eng_web": [
                    {
                        "url": "https://github.com/o/r.git",
                        "branch": "",
                        "repo_id": "urp_1",
                        "source": "custom",
                    }
                ]
            }
        )
