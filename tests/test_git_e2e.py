"""端到端：工作区仓库 → clone → 提交 → push 任务分支（真实 git，本地 bare 远端）。

验证设计文档 §7.1 的分支语义：只推远端同名分支，目标分支不被改动，无 merge。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sortie_deck.git_cred import (
    SCOPE_WORKSPACE,
    new_secret_key,
    resolver_from_settings,
    stores_from_settings,
)
from sortie_deck.git_publish import publish_repos
from sortie_deck.models import RepoConfig
from sortie_deck.plugins.coding import clone_repos
from sortie_deck.settings import settings


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def origin_repo(tmp_path: Path) -> Path:
    """带一个 main 提交的 bare 仓库，充当远端。"""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-q", str(origin))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    _git("config", "user.email", "t@example.com", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "README.md").write_text("# demo\n", encoding="utf-8")
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "init", cwd=seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)
    return origin


async def test_workspace_repo_clone_and_publish(tmp_path: Path, origin_repo: Path, monkeypatch):
    monkeypatch.setattr(settings, "git_registry_path", tmp_path / "git_registry.sqlite")
    monkeypatch.setattr(settings, "git_secret_key", new_secret_key())
    monkeypatch.setattr(settings, "git_cleanup_after_push", False)

    creds, workspace_repos, _user_repos = stores_from_settings()
    cred = creds.create(
        scope=SCOPE_WORKSPACE,
        owner_id="workspace",
        label="svc",
        host="origin.local",
        scheme="https",
        username="svc",
        secret="svc-token-e2e",
    )
    workspace_repos.create(
        name="repo",
        url=str(origin_repo),
        default_branch="main",
        role_slots=["eng_backend"],
        credential_id=cred.id,
        created_by="usr_admin",
    )
    repo = RepoConfig(
        url=str(origin_repo),
        branch="main",
        name="repo",
        source="workspace",
        credential_id=cred.id,
    )
    resolver = resolver_from_settings()
    workspace = tmp_path / "ws"
    workspace.mkdir()

    main_before = _git("--git-dir", str(origin_repo), "rev-parse", "main")

    section, log = await clone_repos(
        [repo], workspace, "ini_e2e", actor_user_id="usr_admin", resolver=resolver
    )
    clone = workspace / "repos" / "repo"
    assert (clone / ".git").exists(), log
    assert "sortie/ini_e2e-repo" in section
    # 任务分支基于 main 创建
    assert _git("symbolic-ref", "--short", "HEAD", cwd=clone) == "sortie/ini_e2e-repo"

    (clone / "impl.py").write_text("print('hi')\n", encoding="utf-8")
    results = await publish_repos(
        workspace,
        "ini_e2e",
        [repo],
        commit_message="e2e change",
        actor_user_id="usr_admin",
        resolver=resolver,
    )
    assert len(results) == 1
    assert results[0].error is None, results[0].error
    assert results[0].pushed is True

    # 远端出现任务分支，且 main 未被改动（不合并、不覆盖）
    remote_branch = _git(
        "--git-dir", str(origin_repo), "rev-parse", "--verify", "sortie/ini_e2e-repo"
    )
    assert remote_branch
    assert _git("--git-dir", str(origin_repo), "rev-parse", "main") == main_before
    # 远端只有 main + 任务分支，没有额外分支
    branches = _git("--git-dir", str(origin_repo), "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert sorted(branches.splitlines()) == ["main", "sortie/ini_e2e-repo"]
