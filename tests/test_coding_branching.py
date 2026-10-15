"""编码阶段分支化测试：沙盒路径、clone→任务分支、prompt 注入、publish 触发与磁盘防护。"""

import subprocess
from pathlib import Path

import pytest

from sortie_deck.graph import _maybe_publish
from sortie_deck.models import RepoConfig, RoleId, StageContext, StageResult
from sortie_deck.plugins.coding import clone_repos, ensure_worktree
from sortie_deck.settings import settings


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False, timeout=30
    )


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    work = tmp_path / "origin_work"
    work.mkdir()
    assert _git(work, "init", "-b", "main").returncode == 0
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "app.py").write_text("print('hi')\n", encoding="utf-8")
    assert _git(work, "add", "-A").returncode == 0
    assert _git(work, "commit", "-m", "init").returncode == 0
    bare = tmp_path / "origin.git"
    assert _git(tmp_path, "clone", "--bare", str(work), str(bare)).returncode == 0
    return bare


def test_ensure_worktree_sandbox_layout(tmp_path: Path):
    """沙盒退化为普通目录：missions/<id>/workspace，不再有 git worktree/tdt 分支。"""
    missions = tmp_path
    ws = ensure_worktree(tmp_path, "ini_ws1", missions)
    assert ws == missions / "ini_ws1" / "workspace"
    assert ws.is_dir()
    assert (ws / "README.md").is_file()
    # 幂等：重复调用同一路径
    assert ensure_worktree(tmp_path, "ini_ws1", missions) == ws
    # 不是 git 仓库（无 worktree 语义）
    assert not (ws / ".git").exists()


@pytest.mark.anyio
async def test_clone_creates_task_branch_and_prompt(tmp_path: Path, origin_repo: Path):
    ini = "ini_br1"
    workspace = ensure_worktree(tmp_path, ini, tmp_path)
    repos = [RepoConfig(url=str(origin_repo), branch="main", name="repo")]
    section, log = await clone_repos(repos, workspace, ini)
    clone = workspace / "repos" / "repo"
    # 任务分支存在且已 checkout
    assert _git(clone, "rev-parse", "--verify", f"sortie/{ini}").returncode == 0
    assert _git(clone, "symbolic-ref", "--short", "HEAD").stdout.strip() == f"sortie/{ini}"
    # prompt 与日志注入了任务分支信息
    assert f"sortie/{ini}" in section
    assert any(f"task branch created=sortie/{ini}" in line for line in log)
    # 幂等重试：分支复用
    section2, log2 = await clone_repos(repos, workspace, ini)
    assert any(f"task branch reused=sortie/{ini}" in line for line in log2)
    # 多仓库命名：带仓库名后缀
    repos2 = [
        RepoConfig(url=str(origin_repo), branch="main", name="backend"),
        RepoConfig(url=str(origin_repo), branch="main", name="web"),
    ]
    ws2 = ensure_worktree(tmp_path, "ini_br2", tmp_path)
    await clone_repos(repos2, ws2, "ini_br2")
    assert (
        _git(ws2 / "repos" / "backend", "rev-parse", "--verify", "sortie/ini_br2-backend")
    ).returncode == 0
    assert (
        _git(ws2 / "repos" / "web", "rev-parse", "--verify", "sortie/ini_br2-web")
    ).returncode == 0


def _ctx(ini: str, workspace: Path, repos: list[RepoConfig], tmp_path: Path) -> StageContext:
    return StageContext(
        initiative_id=ini,
        thread_id="thr_x",
        stage="eng_implement",
        role=RoleId.ENG,
        brief="b",
        artifact_dir=str(tmp_path / "artifacts"),
        workspace_dir=str(workspace),
        repos=repos,
    )


@pytest.mark.anyio
async def test_maybe_publish_on_stage_success(tmp_path: Path, monkeypatch, origin_repo: Path):
    monkeypatch.setattr(settings, "git_push_mode", "on_stage_success")
    monkeypatch.setattr(settings, "git_cleanup_after_push", True)
    ini = "ini_hook1"
    workspace = ensure_worktree(tmp_path, ini, tmp_path)
    repos = [RepoConfig(url=str(origin_repo), branch="main", name="repo")]
    await clone_repos(repos, workspace, ini)
    clone = workspace / "repos" / "repo"
    (clone / "impl.py").write_text("x=1\n", encoding="utf-8")

    ctx = _ctx(ini, workspace, repos, tmp_path)
    result = StageResult(status="ok", message="done")
    meta = await _maybe_publish({"initiative_id": ini}, ctx, result, "eng_implement")
    assert meta is not None and meta["git_publish_pushed"] == 1
    assert meta["git_publish"][0]["branch"] == f"sortie/{ini}"
    # push 后副本清理
    assert not (workspace / "repos").exists()


@pytest.mark.anyio
async def test_maybe_publish_gates(tmp_path: Path, monkeypatch, origin_repo: Path):
    monkeypatch.setattr(settings, "git_push_mode", "on_approval")
    ini = "ini_hook2"
    workspace = ensure_worktree(tmp_path, ini, tmp_path)
    repos = [RepoConfig(url=str(origin_repo), branch="main", name="repo")]
    await clone_repos(repos, workspace, ini)
    ctx = _ctx(ini, workspace, repos, tmp_path)
    result = StageResult(status="ok")

    # on_approval 模式下无 publish_pending 标记 → 不回写
    assert await _maybe_publish({}, ctx, result, "eng_implement") is None
    # 带 publish_pending → 回写
    meta = await _maybe_publish(
        {"initiative_id": ini, "publish_pending": True}, ctx, result, "eng_implement"
    )
    assert meta is not None
    # off 模式 → 永不回写
    monkeypatch.setattr(settings, "git_push_mode", "off")
    assert await _maybe_publish({"publish_pending": True}, ctx, result, "eng_implement") is None
    # 失败阶段 → 不回写
    monkeypatch.setattr(settings, "git_push_mode", "on_stage_success")
    assert await _maybe_publish({}, ctx, StageResult(status="fail"), "eng_implement") is None


@pytest.mark.anyio
async def test_disk_guard_skips_clone(tmp_path: Path, monkeypatch, origin_repo: Path):
    monkeypatch.setattr(settings, "min_disk_free_mb", 1 << 40)  # 巨大阈值 → 必触发
    ini = "ini_disk"
    workspace = ensure_worktree(tmp_path, ini, tmp_path)
    repos = [RepoConfig(url=str(origin_repo), branch="main", name="repo")]
    section, log = await clone_repos(repos, workspace, ini)
    assert any("DISK GUARD" in line for line in log)
    assert not (workspace / "repos" / "repo").exists()
    assert "NOT AVAILABLE" in section
