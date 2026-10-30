from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path

from sortie_deck import observability as obs
from sortie_deck.models import RepoConfig, StageContext, StageResult

logger = logging.getLogger(__name__)

# 这些 flag 后面跟的是凭据，入 span 前要打码
_SENSITIVE_FLAGS = {"--api-key", "--token", "--password", "--auth-token", "--secret"}
_MAX_COMMAND_CHARS = 200

_PERM_RE = re.compile(
    r"(haven'?t granted|permission(?:s)? blocker|approve the (?:file-)?write|write permission|"
    r"requested permissions to write|permission denied|needs? approval|"
    r"授权|尚未授予|权限被拒绝|需要.*批准)",
    re.IGNORECASE,
)

_AUTH_RE = re.compile(
    r"(authentication required|please run ['\"]?agent login|CURSOR_API_KEY|"
    r"not (?:logged|authenticated)|login required|unauthorized)",
    re.IGNORECASE,
)


async def _mcp_prompt_and_log(ctx: StageContext) -> tuple[str, str]:
    """When MCP runtime is on, list tools from bound MCP toolkit items and return prompt + log."""
    from sortie_deck.settings import settings

    if not settings.mcp_runtime or not ctx.toolkit_ids:
        return "", ""
    try:
        from sortie_deck.mcp_runtime import McpSessionManager, resolve_mcp_items
        from sortie_deck.orchestrator import _toolkit_store
    except Exception:
        return "", ""
    items = resolve_mcp_items(_toolkit_store, list(ctx.toolkit_ids))
    if not items:
        return "", "MCP runtime on but no MCP toolkit items bound\n"
    mgr = McpSessionManager()
    try:
        await mgr.open_for_items(items)
        manifest = mgr.tools_manifest()
        log = mgr.combined_log()
        extra = ""
        if manifest:
            if bool(getattr(settings, "mcp_call_enabled", False)):
                note = (
                    "You may request a call: emit "
                    '`StageResult.meta["mcp_calls"] = [{"tool": "...", "arguments": {...}}]` '
                    "and the orchestrator will execute it (whitelist + write confirmation apply)."
                )
            else:
                note = "Tool invocation is disabled — treat this as a read-only manifest."
            extra = "\n\nAvailable MCP tools:\n" f"{manifest}\n" f"({note})\n"
        return extra, log
    finally:
        await mgr.close_all()


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _coding_timeout() -> int:
    from sortie_deck.settings import settings

    return int(getattr(settings, "coding_timeout_seconds", 900) or 900)


async def _artifact_brief_section(ctx: StageContext) -> tuple[str, str]:
    """coding CLI 的上游产物段落：与 harness 共用「摘要 + 路径」口径。

    返回 (prompt 段落, 日志行)；任何异常都回落旧的 PRD 全文，不影响阶段执行。
    """
    from sortie_deck.settings import settings

    fallback = f"PRD:\n{ctx.upstream_artifacts.get('prd.md', ctx.brief)}\n"
    if not settings.prompt_artifact_briefs or not ctx.upstream_artifacts:
        return fallback, ""

    try:
        from sortie_deck.artifact_summary import build_artifact_briefs, render_artifact_briefs
        from sortie_deck.mission_memory import get_mission_memory

        briefs = await build_artifact_briefs(
            ctx.upstream_artifacts,
            ctx.upstream_paths,
            mission_id=ctx.initiative_id,
            max_tokens=settings.prompt_artifact_summary_max_tokens,
            inline_chars=settings.prompt_inline_artifact_chars,
            use_llm=settings.prompt_artifact_summary_llm,
            timeout=settings.prompt_artifact_summary_timeout,
            memory_dir=get_mission_memory().memory_dir(ctx.initiative_id),
        )
    except Exception:  # noqa: BLE001 — 摘要失败回落全文
        logger.warning(
            "coding 产物摘要失败，回落 PRD 全文: initiative=%s stage=%s",
            ctx.initiative_id,
            ctx.stage,
            exc_info=True,
        )
        return fallback, "artifact_briefs=failed"

    if not briefs:
        return fallback, ""
    section = (
        "Upstream artifacts（只给摘要与路径；需要细节时自行 Read 对应路径，"
        "不要凭文件名猜内容）:\n"
        + render_artifact_briefs(briefs)
        + "\n读取建议：只读与本阶段产出直接相关的 1-2 份，其余按需再取。\n"
    )
    return section, f"artifact_briefs={len(briefs)}"


