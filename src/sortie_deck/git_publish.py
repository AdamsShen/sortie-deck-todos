"""任务代码回写与工作区生命周期管理。

回写链路（编码阶段成功后由 graph.run_stage 触发）::

    workspace/repos/<name>  →  git add -A → commit → push 任务分支
                            →  GitLab API 创建 MR（人工在平台合并）
                            →  cleanup：确认无未上库提交后删除本次的 checkout

设计要点：
- 分支权威数据在 GitLab，本地 clone 只是临时工作副本（确认 push 后才能回收）；
- 任一仓库失败不中断其余仓库（软降级），结果进 StageResult.meta → 事件流；
- 凭据经 `GIT_ASKPASS` 环境变量注入（见 git_cred），命令行与远端配置都不落密钥；
- 幂等可重试：无变更不 commit，MR 按源分支去重复用。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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
    # HEAD 上是否存在未进入远端的提交（含 agent 自己 commit 的情况）。
    # cleanup 必须等 ahead=False 才允许执行，否则会删掉未上库的提交。
    ahead: bool = False


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:80]


def mission_branch(initiative_id: str, repo_name: str, *, multi_repo: bool = True) -> str:
    """任务分支命名：统一 ``sortie/<initiative_id>-<repo>``（决策 D6）。

    ``multi_repo`` 仅为兼容旧调用点保留：早期单仓用 ``sortie/<id>``、多仓才带
    仓库后缀，导致任务后期加仓会留下孤儿分支。现在一律带后缀，参数不影响结果。
    """
    prefix = (settings.git_branch_prefix or "sortie").strip() or "sortie"
    return f"{prefix}/{_sanitize(initiative_id)}-{_sanitize(repo_name)}"


async def _git(
    args: list[str],
    cwd: Path,
    timeout: float = 120.0,
    *,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """在指定目录执行 git 子命令，返回 (returncode, stdout, stderr)。

    ``extra_env`` 用于注入凭据（GIT_ASKPASS），保证密钥不出现在命令行里。
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=({**os.environ, **(extra_env or {})} if extra_env else None),
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


async def _unpushed_count(target: Path) -> tuple[int | None, str | None]:
    """HEAD 上有多少提交未进入任何远端分支；``None`` 表示无法判断。

    工作树干净不代表已上库：agent 可能自己 ``git commit`` 过，此时
    ``_commit_all`` 返回 False，若直接跳过 push 再 cleanup，提交会随工作副本一起丢失。
    """
    # 空仓库（尚无任何提交）没有可上库内容，直接视为 0，避免误报 push 失败
    head_code, _, _ = await _git(["rev-parse", "--verify", "HEAD"], target)
    if head_code != 0:
        return 0, None
    code, out, err = await _git(["rev-list", "--count", "HEAD", "--not", "--remotes"], target)
    if code != 0:
        return None, f"git rev-list 失败: {(err or out)[:200]}"
    try:
        return int((out or "").strip() or "0"), None
    except ValueError:
        return None, f"git rev-list 输出异常: {(out or '')[:120]}"


async def _all_pushed(workspace: Path, repos: list[RepoConfig]) -> bool:
    """cleanup 前的最后一道闸：每个 checkout 都不能有未上库的提交。"""
    from sortie_deck.plugins.coding import _repo_dirname

    for repo in repos:
        target = workspace / "repos" / _repo_dirname(repo)
        if not (target / ".git").exists():
            continue  # 没有工作副本 = 没有本地提交可丢
        count, err = await _unpushed_count(target)
        if err is not None or count:
            logger.warning(
                "cleanup 跳过，存在未上库提交: repo=%s count=%s err=%s",
                target.name,
                count,
                err,
            )
            return False
    return True


