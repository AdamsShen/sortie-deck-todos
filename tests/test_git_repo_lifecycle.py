"""多仓 clone / push / cleanup 生命周期的回归测试。

覆盖四条修复：
1. 浅克隆必须能看到并复用任务分支（--no-single-branch + 远端分支复用）；
2. agent 自己 commit（工作树干净）也必须 push；
3. push 未成功或仍有未上库提交时，不得清理工作副本；
4. 并行准备同名仓库不互相踩踏；cleanup 只删本次发布的仓库。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from sortie_deck.git_publish import (
    _flush_unpushed,
    cleanup_repos,
    mission_branch,
    publish_repos,
)
from sortie_deck.models import RepoConfig
from sortie_deck.plugins.coding import clone_repos


def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"{args} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _make_remote(root: Path) -> Path:
    """建一个带 main 分支初始提交的裸仓库，返回其路径。"""
    root.mkdir(parents=True, exist_ok=True)
    bare = root / "remote.git"
    seed = root / "seed"
    _run(["git", "init", "--bare", "-q", str(bare)])
    _run(["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"])
    _run(["git", "clone", "-q", str(bare), str(seed)])
    _run(["git", "-C", str(seed), "config", "user.email", "t@t"])
    _run(["git", "-C", str(seed), "config", "user.name", "t"])
    (seed / "README.md").write_text("init\n", encoding="utf-8")
    _run(["git", "-C", str(seed), "add", "."])
    _run(["git", "-C", str(seed), "commit", "-qm", "init"])
    _run(["git", "-C", str(seed), "branch", "-M", "main"])
    _run(["git", "-C", str(seed), "push", "-q", "origin", "main"])
    return bare


def _repo(bare: Path, name: str = "repo") -> RepoConfig:
    return RepoConfig(url=f"file://{bare}", branch="main", name=name)


def _head(repo_dir: Path) -> tuple[str, str]:
    return (
        _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).strip(),
        _run(["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"]).strip(),
    )


def _commit_file(checkout: Path, name: str, text: str) -> str:
    (checkout / name).write_text(text, encoding="utf-8")
    _run(["git", "-C", str(checkout), "add", "."])
    _run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            name,
        ]
    )
    return _head(checkout)[0]


@pytest.mark.asyncio
async def test_reclone_restores_pushed_task_branch(tmp_path):
    """push 后删掉工作副本，下一阶段重新 clone 必须复用远端任务分支。"""
    bare = _make_remote(tmp_path / "remote")
    workspace = tmp_path / "ws"
    repo = _repo(bare)
    branch = mission_branch("ini_test", "repo")

    await clone_repos([repo], workspace, "ini_test")
    checkout = workspace / "repos" / "repo"
    assert _head(checkout)[1] == branch

    pushed_head = _commit_file(checkout, "feature.txt", "stage1\n")
    _run(["git", "-C", str(checkout), "push", "-q", "origin", f"{branch}:{branch}"])

    # 模拟 push 后 cleanup + 下一阶段重新 clone
    shutil.rmtree(checkout)
    await clone_repos([repo], workspace, "ini_test")

    head, current = _head(checkout)
    assert current == branch
    assert head == pushed_head, "重新 clone 后必须复用远端任务分支，而不是从基线重建"


@pytest.mark.asyncio
async def test_publish_pushes_clean_tree_commits(tmp_path):
    """agent 自己 commit 过（工作树干净）时，也必须 push 再 cleanup。"""
    bare = _make_remote(tmp_path / "remote")
    workspace = tmp_path / "ws"
    repo = _repo(bare)
    await clone_repos([repo], workspace, "ini_test")
    checkout = workspace / "repos" / "repo"
    head = _commit_file(checkout, "agent.txt", "agent\n")

    results = await publish_repos(workspace, "ini_test", [repo], commit_message="publish")

    assert len(results) == 1
    result = results[0]
    assert result.committed is False, "工作树已干净，不应重复提交"
    assert result.ahead is True, "必须识别出本地领先远端的提交"
    assert result.pushed is True and result.error is None

    remote_head = _run(
        ["git", "-C", str(bare), "rev-parse", mission_branch("ini_test", "repo")]
    ).strip()
    assert remote_head == head, "提交必须真的进了远端"
    assert not checkout.exists(), "确认已 push 后才能清理工作副本"


@pytest.mark.asyncio
async def test_cleanup_skipped_when_push_fails(tmp_path):
    """push 失败时必须保留工作副本，避免本地提交随清理丢失。"""
    bare = _make_remote(tmp_path / "remote")
    workspace = tmp_path / "ws"
    repo = _repo(bare)
    await clone_repos([repo], workspace, "ini_test")
    checkout = workspace / "repos" / "repo"
    _commit_file(checkout, "lost.txt", "must survive\n")

    bad = repo.model_copy(update={"url": f"file://{tmp_path / 'missing.git'}"})
    results = await publish_repos(workspace, "ini_test", [bad], commit_message="publish")

    assert results[0].pushed is False
    assert results[0].error
    assert checkout.exists(), "push 未成功时绝不能清理工作副本"


@pytest.mark.asyncio
async def test_parallel_clone_same_repo_serialized(tmp_path):
    """并行波次同时准备同名仓库：后到的一方复用，不能判失败。"""
    bare = _make_remote(tmp_path / "remote")
    workspace = tmp_path / "ws"
    repo = _repo(bare)

    first, second = await asyncio.gather(
        clone_repos([repo], workspace, "ini_test"),
        clone_repos([repo], workspace, "ini_test"),
    )

    checkout = workspace / "repos" / "repo"
    assert checkout.is_dir()
    logs = "\n".join(first[1] + second[1])
    assert "CHECKOUT FAILED" not in logs
    assert _head(checkout)[1] == mission_branch("ini_test", "repo")


@pytest.mark.asyncio
async def test_flush_unpushed_pushes_before_cleanup(tmp_path):
    """cleanup 之前的兜底：本地已有提交未上库时，先补推再删工作副本。"""
    bare = _make_remote(tmp_path / "remote")
    workspace = tmp_path / "ws"
    repo = _repo(bare)
    await clone_repos([repo], workspace, "ini_test")
    checkout = workspace / "repos" / "repo"
    head = _commit_file(checkout, "late.txt", "late\n")

    flushed = await _flush_unpushed(workspace, [repo], "ini_test", "flush")

    committed, pushed, ahead, error = flushed["repo"]
    assert error is None
    assert ahead is True and pushed is True
    remote_head = _run(
        ["git", "-C", str(bare), "rev-parse", mission_branch("ini_test", "repo")]
    ).strip()
    assert remote_head == head, "兜底 push 必须真的把提交推到远端"


@pytest.mark.asyncio
async def test_cleanup_retries_failed_push_before_delete(tmp_path, monkeypatch):
    """首轮 push 失败时，cleanup 前必须再补推一次；补推成功才允许删除。"""
    import sortie_deck.git_publish as git_publish

    bare = _make_remote(tmp_path / "remote")
    workspace = tmp_path / "ws"
    repo = _repo(bare)
    await clone_repos([repo], workspace, "ini_test")
    checkout = workspace / "repos" / "repo"
    _commit_file(checkout, "retry.txt", "retry\n")

    real_push = git_publish._push_branch
    calls = {"n": 0}

    async def flaky_push(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "transient network error"
        return await real_push(*args, **kwargs)

    monkeypatch.setattr(git_publish, "_push_branch", flaky_push)

    results = await publish_repos(workspace, "ini_test", [repo], commit_message="publish")

    assert calls["n"] == 2, "首轮失败后应补推一次"
    assert results[0].error is None and results[0].pushed is True
    assert not checkout.exists(), "补推成功后应正常 cleanup"


@pytest.mark.asyncio
async def test_cleanup_scoped_to_published_repos(tmp_path):
    """cleanup 只删本次发布的仓库，不能把并行阶段在用的 checkout 一起删掉。"""
    bare_a = _make_remote(tmp_path / "remote-a")
    bare_b = _make_remote(tmp_path / "remote-b")
    workspace = tmp_path / "ws"
    repo_a = _repo(bare_a, "a")
    repo_b = _repo(bare_b, "b")

    await clone_repos([repo_a, repo_b], workspace, "ini_test")
    assert (workspace / "repos" / "a").is_dir()
    assert (workspace / "repos" / "b").is_dir()

    await cleanup_repos(workspace, [repo_a])

    assert not (workspace / "repos" / "a").exists()
    assert (workspace / "repos" / "b").exists(), "其他阶段的 checkout 不能被误删"