def _compact_env() -> dict[str, str]:
    """coding CLI 子进程的上下文压缩窗口（与 harness 同一套配置）。"""
    from sortie_deck.settings import settings

    try:
        window = int(settings.harness_auto_compact_window or 0)
    except (TypeError, ValueError):
        return {}
    if window <= 0:
        return {}
    return {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(window)}


def _write_mcp_config(workspace: Path, ctx: StageContext) -> tuple[str, list[str]]:
    """把本阶段绑定的 MCP 落成 claude CLI 的 ``--mcp-config`` 文件。

    返回 (配置文件路径, 说明行)；strict 关闭 / 无绑定 / 写盘失败时都返回空路径，
    调用方据此退回 CLI 默认行为（读取用户级 MCP 配置）。
    """
    from sortie_deck.settings import settings

    if not settings.harness_mcp_strict:
        return "", ["MCP: strict 关闭，沿用 CLI 默认配置"]
    try:
        from sortie_deck.harness_providers.mcp_binding import mcp_servers_from_toolkit
        from sortie_deck.orchestrator import _toolkit_store
    except Exception:  # noqa: BLE001 — 工具箱不可用时按「无 MCP」处理
        logger.warning("coding MCP 绑定跳过：依赖不可用", exc_info=True)
        return "", ["MCP: 依赖不可用，未挂载"]

    servers, notes = mcp_servers_from_toolkit(_toolkit_store, list(ctx.toolkit_ids))
    path = workspace / ".tdt" / "mcp-config.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("MCP 配置文件写入失败（忽略）: %s", path, exc_info=True)
        return "", [*notes, "MCP: 配置写入失败，未挂载"]
    return str(path), notes


def _permission_mode(ctx: StageContext) -> str:
    from sortie_deck.settings import settings

    # HITL approve after permission block can force bypass for retry
    forced = (ctx.permissions or {}).get("coding_permission_mode") or (
        ctx.human_instruction or ""
    )
    if isinstance(forced, str) and "bypass" in forced.lower():
        return "bypassPermissions"
    mode = (getattr(settings, "coding_permission_mode", None) or "acceptEdits").strip()
    if mode not in {
        "acceptEdits",
        "auto",
        "bypassPermissions",
        "default",
        "dontAsk",
        "plan",
    }:
        mode = "acceptEdits"
    return mode


def _looks_like_permission_block(text: str) -> bool:
    return bool(_PERM_RE.search(text or ""))


def _looks_like_auth_block(text: str) -> bool:
    return bool(_AUTH_RE.search(text or ""))


def _safe_command(args: list[str]) -> str:
    """命令行转成可入 span 的字符串：敏感 flag 的值打码后截断。

    coding CLI 的 ``-p`` 参数是整段 prompt，截断后仍能看出调的是哪个 CLI；
    真正的输出在产物文件里，不需要也不应该把 stdout 塞进 trace。
    """
    parts: list[str] = []
    hide_next = False
    for arg in args:
        if hide_next:
            parts.append("***")
            hide_next = False
            continue
        parts.append(arg)
        if arg in _SENSITIVE_FLAGS:
            hide_next = True
    return obs.truncate(" ".join(parts), _MAX_COMMAND_CHARS)