async def _flush_unpushed(
    workspace: Path,
    repos: list[RepoConfig],
    initiative_id: str,
    commit_message: str,
    *,
    actor_user_id: str = "",
    resolver: Any | None = None,
) -> dict[str, tuple[bool, bool, bool, str | None]]:
    """cleanup 前的兜底提交：把仍在本地的改动/提交补推一次。

    返回 ``{repo_name: (committed, pushed, ahead, error)}``。典型场景：
    - 首轮 push 因瞬时网络/凭据抖动失败，这里再试一次；
    - 并行阶段在本阶段 push 之后又往同一 checkout 写了新提交。
    """
    from sortie_deck.plugins.coding import _repo_dirname

    flushed: dict[str, tuple[bool, bool, bool, str | None]] = {}
    for repo in repos:
        name = _repo_dirname(repo)
        target = workspace / "repos" / name
        if not (target / ".git").exists():
            continue
        committed, error = await _commit_all(target, commit_message)
        if error is not None:
            flushed[name] = (committed, False, False, error)
            continue
        count, count_err = await _unpushed_count(target)
        if count_err is not None:
            flushed[name] = (committed, False, False, count_err)
            continue
        ahead = bool(count)
        pushed = False
        if committed or ahead:
            branch = mission_branch(initiative_id, name, multi_repo=len(repos) > 1)
            pushed, error = await _push_branch(
                target, repo, branch, actor_user_id=actor_user_id, resolver=resolver
            )
        flushed[name] = (committed, pushed, ahead, error)
    return flushed


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


async def _push_branch(
    target: Path,
    repo: RepoConfig,
    branch: str,
    *,
    actor_user_id: str = "",
    resolver: Any | None = None,
) -> tuple[bool, str | None]:
    """把任务分支 push 到远端（推到**远端同名分支**，不合并、不加 --force）。

    凭据经 askpass env 注入；命令行里始终是裸 URL。
    ``-u`` 建立上游跟踪，供脏状态判定（@{u}..HEAD）使用。
    """
    extra_env: dict[str, str] = {}
    git_ctx = None
    if resolver is not None:
        from sortie_deck.git_cred import build_askpass_env

        try:
            resolved = resolver.resolve(repo, actor_user_id=actor_user_id)
        except Exception as exc:  # noqa: BLE001 — 凭据问题属于可读失败原因
            return False, f"git push 失败: 凭据不可用（{exc}）"
        if resolved.scheme == "https":
            git_ctx = build_askpass_env(
                username=resolved.username, secret=resolved.secret, workdir=target
            )
            extra_env = git_ctx.env
    try:
        code, out, err = await _git(
            ["push", "-u", _clone_url(repo), f"{branch}:{branch}"],
            target,
            extra_env=extra_env,
        )
    finally:
        if git_ctx is not None:
            git_ctx.__exit__()
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
    actor_user_id: str = "",
    resolver: Any | None = None,
) -> list[PublishResult]:
    """逐仓库 commit + push 任务分支 + 建 MR；任一失败不中断其余仓库。

    全部仓库处理完后、cleanup 之前会再补一次 commit+push（覆盖首轮失败与并行
    阶段的新提交），确认每个 checkout 都没有未上库提交后，才删除工作副本。

    输入输出示例：workspace=data/missions/<id>/workspace，repos=[RepoConfig(url,
    branch, name)] → [PublishResult(repo_name="repo", branch="sortie/ini_x-repo",
    committed=True, pushed=True, mr_url="https://gitlab/.../-/merge_requests/1",
    error=None)]。

    ``actor_user_id`` 用于解析 custom 仓库的个人凭据；工作区仓库不受其影响。
    """
    from sortie_deck.plugins.coding import _repo_dirname  # noqa: F401 — 保持一致命名来源

    if resolver is None and actor_user_id:
        try:
            from sortie_deck.git_cred import resolver_from_settings

            resolver = resolver_from_settings()
        except Exception:  # noqa: BLE001 — 凭据库不可用时按无凭据回写（公开仓库）
            logger.warning("git 凭据库不可用，push 将以无凭据方式执行", exc_info=True)

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
        ahead: bool = False
        mr_url: str | None = None
        error: str | None = None
        committed, error = await _commit_all(target, commit_message)
        if error is None:
            count, count_err = await _unpushed_count(target)
            if count_err is not None:
                error = count_err
            else:
                ahead = bool(count)
                # 有未上库提交就必须 push：包括 agent 自己 commit、工作树已干净的情况。
                # 否则 cleanup 会删掉这些只存在于本地的提交。
                if committed or ahead:
                    pushed, error = await _push_branch(
                        target, repo, branch, actor_user_id=actor_user_id, resolver=resolver
                    )
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
                ahead=ahead,
            )
        )
        if error:
            logger.warning(
                "git publish 部分失败 initiative=%s repo=%s: %s", initiative_id, name, error
            )
    # cleanup 前先补一次 commit+push：首轮 push 失败、或并行阶段刚写入的新提交，
    # 都在这里再推一次；推完再按 git 事实确认「每个 checkout 都没有未进入远端的
    # 提交」，确认通过才允许删除工作副本。
    if settings.git_cleanup_after_push and results:
        by_name = {r.repo_name: r for r in results}
        flushed = await _flush_unpushed(
            workspace,
            repos,
            initiative_id,
            commit_message,
            actor_user_id=actor_user_id,
            resolver=resolver,
        )
        for name, (committed, pushed, ahead, error) in flushed.items():
            result = by_name.get(name)
            if result is None:
                continue
            result.committed = result.committed or committed
            result.pushed = result.pushed or pushed
            # 保留首轮观察到的「本地领先远端」事实：兜底 push 成功后 ahead 会归零，
            # 但事件流里应该能看到这次是靠补推才上库的。
            result.ahead = result.ahead or ahead
            # 兜底结果是当前状态的权威值：补推成功要清掉首轮的错误，否则 cleanup 会被误拦
            result.error = error
        if all(r.error is None for r in results) and await _all_pushed(workspace, repos):
            await cleanup_repos(workspace, repos)
        else:
            logger.warning("git cleanup 跳过：仍有未上库提交 initiative=%s", initiative_id)
    return results


