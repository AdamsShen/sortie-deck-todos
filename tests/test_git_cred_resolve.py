"""凭据解析与 askpass 注入：来源决定凭据、密钥不落命令行。

对应设计文档 §4.2 / §4.5。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sortie_deck.git_cred import (
    SCOPE_USER,
    SCOPE_WORKSPACE,
    CredentialMissing,
    GitCredentialResolver,
    GitCredentialStore,
    WorkspaceRepoStore,
    build_askpass_env,
    host_of,
    new_secret_key,
    scheme_of,
)
from sortie_deck.models import RepoConfig

SVC_TOKEN = "glpat-service-token-0001"
USER_TOKEN = "glpat-user-token-0002"


def _setup(tmp_path: Path) -> tuple[GitCredentialStore, WorkspaceRepoStore, GitCredentialResolver]:
    db = tmp_path / "git_registry.sqlite"
    creds = GitCredentialStore(db, secret_key=new_secret_key())
    ws = WorkspaceRepoStore(db, credentials=creds)
    return creds, ws, GitCredentialResolver(credentials=creds, workspace_repos=ws)


def _workspace_repo(creds: GitCredentialStore, ws: WorkspaceRepoStore, host: str) -> str:
    cred = creds.create(
        scope=SCOPE_WORKSPACE,
        owner_id="workspace",
        label="服务账号",
        host=host,
        scheme="https",
        username="svc",
        secret=SVC_TOKEN,
    )
    ws.create(
        name="platform-server",
        url=f"https://{host}/group/platform-server.git",
        default_branch="main",
        role_slots=["eng_backend"],
        credential_id=cred.id,
        created_by="usr_admin",
    )
    # RepoConfig.credential_id 指向凭据本身，不是仓库条目 id
    return cred.id


def _user_cred(creds: GitCredentialStore, owner: str, host: str) -> None:
    creds.create(
        scope=SCOPE_USER,
        owner_id=owner,
        label="我的凭据",
        host=host,
        scheme="https",
        username="alice",
        secret=USER_TOKEN,
    )


def test_host_and_scheme_parsing():
    assert host_of("https://gitlab.example.com/group/repo.git") == "gitlab.example.com"
    assert scheme_of("https://gitlab.example.com/group/repo.git") == "https"
    assert host_of("git@github.com:org/repo.git") == "github.com"
    assert scheme_of("git@github.com:org/repo.git") == "ssh"


def test_resolve_workspace_source(tmp_path: Path):
    creds, ws, resolver = _setup(tmp_path)
    repo_id = _workspace_repo(creds, ws, "gitlab.example.com")
    # actor 自己也有同 host 的个人凭据，但工作区来源必须用工作区凭据
    _user_cred(creds, "usr_actor", "gitlab.example.com")

    resolved = resolver.resolve(
        RepoConfig(
            url="https://gitlab.example.com/group/platform-server.git",
            branch="main",
            source="workspace",
            credential_id=repo_id,
        ),
        actor_user_id="usr_actor",
    )
    assert resolved.secret == SVC_TOKEN
    assert resolved.username == "svc"


def test_resolve_custom_source(tmp_path: Path):
    creds, _ws, resolver = _setup(tmp_path)
    _user_cred(creds, "usr_actor", "github.com")

    resolved = resolver.resolve(
        RepoConfig(url="https://github.com/alice/my-tools.git", source="custom"),
        actor_user_id="usr_actor",
    )
    assert resolved.secret == USER_TOKEN
    assert resolved.host == "github.com"


def test_resolve_missing_raises(tmp_path: Path):
    _creds, _ws, resolver = _setup(tmp_path)
    with pytest.raises(CredentialMissing) as exc:
        resolver.resolve(
            RepoConfig(url="https://unknown.example.com/x/y.git", source="custom"),
            actor_user_id="usr_actor",
        )
    message = str(exc.value)
    assert "unknown.example.com" in message
    assert USER_TOKEN not in message and SVC_TOKEN not in message


def test_resolve_workspace_without_credential_id(tmp_path: Path):
    _creds, _ws, resolver = _setup(tmp_path)
    with pytest.raises(CredentialMissing):
        resolver.resolve(
            RepoConfig(
                url="https://gitlab.example.com/group/x.git", source="workspace", credential_id=None
            ),
            actor_user_id="usr_actor",
        )


def test_resolved_public_view_has_no_secret(tmp_path: Path):
    creds, ws, resolver = _setup(tmp_path)
    repo_id = _workspace_repo(creds, ws, "gitlab.example.com")
    resolved = resolver.resolve(
        RepoConfig(
            url="https://gitlab.example.com/group/platform-server.git",
            source="workspace",
            credential_id=repo_id,
        ),
        actor_user_id="usr_actor",
    )
    payload = str(resolved.to_public())
    assert SVC_TOKEN not in payload


def test_askpass_script_permissions(tmp_path: Path):
    with build_askpass_env(username="alice", secret=USER_TOKEN, workdir=tmp_path) as git_env:
        script = Path(git_env.env["GIT_ASKPASS"])
        assert script.exists()
        assert (script.stat().st_mode & 0o777) == 0o700
        assert USER_TOKEN not in script.read_text(encoding="utf-8")
        # 密钥只经环境变量传递
        assert git_env.env["SORTIE_GIT_PASSWORD"] == USER_TOKEN


def test_askpass_env_shape(tmp_path: Path):
    with build_askpass_env(username="alice", secret=USER_TOKEN, workdir=tmp_path) as git_env:
        assert git_env.env["GIT_TERMINAL_PROMPT"] == "0"
        assert git_env.env["SORTIE_GIT_USERNAME"] == "alice"
        assert "GIT_ASKPASS" in git_env.env


def test_askpass_cleanup(tmp_path: Path):
    with build_askpass_env(username="alice", secret=USER_TOKEN, workdir=tmp_path) as git_env:
        script = Path(git_env.env["GIT_ASKPASS"])
        assert script.exists()
    assert not script.exists()
    # 临时目录整体删除，不残留 0700 目录
    assert os.listdir(tmp_path) == [] or all(
        not p.startswith("sortie-git-") for p in os.listdir(tmp_path)
    )