async def _run_cmd(
    args: list[str],
    cwd: Path,
    timeout: int | None = None,
    *,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """执行子进程——coding executor 的唯一子进程出口（含 git diff 等辅助命令）。

    CLI 内部行为不可观测，能记的只有命令行、耗时、退出码；这三项足以回答
    「阶段慢在哪个命令」「exit=124 是超时还是任务本身失败」。
    """
    timeout = timeout if timeout is not None else _coding_timeout()
    with obs.span(
        "process.exec",
        **{
            "process.command": _safe_command(args),
            "process.cwd": str(cwd),
            "sortie.timeout_seconds": timeout,
        },
    ) as proc_span:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # CI=1 让 CLI 走非交互；extra_env 用于注入上下文压缩窗口等增量配置
            env={**os.environ, "CI": "1", **(extra_env or {})},
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            # 超时是「转成返回值的失败」（返回码 124 而非抛异常），必须显式标 ERROR
            obs.mark_failure(proc_span, f"timeout after {timeout}s")
            return 124, "", f"Timeout after {timeout}s"
        code = proc.returncode or 0
        obs.set_attributes(proc_span, {"sortie.exit_code": code})
        if code != 0:
            obs.mark_failure(proc_span, f"exit code {code}")
        return code, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode(
            "utf-8", errors="replace"
        )


async def _git_diff(workspace: Path, repo_root: Path, repos: list[RepoConfig] | None = None) -> str:
    if not _which("git"):
        return "# (git not available)\n"
    has_git = (workspace / ".git").exists() or (repo_root / ".git").exists()
    if not has_git:
        # Count files written even without git
        files = [p for p in workspace.rglob("*") if p.is_file() and p.name != "README.md"]
        if files:
            listing = "\n".join(f"+ {p.relative_to(workspace)}" for p in sorted(files)[:80])
            return f"# worktree files (no git)\n{listing}\n"
        return "# (no git repository)\n"
    _code, out, err = await _run_cmd(["git", "diff", "--no-ext-diff"], cwd=workspace, timeout=60)
    diff = out or err or "# (empty diff)\n"
    # Mission repos cloned under workspace/repos: collect per-repo diffs too
    for repo in repos or []:
        target = workspace / "repos" / _repo_dirname(repo)
        if not (target / ".git").exists():
            continue
        rcode, rout, rerr = await _run_cmd(
            ["git", "diff", "--no-ext-diff"], cwd=target, timeout=60
        )
        rdiff = rout or rerr or ""
        if rdiff.strip():
            diff += f"\n# diff in repo {target.name}\n{rdiff}\n"
    return diff


def _repo_dirname(repo: RepoConfig) -> str:
    """Checkout dir name for a repo: explicit name → url basename minus .git."""
    if repo.name:
        return repo.name
    base = repo.url.rstrip("/").split("/")[-1]
    if base.endswith(".git"):
        base = base[: -len(".git")]
    return base or "repo"


def _clone_url(repo: RepoConfig) -> str:
    """Insert the access token into https URLs (never log the result)."""
    if repo.token and repo.url.startswith("https://"):
        return repo.url.replace("https://", f"https://{repo.token}@", 1)
    return repo.url


async def clone_repos(
    repos: list[RepoConfig], workspace: Path, initiative_id: str = ""
) -> tuple[str, list[str]]:
    """Clone each mission repo into ``workspace/repos/<name>`` before coding.

    Returns ``(prompt_section, log_lines)``. Already-present checkouts are
    kept (idempotent on retry); clone failures are logged but do not abort the
    stage — the agent is told which checkouts exist.

    任务分支策略：clone 成功后基于目标分支 HEAD 创建任务分支
    ``sortie/<initiative_id>``（多仓库 ``sortie/<id>-<name>``，见
    git_publish.mission_branch），agent 在任务分支上迭代，push 后建 MR。
    分支幂等：已存在则复用（重试场景）。

    磁盘水位防护：可用空间低于 ``TDT_MIN_DISK_FREE_MB`` 时跳过 clone 并在
    prompt/log 中说明，防止极端并发打爆服务器磁盘。
    """
    if not repos:
        return "", []
    repos_root = workspace / "repos"
    repos_root.mkdir(parents=True, exist_ok=True)
    log: list[str] = [f"mission_repos={len(repos)}"]

    # 磁盘水位防护（云上多用户安全阀）
    from sortie_deck.git_publish import mission_branch
    from sortie_deck.settings import settings as _settings

    free_mb = -1
    try:
        import shutil as _shutil

        free_mb = int(_shutil.disk_usage(str(workspace)).free / (1024 * 1024))
    except OSError:
        pass
    min_free = int(getattr(_settings, "min_disk_free_mb", 0) or 0)
    if min_free > 0 and 0 <= free_mb < min_free:
        log.append(f"DISK GUARD: free={free_mb}MB < min_disk_free_mb={min_free}MB, clone skipped")
        section = (
            "\n\nMission code repositories: NOT AVAILABLE this run — the server is low on "
            f"disk (free {free_mb}MB < required {min_free}MB). Clones were skipped; do not "
            "attempt file writes into repos/ and report this limitation.\n"
        )
        return section, log

    entries: list[str] = []
    branch_notes: list[str] = []
    for repo in repos:
        target = repos_root / _repo_dirname(repo)
        display = repo.branch or "(default branch)"
        if (target / ".git").exists():
            log.append(f"repo {target.name}: reuse existing checkout")
        elif _which("git"):
            args = ["git", "clone", "--depth", "50"]
            if repo.branch:
                args += ["--branch", repo.branch, "--single-branch"]
            args += [_clone_url(repo), str(target)]
            code, out, err = await _run_cmd(args, cwd=workspace)
            log.append(f"repo {target.name}: clone exit={code}")
            if code != 0:
                log.append(f"clone stderr={(err or out)[:500]}")
            else:
                # clone 成功后把 origin 换成无 token 的干净 URL，避免凭证落盘 .git/config
                #（push 时用带 token 的完整 URL，见 git_publish._push_branch）
                await _run_cmd(["git", "remote", "set-url", "origin", repo.url], cwd=target, timeout=15)
        else:
            log.append(f"repo {target.name}: git not available, clone skipped")
        if (target / ".git").exists():
            # 任务分支：基于目标分支 HEAD 创建（幂等，已存在则复用）
            task_branch = mission_branch(initiative_id, _repo_dirname(repo), multi_repo=len(repos) > 1)
            code, _, err = await _run_cmd(
                ["git", "rev-parse", "--verify", task_branch], cwd=target, timeout=15
            )
            if code == 0:
                await _run_cmd(["git", "checkout", task_branch], cwd=target, timeout=15)
                log.append(f"repo {target.name}: task branch reused={task_branch}")
            else:
                code, _, cerr = await _run_cmd(
                    ["git", "checkout", "-b", task_branch], cwd=target, timeout=15
                )
                log.append(
                    f"repo {target.name}: task branch created={task_branch} exit={code}"
                    + (f" stderr={cerr[:200]}" if code != 0 else "")
                )
            entries.append(f"- `{target}` (base: {display}, working branch: `{task_branch}`)")
            branch_notes.append(f"{target.name}: `{task_branch}`")
        else:
            entries.append(f"- `{target}` (branch: {display}) — CHECKOUT FAILED, see log")
    section = (
        "\n\nMission code repositories (read these first, then iterate on them):\n"
        + "\n".join(entries)
        + "\nExplore the code, implement the PRD changes inside these checkouts.\n"
    )
    if branch_notes:
        section += (
            "\nIMPORTANT — you are working on the mission's task branch; commit freely:\n"
            + "\n".join(f"- {note}" for note in branch_notes)
            + "\n"
        )
    return section, log





def ensure_worktree(repo_root: Path, initiative_id: str, worktrees_root: Path) -> Path:
    """创建任务沙盒目录（纯普通目录，git worktree 语义已移除）。

    历史行为是从服务器自身仓库做 ``git worktree add`` + ``tdt/<id>`` 分支——
    与用户业务项目无关且在云上多用户下有并发竞态，已废弃。业务代码统一走
    mission repos 的 per-task clone（``workspace/repos/<name>``，由 ``clone_repos``
    落在沙盒下），沙盒本身只是编码 agent 的工作目录。

    repo_root 参数保留仅为兼容旧调用签名，不再参与任何 git 操作。
    路径布局：``worktrees_root/<initiative_id>/workspace``（worktrees_root 传 missions 根）。
    """
    target = worktrees_root / initiative_id / "workspace"
    target.mkdir(parents=True, exist_ok=True)
    readme = target / "README.md"
    if not readme.exists():
        readme.write_text(
            f"# Workspace for {initiative_id}\n"
            f"Isolated mission sandbox. Mission repos are cloned under `repos/`.\n"
            f"Branch data lives on the remote git server (GitLab); this directory is a\n"
            f"disposable working copy and may be reclaimed after a successful push.\n",
            encoding="utf-8",
        )
    return target


def _count_impl_files(workspace: Path) -> int:
    # .cursor / .sortie-deck 是知识包注入产物，不是 agent 写的实现文件；
    # 不排除会让「exit=0 但没写代码」的失败判定永远不触发。
    skip = {
        ".git",
        "__pycache__",
        "node_modules",
        ".write_probe",
        "repos",
        ".cursor",
        ".sortie-deck",
    }
    n = 0
    for p in workspace.rglob("*"):
        if not p.is_file():
            continue
        if any(part in skip for part in p.parts):
            continue
        if p.name == "README.md":
            continue
        n += 1
    return n


def _resolve_cursor_binary() -> tuple[str | None, list[str]]:
    """Return (binary, prefix_args) for Cursor Agent CLI.

    Only `cursor-agent` / `agent` count. The IDE shim `cursor` (→ code) is NOT
    a headless Agent CLI and will hang or no-op on `-p` — treat as missing.
    """
    for name in ("cursor-agent", "agent"):
        path = _which(name)
        if path:
            return path, []
    return None, []


class ClaudeCodeExecutor:
    """Adapter for Claude Code CLI (`claude`) with worktree write permissions."""

    name = "claude_code"

    def __init__(self, repo_root: Path, worktrees_root: Path) -> None:
        self.repo_root = repo_root
        self.worktrees_root = worktrees_root

    def probe(self) -> dict[str, str]:
        claude = _which("claude")
        if not claude:
            return {"status": "missing", "detail": "claude not on PATH"}
        from sortie_deck.settings import settings

        mode = getattr(settings, "coding_permission_mode", "acceptEdits")
        return {"status": "ok", "detail": f"{claude} · permission-mode={mode}"}

    async def run(self, ctx: StageContext) -> StageResult:
        workspace = ensure_worktree(self.repo_root, ctx.initiative_id, self.worktrees_root)
        artifact_section, brief_log = await _artifact_brief_section(ctx)
        mcp_extra, mcp_log = await _mcp_prompt_and_log(ctx)
        repo_section, repo_log = await clone_repos(ctx.repos, workspace, ctx.initiative_id)
        mode = _permission_mode(ctx)
        prompt = (
            f"{ctx.persona}\n\n"
            f"You are running inside an isolated Sortie Deck worktree. You MAY create/edit files here.\n"
            f"Workspace (cwd): {workspace}\n"
            f"Write the implementation into this directory (e.g. index.html / src/).\n"
            f"Do not ask the user to approve writes — permission mode is `{mode}`.\n"
            f"Human instruction: {ctx.human_instruction or 'none'}\n\n"
            f"{artifact_section}"
            f"{repo_section}"
            f"{mcp_extra}"
        )
        claude = _which("claude")
        log_parts: list[str] = []
        if brief_log:
            log_parts.append(brief_log)
        if repo_log:
            log_parts.append("\n".join(repo_log))
        if mcp_log:
            log_parts.append(mcp_log)
        ok = False
        needs_perm = False
        if claude:
            args = [
                claude,
                "-p",
                prompt,
                "--output-format",
                "text",
                "--permission-mode",
                mode,
                "--add-dir",
                str(workspace),
            ]
            if mode == "bypassPermissions":
                args.append("--dangerously-skip-permissions")
            # 严格模式：只挂本阶段绑定的 MCP，切断用户级 mcpServers 的全量注入
            mcp_config, mcp_notes = _write_mcp_config(workspace, ctx)
            if mcp_config:
                args.extend(["--mcp-config", mcp_config, "--strict-mcp-config"])
            if mcp_notes:
                log_parts.append("MCP: " + "; ".join(mcp_notes))
            log_parts.append(f"cmd={' '.join(args[:4])} … --permission-mode {mode}")
            code, out, err = await _run_cmd(args, cwd=workspace, extra_env=_compact_env())
            combined = f"{out}\n{err}"
            log_parts.append(f"exit={code}\n{combined[:3500]}")
            needs_perm = _looks_like_permission_block(combined)
            wrote = _count_impl_files(workspace)
            ok = code == 0 and not needs_perm and (wrote > 0 or "DONE" in combined.upper())
            if code == 0 and wrote == 0 and not needs_perm:
                # Succeeded textually but no files — treat as soft fail for Teams loop
                ok = False
                log_parts.append("WARN: exit=0 but worktree has no implementation files")
        else:
            msg = (
                "Claude Code CLI (`claude`) not found on PATH. "
                "Install from https://docs.anthropic.com/en/docs/claude-code and retry."
            )
            log_parts.append(msg)

        diff = await _git_diff(workspace, self.repo_root, ctx.repos)
        tasks = {
            "tasks": [
                {
                    "id": "T1",
                    "title": "Claude Code implementation",
                    "status": "done" if ok else ("needs_permission" if needs_perm else "failed"),
                }
            ],
            "workspace": str(workspace),
            "permission_mode": mode,
            "files_written": _count_impl_files(workspace),
            "repos": [
                {"url": r.url, "branch": r.branch or "default", "name": _repo_dirname(r)}
                for r in ctx.repos
            ],
        }
        implementation = f"""# Claude Code run

Workspace: `{workspace}`
Available CLI: `{bool(claude)}`
Permission mode: `{mode}`

## Log
{chr(10).join(log_parts)[:4000]}
"""
        if needs_perm:
            return StageResult(
                status="needs_hitl",
                message=(
                    "编码 Agent 需要写 worktree 权限。"
                    "请在门禁点「放行」以 bypass 权限重试，或在本机 Claude 设置中信任该目录。"
                ),
                artifacts={
                    "implementation.md": implementation,
                    "tasks.json": json.dumps(tasks, ensure_ascii=False, indent=2),
                    "diff.patch": diff,
                },
                proposed_next=ctx.stage,
                meta={
                    "workspace": str(workspace),
                    "permission_block": True,
                    "permission_mode": mode,
                    "retry_permission_mode": "bypassPermissions",
                },
            )

        fail_message = (
            "Claude Code CLI not found; install `claude` and retry"
            if not claude
            else "Claude Code 未写出代码或执行失败（见 implementation.md）"
        )
        return StageResult(
            status="ok" if ok else "fail",
            message="Claude Code stage finished" if ok else fail_message,
            artifacts={
                "implementation.md": implementation,
                "tasks.json": json.dumps(tasks, ensure_ascii=False, indent=2),
                "diff.patch": diff,
            },
            proposed_next="qa_verify" if ok else ctx.stage,
            meta={"workspace": str(workspace), "permission_mode": mode},
        )


class CursorCliExecutor:
    """Adapter for Cursor Agent CLI (`cursor-agent`, `agent`, or `cursor agent`)."""

    name = "cursor_cli"

    def __init__(self, repo_root: Path, worktrees_root: Path) -> None:
        self.repo_root = repo_root
        self.worktrees_root = worktrees_root

    def probe(self) -> dict[str, str]:
        binary, prefix = _resolve_cursor_binary()
        if not binary:
            ide = _which("cursor")
            detail = (
                "cursor-agent / agent not on PATH. "
                "Install: curl -sS https://cursor.com/install | bash "
                "(IDE `cursor` alone is not enough)."
            )
            if ide:
                detail += f" Found IDE shim at {ide}."
            return {"status": "missing", "detail": detail}
        label = " ".join([binary, *prefix]).strip()
        from sortie_deck.llm import _cursor_agent_ready

        if not _cursor_agent_ready():
            return {
                "status": "auth_required",
                "detail": f"{label} · run `cursor-agent login` or set CURSOR_API_KEY",
            }
        return {"status": "ok", "detail": label}

    async def run(self, ctx: StageContext) -> StageResult:
        workspace = ensure_worktree(self.repo_root, ctx.initiative_id, self.worktrees_root)
        # prd 仅用于知识库检索（检索需要原文），prompt 里走「摘要 + 路径」
        prd = ctx.upstream_artifacts.get("prd.md", ctx.brief)
        artifact_section, brief_log = await _artifact_brief_section(ctx)
        repo_section, repo_log = await clone_repos(ctx.repos, workspace, ctx.initiative_id)
        query = f"{ctx.brief}\n{prd}\n{ctx.human_instruction or ''}"
        kb_meta: dict[str, str] = {}
        try:
            from sortie_deck.knowledge_gateway import gateway_from_settings
            from sortie_deck.knowledge_pack import load_pack_for_cursor
            from sortie_deck.settings import settings as _settings

            gw = gateway_from_settings(_settings)
            # 知识包除 workspace（供 Cursor 自动加载）外，再归档一份到任务目录，
            # 否则 workspace 回收后无法追溯「当时给 agent 灌了什么」。
            mission_root = (
                Path(ctx.data_dir) / "missions" / ctx.initiative_id if ctx.data_dir else None
            )
            kb_meta = load_pack_for_cursor(
                gw.store, workspace, query, gateway=gw, mission_root=mission_root, stage=ctx.stage
            )
        except Exception as exc:  # noqa: BLE001 — soft-fail knowledge load
            kb_meta = {"error": str(exc)}

        mcp_extra, mcp_log = await _mcp_prompt_and_log(ctx)
        inputs_hint = ""
        if ctx.inputs:
            from sortie_deck.mission_input import render_input_index

            index_block = render_input_index(ctx.inputs)
            if index_block:
                inputs_hint = "\n" + index_block
        kb_hint = ""
        if kb_meta.get("rule"):
            kb_hint = (
                f"\nKnowledge pack loaded for this mission.\n"
                f"- Follow Cursor rule: `{kb_meta['rule']}`\n"
                f"- Full pack: `{kb_meta.get('pack', '')}` "
                f"({kb_meta.get('hits', '0')} docs)\n"
            )
            if kb_meta.get("archive"):
                kb_hint += f"- Mission archive: `{kb_meta['archive']}`\n"
        prompt = (
            f"{ctx.persona}\n\nImplement against PRD in this isolated worktree.\n"
            f"Workspace: {workspace}\n"
            f"You may create/edit files here without asking for write approval.\n"
            f"Instruction: {ctx.human_instruction or 'none'}\n\n{artifact_section}"
            f"{inputs_hint}"
            f"{repo_section}"
            f"{kb_hint}"
            f"{mcp_extra}"
        )
        binary, prefix = _resolve_cursor_binary()
        log_parts: list[str] = []
        if brief_log:
            log_parts.append(brief_log)
        if repo_log:
            log_parts.append("\n".join(repo_log))
        if kb_meta:
            log_parts.append(f"knowledge_pack={kb_meta}")
        if mcp_log:
            log_parts.append(mcp_log)
        ok = False
        needs_perm = False
        needs_auth = False
        if binary:
            args = [binary, *prefix, "-p", prompt]
            # cursor CLI 没有 --mcp-config，严格模式下本阶段就是「无 MCP」
            log_parts.append("MCP: cursor CLI 不支持 --mcp-config，本阶段未挂载")
            log_parts.append(f"cmd={' '.join(args[:4])}…")
            code, out, err = await _run_cmd(args, cwd=workspace, extra_env=_compact_env())
            combined = f"{out}\n{err}"
            log_parts.append(f"exit={code}\n{combined[:3500]}")
            needs_auth = _looks_like_auth_block(combined)
            needs_perm = (not needs_auth) and _looks_like_permission_block(combined)
            wrote = _count_impl_files(workspace)
            ok = code == 0 and not needs_perm and not needs_auth and wrote > 0
            if code == 0 and wrote == 0 and not needs_perm and not needs_auth:
                ok = False
                log_parts.append("WARN: exit=0 but worktree has no implementation files")
        else:
            log_parts.append(
                "Cursor Agent CLI not found. Install with:\n"
                "  curl -sS https://cursor.com/install | bash\n"
                "Ensure `cursor-agent` or `agent` is on PATH (IDE `cursor` alone is insufficient)."
            )
        log = "\n".join(log_parts)

        diff = await _git_diff(workspace, self.repo_root, ctx.repos)
        task_status = "done" if ok else (
            "needs_auth" if needs_auth else ("needs_permission" if needs_perm else "failed")
        )
        tasks = {
            "tasks": [
                {
                    "id": "T1",
                    "title": "Cursor CLI implementation",
                    "status": task_status,
                }
            ],
            "workspace": str(workspace),
            "files_written": _count_impl_files(workspace),
            "repos": [
                {"url": r.url, "branch": r.branch or "default", "name": _repo_dirname(r)}
                for r in ctx.repos
            ],
        }
        implementation = f"""# Cursor CLI run

Workspace: `{workspace}`
CLI: `{binary}` {' '.join(prefix)}

## Log
{log[:4000]}
"""
        if needs_auth:
            return StageResult(
                status="needs_hitl",
                message=(
                    "Cursor Agent 未登录。"
                    "请在本机运行 `cursor-agent login`（或设置 CURSOR_API_KEY），"
                    "完成后在 Teams 回复 `/approve` 重试；也可改用 Claude Code。"
                ),
                artifacts={
                    "implementation.md": implementation,
                    "tasks.json": json.dumps(tasks, ensure_ascii=False, indent=2),
                    "diff.patch": diff,
                },
                proposed_next=ctx.stage,
                meta={
                    "workspace": str(workspace),
                    "permission_block": True,  # reuse Teams permission HITL copy + retry
                    "auth_block": True,
                    "retry_permission_mode": "bypassPermissions",
                    "knowledge_pack": kb_meta,
                },
            )
        if needs_perm:
            return StageResult(
                status="needs_hitl",
                message="Cursor Agent 需要写 worktree 权限。请在门禁放行后重试，或检查 Cursor CLI 信任目录。",
                artifacts={
                    "implementation.md": implementation,
                    "tasks.json": json.dumps(tasks, ensure_ascii=False, indent=2),
                    "diff.patch": diff,
                },
                proposed_next=ctx.stage,
                meta={
                    "workspace": str(workspace),
                    "permission_block": True,
                    "retry_permission_mode": "bypassPermissions",
                    "knowledge_pack": kb_meta,
                },
            )

        fail_message = (
            "Cursor CLI not found; install cursor-agent and retry"
            if not binary
            else "Cursor Agent 未写出代码或执行失败（见 implementation.md）"
        )
        return StageResult(
            status="ok" if ok else "fail",
            message="Cursor CLI stage finished" if ok else fail_message,
            artifacts={
                "implementation.md": implementation,
                "tasks.json": json.dumps(tasks, ensure_ascii=False, indent=2),
                "diff.patch": diff,
            },
            proposed_next="qa_verify" if ok else ctx.stage,
            meta={"workspace": str(workspace), "knowledge_pack": kb_meta},
        )
