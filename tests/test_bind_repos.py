"""绑定校验：来源/凭据归属 + ls-remote 预检 + 旧 token 字段失效。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sortie_deck.git_cred import (
    SCOPE_USER,
    SCOPE_WORKSPACE,
    _classify_git_error,
    new_secret_key,
    preflight_git_access,
    stores_from_settings,
)
from sortie_deck.service import ServiceError, _clean_role_repos, _validate_repo_credentials
from sortie_deck.settings import settings


@pytest.fixture()
def git_db(tmp_path: Path, monkeypatch):
    """把凭据库指向临时目录，避免碰到真实 data_dir。"""
    monkeypatch.setattr(settings, "git_registry_path", tmp_path / "git_registry.sqlite")
    monkeypatch.setattr(settings, "git_secret_key", new_secret_key())
    return tmp_path


def test_clean_keeps_source_and_drops_token():
    clean = _clean_role_repos(
        {
            "eng": [
                {
                    "url": "https://gitlab.example.com/group/app.git",
                    "branch": "main",
                    "source": "workspace",
                    "credential_id": "crd_1",
                    "token": "glpat-legacy",
                }
            ]
        }
    )
    repo = clean["eng"][0]
    assert repo.source == "workspace"
    assert repo.credential_id == "crd_1"
    assert repo.token is None  # 旧字段被丢弃，不再进任务 payload


def test_validate_workspace_requires_credential_id(git_db: Path):
    with pytest.raises(ServiceError) as exc:
        _validate_repo_credentials(
            _clean_role_repos(
                {
                    "eng": [
                        {
                            "url": "https://gitlab.example.com/group/app.git",
                            "branch": "main",
                            "source": "workspace",
                        }
                    ]
                }
            ),
            actor_user_id="usr_1",
        )
    assert "绑定凭据" in str(exc.value)


def test_validate_workspace_rejects_unknown_credential(git_db: Path):
    with pytest.raises(ServiceError) as exc:
        _validate_repo_credentials(
            _clean_role_repos(
                {
                    "eng": [
                        {
                            "url": "https://gitlab.example.com/group/app.git",
                            "branch": "main",
                            "source": "workspace",
                            "credential_id": "crd_missing",
                        }
                    ]
                }
            ),
            actor_user_id="usr_1",
        )
    assert "凭据不存在" in str(exc.value)


def test_validate_workspace_accepts_valid_credential(git_db: Path):
    creds, _ws, _user = stores_from_settings()
    cred = creds.create(
        scope=SCOPE_WORKSPACE,
        owner_id="workspace",
        label="svc",
        host="gitlab.example.com",
        scheme="https",
        username="svc",
        secret="svc-token",
    )
    _validate_repo_credentials(
        _clean_role_repos(
            {
                "eng": [
                    {
                        "url": "https://gitlab.example.com/group/app.git",
                        "branch": "main",
                        "source": "workspace",
                        "credential_id": cred.id,
                    }
                ]
            }
        ),
        actor_user_id="usr_1",
    )  # 不抛异常即通过


def test_validate_custom_requires_personal_credential(git_db: Path):
    clean = _clean_role_repos(
        {
            "eng": [
                {
                    "url": "https://github.com/alice/app.git",
                    "branch": "main",
                    "source": "custom",
                }
            ]
        }
    )
    with pytest.raises(ServiceError) as exc:
        _validate_repo_credentials(clean, actor_user_id="usr_1")
    assert "个人 git 凭据" in str(exc.value)

    creds, _ws, _user = stores_from_settings()
    creds.create(
        scope=SCOPE_USER,
        owner_id="usr_1",
        label="mine",
        host="github.com",
        scheme="https",
        username="alice",
        secret="pat-1234567",
    )
    _validate_repo_credentials(clean, actor_user_id="usr_1")  # 配好后通过


async def test_preflight_local_repo_ok(tmp_path: Path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    ok, reason = await preflight_git_access(
        type("R", (), {"url": str(origin)})(), timeout=10.0
    )
    assert ok is True and reason == ""


async def test_preflight_missing_repo_reports_reason(tmp_path: Path):
    ok, reason = await preflight_git_access(
        type("R", (), {"url": str(tmp_path / "nope.git")})(), timeout=10.0
    )
    assert ok is False
    assert reason  # 至少给出可读原因（不存在 / 无法访问）


def test_classify_git_error():
    assert "鉴权失败" in _classify_git_error("fatal: Authentication failed for 'https://x'")
    assert "网络不可达" in _classify_git_error("fatal: unable to access 'https://x': Could not resolve host: x")
    assert "仓库不存在" in _classify_git_error("remote: Repository not found")
