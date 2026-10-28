"""任务代码回写与工作区生命周期管理。

回写链路（编码阶段成功后由 graph.run_stage 触发）::

    workspace/repos/<name>  →  git add -A → commit → push 任务分支
                            →  GitLab API 创建 MR（人工在平台合并）
                            →  cleanup：删除 repos/ 工作副本

设计要点：
- 分支权威数据在 GitLab，本地 clone 只是临时工作副本（push 后即可回收）；
- 任一仓库失败不中断其余仓库（软降级），结果进 StageResult.meta → 事件流；
- push 使用带 token 的完整 URL（复用 coding._clone_url），不在远端配置里落 token；
- 幂等可重试：无变更不 commit，MR 按源分支去重复用。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sortie_deck.models import RepoConfig
from sortie_deck.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class PublishResult:
    """单个仓库的回写结果（进阶段 meta 与事件流）。"""

    repo_name: str  # checkout 目录名（workspace/repos/<name>）
    branch: str  # 任务分支名
    committed: bool  # 本次是否产生新提交
    pushed: bool  # 是否成功 push
    mr_url: str | None  # GitLab MR web_url（新建或复用）
    error: str | None  # 软降级错误信息（None=成功）


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:80]


def mission_branch(initiative_id: str, repo_name: str, *, multi_repo: bool) -> str:
    """任务分支命名：单仓库 ``sortie/<initiative_id>``；多仓库 ``sortie/<id>-<name>``。"""
    prefix = (settings.git_branch_prefix or "sortie").strip() or "sortie"
    if multi_repo:
        return f"{prefix}/{_sanitize(initiative_id)}-{_sanitize(repo_name)}"
    return f"{prefix}/{_sanitize(initiative_id)}"


async def _git(args: list[str], cwd: Path, timeout: float = 120.0) -> tuple[int, str, str]:
    """在指定目录执行 git 子命令，返回 (returncode, stdout, stderr)。"""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", f"git timeout after {timeout}s"
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


def _scrub(text: str, repo: RepoConfig) -> str:
    """日志脱敏：出现 clone token 的地方替换为 ***。"""
    if repo.token:
        return text.replace(repo.token, "***")
    return text


def _repo_dirname(repo: RepoConfig) -> str:
    from sortie_deck.plugins.coding import _repo_dirname as _dirname

    return _dirname(repo)


def _clone_url(repo: RepoConfig) -> str:
    from sortie_deck.plugins.coding import _clone_url as _url

    return _url(repo)


async def _commit_all(target: Path, message: str) -> tuple[bool, str | None]:
    """git add -A + commit（有变更才提交）。返回 (是否有新提交, 错误信息)。"""
    code, _, err = await _git(["add", "-A"], target)
    if code != 0:
        return False, f"git add 失败: {err[:300]}"
    code, status, _ = await _git(["status", "--porcelain"], target)
    if code != 0:
        return False, "git status 失败"
    if not status.strip():
        return False, None  # 无变更，无需提交
    # -c 注入身份：服务器上通常没有全局 git user 配置
    code, _, err = await _git(
        [
            "-c",
            "user.name=Sortie Deck",
            "-c",
            "user.email=sortie-deck@localhost",
            "commit",
            "-m",
            message[:500],
        ],
        target,
    )
    if code != 0:
        return False, f"git commit 失败: {err[:300]}"
    return True, None


async def _push_branch(target: Path, repo: RepoConfig, branch: str) -> tuple[bool, str | None]:
    """把任务分支 push 到远端（用带 token 的完整 URL，不在 remote 配置里落 token）。

    ``-u`` 建立上游跟踪，供脏状态判定（@{u}..HEAD）使用。
    """
    code, out, err = await _git(["push", "-u", _clone_url(repo), f"{branch}:{branch}"], target)
    if code != 0:
        return False, _scrub(f"git push 失败: {(err or out)[:300]}", repo)
    # push 已成功，同步远端跟踪引用（URL push 不会自动更新 refs/remotes），
    # 供脏状态判定（rev-list HEAD --not --remotes）区分"已上库/未上库"
    await _git(["update-ref", f"refs/remotes/origin/{branch}", "HEAD"], target)
    return True, None


def _ensure_mr(repo: RepoConfig, branch: str, initiative_id: str, title: str) -> str | None:
    """创建 GitLab MR（人工合并），返回 web_url；未配置/失败返回 None（软降级）。"""
    base_url = (settings.gitlab_base_url or "").strip()
    if not base_url:
        return None
    token = (settings.gitlab_api_token or "").strip() or (repo.token or "").strip()
    if not token:
        logger.warning("gitlab 未配置 API token，跳过 MR 创建（initiative=%s）", initiative_id)
        return None
    from sortie_deck.gitlab import GitLabClient, GitLabError, project_path_from_url

    path = project_path_from_url(repo.url)
    if not path:
        logger.warning(
            "无法从 repo url 解析 GitLab project path，跳过 MR（initiative=%s）", initiative_id
        )
        return None
    try:
        client = GitLabClient(base_url, token)
        return client.create_mr(
            path,
            branch,
            repo.branch or None,
            title,
            description=f"由 Sortie Deck 任务 `{initiative_id}` 自动创建（编码 Agent 产出，请人工审核合并）。",
        )
    except GitLabError as exc:
        # MR 失败不回滚 push，只记日志进事件流
        logger.warning("gitlab MR 创建失败（initiative=%s, repo=%s）: %s", initiative_id, path, exc)
        return None


async def publish_repos(
    workspace: Path,
    initiative_id: str,
    repos: list[RepoConfig],
    *,
    commit_message: str,
) -> list[PublishResult]:
    """逐仓库 commit + push 任务分支 + 建 MR；任一失败不中断其余仓库。

    输入输出示例：workspace=data/missions/<id>/workspace，repos=[RepoConfig(url,
    branch, name)] → [PublishResult(repo_name="repo", branch="sortie/ini_x",
    committed=True, pushed=True, mr_url="https://gitlab/.../-/merge_requests/1",
    error=None)]。
    """
    from sortie_deck.plugins.coding import _repo_dirname  # noqa: F401 — 保持一致命名来源

    multi = len(repos) > 1
    results: list[PublishResult] = []
    for repo in repos:
        name = _repo_dirname(repo)
        branch = mission_branch(initiative_id, name, multi_repo=multi)
        target = workspace / "repos" / name
        if not (target / ".git").exists():
            results.append(
                PublishResult(
                    repo_name=name,
                    branch=branch,
                    committed=False,
                    pushed=False,
                    mr_url=None,
                    error="checkout 缺失（clone 未执行或已被清理）",
                )
            )
            continue
        committed: bool = False
        pushed: bool = False
        mr_url: str | None = None
        error: str | None = None
        committed, error = await _commit_all(target, commit_message)
        if error is None and committed:
            pushed, error = await _push_branch(target, repo, branch)
        if error is None and pushed:
            mr_url = _ensure_mr(
                repo, branch, initiative_id, f"Sortie Deck: {initiative_id} → {repo.branch or 'default'}"
            )
        results.append(
            PublishResult(
                repo_name=name,
                branch=branch,
                committed=committed,
                pushed=pushed,
                mr_url=mr_url,
                error=error,
            )
        )
        if error:
            logger.warning(
                "git publish 部分失败 initiative=%s repo=%s: %s", initiative_id, name, error
            )
    if settings.git_cleanup_after_push and all(r.error is None for r in results) and results:
        await cleanup_repos(workspace)
    return results


async def cleanup_repos(workspace: Path) -> None:
    """push 全部成功后删除 workspace/repos 工作副本（保留沙盒目录本身）。"""
    repos_root = workspace / "repos"
    if not repos_root.exists():
        return
    try:
        await asyncio.to_thread(shutil.rmtree, repos_root)
        logger.info("工作副本已回收（push 完成）: %s", repos_root)
    except asyncio.CancelledError:
        raise
    except (OSError, SystemExit) as exc:
        # 清理是收尾动作，失败不允许影响阶段结果：除 OSError 外，运行环境
        # （如 IDE 的批量删除守卫）会在 rmtree 里 raise SystemExit——它是
        # BaseException，继续上抛会穿过阶段与 LangGraph 直接杀掉进程
        # （2026-10-27 事故），因此这里一并吞掉，只记 warning。
        logger.warning("工作副本回收失败 %s: %s", repos_root, exc)


def cleanup_workspace(workspace_dir: Path) -> None:
    """任务终态：无条件删除 workspace 沙盒目录（记忆/产物/聊天保留，属任务档案）。"""
    if not workspace_dir.exists():
        return
    try:
        shutil.rmtree(workspace_dir)
        logger.info("任务终态清理 workspace: %s", workspace_dir)
    except (OSError, SystemExit) as exc:
        # 同上：终态清理失败（含环境注入的 SystemExit）不能反过来打断状态流转
        logger.warning("任务终态清理失败 %s: %s", workspace_dir, exc)


def is_dirty(repo_dir: Path) -> bool:
    """工作副本是否为脏状态（未提交变更或未推送提交）——TTL sweep 延长保留的依据。

    对标 Gitpod：干净 git 的 workspace 停止后 14 天 GC，脏 git 延长到 28 天。
    同步实现（sweep 运行在线程池里，避免嵌套事件循环）。
    """
    import subprocess

    def _run(args: list[str]) -> tuple[int, str]:
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            return r.returncode, r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return 1, ""

    code, status = _run(["status", "--porcelain"])
    if code != 0:
        return False  # 不是有效 git 目录（或已被清理），不按脏处理
    if status:
        return True
    # 未推送提交判定：有上游用 @{u}..HEAD；无上游（尚未 push 过）用
    # "不在任何远端引用上的提交数"——clone 自带的 origin/<base> 会被计入远端，
    # 因此全新 clone（agent 未提交任何东西）判干净，agent 已提交未 push 判脏。
    code, ahead = _run(["rev-list", "--count", "@{u}..HEAD"])
    if code == 0:
        return bool(ahead.isdigit() and int(ahead) > 0)
    code, unpushed = _run(["rev-list", "--count", "HEAD", "--not", "--remotes"])
    if code == 0:
        return bool(unpushed.isdigit() and int(unpushed) > 0)
    return False


def sweep_worktrees(
    missions_root: Path,
    active_ids: set[str],
    ttl_seconds: float,
    dirty_ttl_seconds: float,
) -> list[str]:
    """TTL 兜底扫描：删除非运行中任务遗留的 workspace（进程崩溃残留等）。

    - 运行中（active_ids）任务跳过；
    - 脏状态（未提交/未推送）用 dirty_ttl，否则用 ttl；
    - 返回被清理的 initiative id 列表。
    """
    cleaned: list[str] = []
    now = time.time()
    root = Path(missions_root)
    if not root.is_dir():
        return cleaned
    for ini_dir in sorted(root.iterdir()):
        if not ini_dir.is_dir() or ini_dir.name in active_ids:
            continue
        ws = ini_dir / "workspace"
        if not ws.is_dir():
            continue
        try:
            age = now - ws.stat().st_mtime
        except OSError:
            continue
        limit = ttl_seconds
        if any(
            is_dirty(repo)
            for repo in (ws / "repos").glob("*")
            if repo.is_dir() and (repo / ".git").exists()
        ):
            limit = dirty_ttl_seconds
        if age <= limit:
            continue
        cleanup_workspace(ws)
        cleaned.append(ini_dir.name)
    return cleaned


def publish_summary_meta(results: list[PublishResult]) -> dict[str, Any]:
    """把回写结果整理成可进 StageResult.meta / 事件流的 dict。"""
    pushed = [r for r in results if r.pushed]
    return {
        "git_publish": [asdict(r) for r in results],
        "git_publish_pushed": len(pushed),
        "git_publish_errors": [r.error for r in results if r.error],
    }
