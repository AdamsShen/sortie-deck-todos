"""git 回写链路测试：本地裸仓库模拟远端，验证 commit/push/清理/脏状态/TTL sweep。

不依赖真实 GitLab（gitlab_base_url 默认为空 → MR 步骤软跳过）。
"""

import os
import subprocess
from pathlib import Path

import pytest

from sortie_deck.git_publish import (
    cleanup_repos,
    cleanup_workspace,
    is_dirty,
    mission_branch,
    publish_repos,
    sweep_worktrees,
)
from sortie_deck.models import RepoConfig
from sortie_deck.plugins.coding import clone_repos, ensure_worktree
from sortie_deck.settings import settings


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False, timeout=30
    )


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    """本地裸仓库（模拟 GitLab 远端），含 main 分支一个初始提交。"""
    work = tmp_path / "origin_work"
    work.mkdir()
    assert _git(work, "init", "-b", "main").returncode == 0
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("init\n", encoding="utf-8")
    assert _git(work, "add", "-A").returncode == 0
    assert _git(work, "commit", "-m", "init").returncode == 0
    bare = tmp_path / "origin.git"
    assert _git(tmp_path, "clone", "--bare", str(work), str(bare)).returncode == 0
    return bare


def test_mission_branch_naming():
    assert mission_branch("ini_1", "repo", multi_repo=False) == "sortie/ini_1"
    assert mission_branch("ini_1", "repo", multi_repo=True) == "sortie/ini_1-repo"


@pytest.mark.anyio
async def test_publish_commit_push_and_cleanup(tmp_path: Path, monkeypatch, origin_repo: Path):
    monkeypatch.setattr(settings, "git_cleanup_after_push", True)
    ini = "ini_pub1"
    workspace = ensure_worktree(tmp_path, ini, tmp_path)
    repos = [RepoConfig(url=str(origin_repo), branch="main", name="repo")]
    section, log = await clone_repos(repos, workspace, ini)
    assert section  # prompt 注入了仓库说明

    clone = workspace / "repos" / "repo"
    # 任务分支已创建并被 checkout
    assert (_git(clone, "rev-parse", "--verify", "sortie/" + ini)).returncode == 0
    head = _git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip()
    assert head == f"sortie/{ini}"

    # agent 修改代码 → publish
    (clone / "feature.py").write_text("print('x')\n", encoding="utf-8")
    results = await publish_repos(
        workspace, ini, repos, commit_message=f"sortie: {ini} stage=eng_implement"
    )
    assert len(results) == 1
    r = results[0]
    assert r.error is None and r.committed and r.pushed
    assert r.branch == f"sortie/{ini}"
    assert r.mr_url is None  # 未配置 GitLab API → 软跳过
    # push 成功后工作副本被回收
    assert not (workspace / "repos").exists()
    # 远端裸仓库上存在任务分支
    out = _git(origin_repo, "rev-parse", "--verify", f"refs/heads/sortie/{ini}")
    assert out.returncode == 0


@pytest.mark.anyio
async def test_publish_idempotent_when_no_changes(tmp_path: Path, monkeypatch, origin_repo: Path):
    monkeypatch.setattr(settings, "git_cleanup_after_push", False)
    ini = "ini_pub2"
    workspace = ensure_worktree(tmp_path, ini, tmp_path)
    repos = [RepoConfig(url=str(origin_repo), branch="main", name="repo")]
    await clone_repos(repos, workspace, ini)
    # 未做任何修改 → 不 commit 不 push，无错误
    results = await publish_repos(workspace, ini, repos, commit_message="no-op")
    assert results[0].error is None
    assert results[0].committed is False and results[0].pushed is False


@pytest.mark.anyio
async def test_publish_missing_checkout_soft_fails(tmp_path: Path, origin_repo: Path):
    workspace = ensure_worktree(tmp_path, "ini_pub3", tmp_path)
    repos = [RepoConfig(url=str(origin_repo), branch="main", name="ghost")]
    results = await publish_repos(workspace, "ini_pub3", repos, commit_message="m")
    assert results[0].error and "checkout 缺失" in results[0].error


def test_is_dirty_states(tmp_path: Path, origin_repo: Path):
    ini = "ini_dirty"
    workspace = ensure_worktree(tmp_path, ini, tmp_path)
    repos = [RepoConfig(url=str(origin_repo), branch="main", name="repo")]
    import asyncio

    asyncio.run(clone_repos(repos, workspace, ini))
    clone = workspace / "repos" / "repo"
    # 全新 clone（无改动无提交）→ 干净
    assert is_dirty(clone) is False
    # 未提交修改 → 脏
    (clone / "new.py").write_text("x=1\n", encoding="utf-8")
    assert is_dirty(clone) is True
    # 提交但未 push → 脏
    _git(clone, "add", "-A")
    _git(clone, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "wip")
    assert is_dirty(clone) is True
    # push（带 -u）+ 同步远端跟踪引用（生产路径 _push_branch 的行为）→ 干净
    _git(clone, "push", "-u", str(origin_repo), f"sortie/{ini}:sortie/{ini}")
    _git(clone, "update-ref", f"refs/remotes/origin/sortie/{ini}", "HEAD")
    assert is_dirty(clone) is False


