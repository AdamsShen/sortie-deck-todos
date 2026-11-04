"""git 凭据 / 仓库 API：管理员维护工作区仓库，个人凭据仅本人可见。

沿用项目既有风格：直接调用路由函数 + 把 settings 指向 tmp_path，
不启动 TestClient（避免真实 data/ 被写脏）。
"""

from __future__ import annotations

import json
from importlib import import_module

import pytest
from fastapi import HTTPException

from sortie_deck.auth import PublicUser
from sortie_deck.git_cred import new_secret_key, stores_from_settings
from sortie_deck.settings import settings

app_mod = import_module("sortie_deck.api.app")

SECRET = "glpat-api-secret-0001"


def _user(uid: str, role: str = "member") -> PublicUser:
    return PublicUser(id=uid, username=uid, display_name=uid, role=role)  # type: ignore[arg-type]


@pytest.fixture()
def git_env(tmp_path, monkeypatch):
    """凭据库与主密钥都指向临时目录。"""
    monkeypatch.setattr(settings, "git_registry_path", tmp_path / "git_registry.sqlite")
    monkeypatch.setattr(settings, "git_secret_key", new_secret_key())
    return tmp_path


def _cred_body(host: str = "gitlab.example.com", secret: str = SECRET):
    return app_mod.GitCredentialBody(
        label="svc", host=host, scheme="https", username="svc", secret=secret
    )


def _workspace_body(host: str = "gitlab.example.com"):
    return app_mod.WorkspaceRepoBody(
        name="platform-server",
        url=f"https://{host}/group/platform-server.git",
        default_branch="main",
        role_slots=["eng_backend"],
        credential=_cred_body(host),
    )


async def test_admin_creates_workspace_repo_all_users_see_it(git_env):
    created = await app_mod.admin_create_workspace_repo(
        _workspace_body(), admin=_user("usr_admin", "admin")
    )
    assert created["url"].endswith("platform-server.git")
    assert SECRET not in json.dumps(created, ensure_ascii=False)

    listed = await app_mod.list_workspace_repos(_user("usr_member"))
    assert [r["id"] for r in listed] == [created["id"]]
    assert SECRET not in json.dumps(listed, ensure_ascii=False)
    # 列表只暴露 credential_id 引用，不含指纹以外的凭据信息
    assert listed[0]["credential_id"] == created["credential_id"]


def test_member_cannot_pass_admin_gate():
    with pytest.raises(HTTPException) as exc:
        app_mod.require_admin(_user("usr_member", "member"))
    assert exc.value.status_code == 403


async def test_workspace_credential_overwrite(git_env):
    created = await app_mod.admin_create_workspace_repo(
        _workspace_body(), admin=_user("usr_admin", "admin")
    )
    await app_mod.admin_set_workspace_credential(
        created["id"],
        app_mod.GitCredentialBody(
            label="rotated", host="gitlab.example.com", username="svc", secret="rotated-token"
        ),
        _admin=_user("usr_admin", "admin"),
    )
    creds, _repos, _user_repos = stores_from_settings()
    # 覆盖同一 host 的凭据而非新建，且新密钥生效
    workspace_creds = creds.list_for_owner("workspace", "workspace")
    assert len(workspace_creds) == 1
    assert creds.decrypt(workspace_creds[0].id) == "rotated-token"


async def test_personal_credentials_are_owner_only(git_env):
    created = await app_mod.create_my_credential(
        _cred_body(host="github.com", secret="pat-owner-only"), user=_user("usr_a")
    )
    assert "secret" not in created and created["fingerprint"].startswith("…")

    mine = await app_mod.list_my_credentials(_user("usr_a"))
    assert [c["id"] for c in mine] == [created["id"]]
    others = await app_mod.list_my_credentials(_user("usr_b"))
    assert others == []

    # 他人删除返回 404（不暴露存在性）
    with pytest.raises(HTTPException) as exc:
        await app_mod.delete_my_credential(created["id"], user=_user("usr_b"))
    assert exc.value.status_code == 404

    # 本人删除成功
    await app_mod.delete_my_credential(created["id"], user=_user("usr_a"))
    assert await app_mod.list_my_credentials(_user("usr_a")) == []


async def test_admin_can_disable_but_not_read_personal_credential(git_env):
    created = await app_mod.create_my_credential(
        _cred_body(host="github.com", secret="pat-to-disable"), user=_user("usr_a")
    )
    await app_mod.admin_disable_credential(created["id"], _admin=_user("usr_admin", "admin"))

    creds, _repos, _user_repos = stores_from_settings()
    assert creds.find_for_host(owner_id="usr_a", host="github.com", scheme="https") is None
    # 列表仍不返回密钥
    rows = await app_mod.list_my_credentials(_user("usr_a"))
    assert "pat-to-disable" not in json.dumps(rows, ensure_ascii=False)


async def test_personal_repo_registry_owner_only(git_env):
    created = await app_mod.create_my_repo(
        app_mod.UserRepoBody(name="my-tools", url="https://github.com/a/my-tools.git"),
        user=_user("usr_a"),
    )
    assert [r["id"] for r in await app_mod.list_my_repos(_user("usr_a"))] == [created["id"]]
    assert await app_mod.list_my_repos(_user("usr_b")) == []

    with pytest.raises(HTTPException) as exc:
        await app_mod.delete_my_repo(created["id"], user=_user("usr_b"))
    assert exc.value.status_code == 404
    await app_mod.delete_my_repo(created["id"], user=_user("usr_a"))


async def test_preflight_unknown_workspace_repo_404(git_env):
    with pytest.raises(HTTPException) as exc:
        await app_mod.git_preflight(
            app_mod.PreflightBody(url="", workspace_repo_id="wrp_missing"),
            user=_user("usr_a"),
        )
    assert exc.value.status_code == 404
