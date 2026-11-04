"""执行链路凭据注入：clone/push 命令里不出现密钥，凭据只走 askpass env。

对应设计文档 §4.5 与 D6（分支名统一带 repo 后缀）。
"""

from __future__ import annotations

from pathlib import Path

from sortie_deck import git_publish
from sortie_deck.git_cred import CredentialMissing, ResolvedCredential, host_of, scheme_of
from sortie_deck.models import RepoConfig
from sortie_deck.plugins import coding

SECRET = "glpat-secret-xyz"
REPO_URL = "https://gitlab.example.com/group/app.git"


class FakeResolver:
    def __init__(self, secret: str = SECRET) -> None:
        self.secret = secret
        self.calls: list[tuple[str, str]] = []

    def resolve(self, repo: RepoConfig, *, actor_user_id: str) -> ResolvedCredential:
        self.calls.append((repo.url, actor_user_id))
        return ResolvedCredential(
            credential_id="crd_1",
            host=host_of(repo.url),
            scheme=scheme_of(repo.url),
            username="alice",
            secret=self.secret,
        )


class FailingResolver:
    def resolve(self, repo: RepoConfig, *, actor_user_id: str) -> ResolvedCredential:
        raise CredentialMissing("未找到 gitlab.example.com 的个人 git 凭据")


async def test_clone_uses_env_not_url(tmp_path: Path, monkeypatch):
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def fake_run(args, cwd, timeout=None, *, extra_env=None):
        calls.append((list(args), dict(extra_env or {})))
        if args[:2] == ["git", "clone"]:
            target = Path(args[-1])
            (target / ".git").mkdir(parents=True, exist_ok=True)
            return 0, "", ""
        if args[:2] == ["git", "rev-parse"]:
            return 1, "", ""  # 分支不存在 → 走 checkout -b
        return 0, "", ""

    monkeypatch.setattr(coding, "_run_cmd", fake_run)
    repo = RepoConfig(
        url=REPO_URL,
        branch="main",
        source="workspace",
        credential_id="crd_1",
    )
    resolver = FakeResolver()

    section, log = await coding.clone_repos(
        [repo], tmp_path, "ini_1", actor_user_id="usr_1", resolver=resolver
    )

    clone_args, clone_env = next(c for c in calls if c[0][:2] == ["git", "clone"])
    assert SECRET not in " ".join(clone_args)
    assert clone_args[-2] == REPO_URL  # 推送/克隆都用裸 URL
    assert clone_env.get("SORTIE_GIT_PASSWORD") == SECRET
    assert clone_env.get("GIT_ASKPASS")
    assert clone_env.get("GIT_TERMINAL_PROMPT") == "0"
    assert resolver.calls == [(REPO_URL, "usr_1")]
    assert "sortie/ini_1-app" in section
    assert any("clone exit=0" in line for line in log)


async def test_clone_skips_repo_when_credential_missing(tmp_path: Path, monkeypatch):
    calls: list[list[str]] = []

    async def fake_run(args, cwd, timeout=None, *, extra_env=None):
        calls.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(coding, "_run_cmd", fake_run)
    repo = RepoConfig(url=REPO_URL, branch="main", source="custom")

    section, log = await coding.clone_repos(
        [repo], tmp_path, "ini_1", actor_user_id="usr_1", resolver=FailingResolver()
    )

    assert not any(a[:2] == ["git", "clone"] for a in calls)
    assert any("credential unavailable" in line for line in log)
    assert "CHECKOUT SKIPPED" in section


async def test_push_uses_env_not_url(tmp_path: Path, monkeypatch):
    target = tmp_path / "repos" / "app"
    (target / ".git").mkdir(parents=True)
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def fake_git(args, cwd, timeout=120.0, *, extra_env=None):
        calls.append((list(args), dict(extra_env or {})))
        return 0, "", ""

    monkeypatch.setattr(git_publish, "_git", fake_git)
    repo = RepoConfig(url=REPO_URL, branch="main")

    ok, error = await git_publish._push_branch(
        target,
        repo,
        "sortie/ini_1-app",
        actor_user_id="usr_1",
        resolver=FakeResolver(),
    )

    assert ok is True and error is None
    push_args, push_env = next(c for c in calls if "push" in c[0])
    assert SECRET not in " ".join(push_args)
    assert REPO_URL in push_args
    assert "--force" not in push_args  # 绝不覆盖远端历史
    assert push_env.get("SORTIE_GIT_PASSWORD") == SECRET


def test_branch_naming_unified():
    """D6：单仓/多仓都带 repo 后缀，避免后期加仓留孤儿分支。"""
    assert git_publish.mission_branch("ini_x", "repo", multi_repo=False) == "sortie/ini_x-repo"
    assert git_publish.mission_branch("ini_x", "repo", multi_repo=True) == "sortie/ini_x-repo"


def test_clone_url_ignores_legacy_token():
    """旧 token 字段不再参与 URL 拼接（否则密钥会进 ps / span）。"""
    repo = RepoConfig(url=REPO_URL, token="glpat-legacy-token")
    assert coding._clone_url(repo) == REPO_URL