def test_sweep_respects_ttl_and_dirty_state(tmp_path: Path, origin_repo: Path):
    import asyncio
    import time as _time

    async def _make(ini: str, dirty: bool) -> Path:
        ws = ensure_worktree(tmp_path, ini, tmp_path)
        repos = [RepoConfig(url=str(origin_repo), branch="main", name="repo")]
        await clone_repos(repos, ws, ini)
        clone = ws / "repos" / "repo"
        if dirty:
            (clone / "wip.py").write_text("x=1\n", encoding="utf-8")
        return ws

    ws_clean = asyncio.run(_make("ini_clean", dirty=False))
    ws_dirty = asyncio.run(_make("ini_dirty", dirty=True))
    # 把两个 workspace 的 mtime 拨到 25 小时前
    for ws in (ws_clean, ws_dirty):
        old = _time.time() - 25 * 3600
        os.utime(ws, (old, old))

    cleaned = sweep_worktrees(
        tmp_path,
        active_ids=set(),
        ttl_seconds=24 * 3600,
        dirty_ttl_seconds=7 * 24 * 3600,
    )
    # 干净且超 TTL → 清理；脏状态在延长 TTL 内 → 保留
    assert "ini_clean" in cleaned
    assert ws_clean.exists() is False
    assert ws_dirty.exists() is True
    # 脏状态超过延长 TTL → 清理
    old = _time.time() - 8 * 24 * 3600
    os.utime(ws_dirty, (old, old))
    cleaned = sweep_worktrees(
        tmp_path,
        active_ids=set(),
        ttl_seconds=24 * 3600,
        dirty_ttl_seconds=7 * 24 * 3600,
    )
    assert "ini_dirty" in cleaned
    assert ws_dirty.exists() is False


def test_sweep_skips_active_and_missing(tmp_path: Path, origin_repo: Path):
    import asyncio
    import time as _time

    async def _make(ini: str) -> Path:
        ws = ensure_worktree(tmp_path, ini, tmp_path)
        repos = [RepoConfig(url=str(origin_repo), branch="main", name="repo")]
        await clone_repos(repos, ws, ini)
        return ws

    ws_active = asyncio.run(_make("ini_active"))
    old = _time.time() - 48 * 3600
    os.utime(ws_active, (old, old))
    cleaned = sweep_worktrees(
        tmp_path,
        active_ids={"ini_active"},
        ttl_seconds=24 * 3600,
        dirty_ttl_seconds=7 * 24 * 3600,
    )
    assert "ini_active" not in cleaned
    assert ws_active.exists() is True
    # cleanup_workspace 幂等
    cleanup_workspace(ws_active)
    cleanup_workspace(ws_active)  # 不抛错
    assert ws_active.exists() is False


def _raise_system_exit(_path):
    """模拟运行环境（IDE 批量删除守卫）在 rmtree 上抛 SystemExit。"""
    raise SystemExit(1)


@pytest.mark.anyio
async def test_cleanup_repos_swallows_system_exit(tmp_path: Path, monkeypatch):
    """回归 2026-10-27 事故：清理时被注入 SystemExit 只能吞掉，不得上抛。

    该异常是 BaseException，若穿透 cleanup_repos 会越过阶段与 LangGraph，
    把运行任务和 API 进程一起带走。
    """
    workspace = tmp_path / "ws"
    (workspace / "repos" / "repo").mkdir(parents=True)
    (workspace / "repos" / "repo" / "f.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr("sortie_deck.git_publish.shutil.rmtree", _raise_system_exit)

    await cleanup_repos(workspace)  # 不抛 SystemExit
    assert (workspace / "repos").exists()  # 保留现场，交由 TTL sweep 兜底


def test_cleanup_workspace_swallows_system_exit(tmp_path: Path, monkeypatch):
    """终态清理同样不能因为环境注入的 SystemExit 打断状态流转。"""
    workspace = tmp_path / "ws2"
    workspace.mkdir()
    monkeypatch.setattr("sortie_deck.git_publish.shutil.rmtree", _raise_system_exit)

    cleanup_workspace(workspace)  # 不抛 SystemExit
    assert workspace.exists()