async def cleanup_repos(workspace: Path, repos: list[RepoConfig] | None = None) -> None:
    """push 全部成功后删除工作副本（保留沙盒目录本身）。

    ``repos`` 非空时只删这些仓库目录：并行波次里另一个阶段可能正在使用别的
    checkout，整目录 rmtree 会把它们一起删掉。
    """
    from sortie_deck.plugins.coding import _repo_dirname

    repos_root = workspace / "repos"
    targets = [repos_root / _repo_dirname(r) for r in repos] if repos else [repos_root]
    for target in targets:
        if not target.exists():
            continue
        try:
            await asyncio.to_thread(shutil.rmtree, target)
            logger.info("工作副本已回收（push 完成）: %s", target)
        except asyncio.CancelledError:
            raise
        except (OSError, SystemExit) as exc:
            # 清理是收尾动作，失败不允许影响阶段结果：除 OSError 外，运行环境
            # （如 IDE 的批量删除守卫）会在 rmtree 里 raise SystemExit——它是
            # BaseException，继续上抛会穿过阶段与 LangGraph 直接杀掉进程
            # （2026-10-27 事故），因此这里一并吞掉，只记 warning。
            logger.warning("工作副本回收失败 %s: %s", target, exc)
    # 本阶段的 checkout 全部清掉后，连空的 repos/ 一起回收；还有别的阶段在用时
    # 目录非空，rmdir 会失败并被忽略。
    with contextlib.suppress(OSError):
        repos_root.rmdir()


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
    *,
    bound_ids: set[str] | None = None,
    archive_max_mb: int = 500,
) -> list[str]:
    """TTL 兜底扫描：删除非运行中任务遗留的 workspace（进程崩溃残留等）。

    - 运行中（active_ids）任务跳过；
    - 脏状态（未提交/未推送）用 dirty_ttl，否则用 ttl；
    - 回收前把未绑定仓库任务（不在 bound_ids）的产出代码归档进 artifacts；
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
        if bound_ids is None or ini_dir.name not in bound_ids:
            try:
                from sortie_deck.workspace import archive_repos_snapshot

                archive_repos_snapshot(ws, ini_dir / "artifacts", max_mb=archive_max_mb)
            except Exception as exc:  # noqa: BLE001 — 归档失败不阻断回收
                logger.warning("sweep 归档失败 ini=%s: %s", ini_dir.name, exc)
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
