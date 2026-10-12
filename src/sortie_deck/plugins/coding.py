from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from sortie_deck.models import RepoConfig, StageContext, StageResult

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


async def _run_cmd(args: list[str], cwd: Path, timeout: int | None = None) -> tuple[int, str, str]:
    timeout = timeout if timeout is not None else _coding_timeout()
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "CI": "1"},  # nudge CLIs toward non-interactive
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", f"Timeout after {timeout}s"
    return proc.returncode or 0, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode(
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
    repos: list[RepoConfig], workspace: Path
) -> tuple[str, list[str]]:
    """Clone each mission repo into ``workspace/repos/<name>`` before coding.

    Returns ``(prompt_section, log_lines)``. Already-present checkouts are
    kept (idempotent on retry); clone failures are logged but do not abort the
    stage — the agent is told which checkouts exist.
    """
    if not repos:
        return "", []
    repos_root = workspace / "repos"
    repos_root.mkdir(parents=True, exist_ok=True)
    log: list[str] = [f"mission_repos={len(repos)}"]
    entries: list[str] = []
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
            log.append(f"repo {target.name}: git not available, clone skipped")
        if (target / ".git").exists():
            entries.append(f"- `{target}` (branch: {display})")
        else:
            entries.append(f"- `{target}` (branch: {display}) — CHECKOUT FAILED, see log")
    section = (
        "\n\nMission code repositories (read these first, then iterate on them):\n"
        + "\n".join(entries)
        + "\nExplore the code, implement the PRD changes inside these checkouts.\n"
    )
    return section, log





def ensure_worktree(repo_root: Path, initiative_id: str, worktrees_root: Path) -> Path:
    """Create an isolated work directory (git worktree when possible)."""
    worktrees_root.mkdir(parents=True, exist_ok=True)
    target = worktrees_root / initiative_id
    if target.exists():
        return target

    git_dir = repo_root / ".git"
    if git_dir.exists() and _which("git"):
        branch = f"tdt/{initiative_id}"
        subprocess.run(
            ["git", "branch", branch],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        result = subprocess.run(
            ["git", "worktree", "add", str(target), branch],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return target

    target.mkdir(parents=True, exist_ok=True)
    (target / "README.md").write_text(
        f"# Workdir for {initiative_id}\nIsolated workspace (non-git fallback).\n"
        f"Coding agents may write freely under this directory.\n",
        encoding="utf-8",
    )
    return target


def _count_impl_files(workspace: Path) -> int:
    skip = {".git", "__pycache__", "node_modules", ".write_probe", "repos"}
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
        prd = ctx.upstream_artifacts.get("prd.md", ctx.brief)
        mcp_extra, mcp_log = await _mcp_prompt_and_log(ctx)
        repo_section, repo_log = await clone_repos(ctx.repos, workspace)
        mode = _permission_mode(ctx)
        prompt = (
            f"{ctx.persona}\n\n"
            f"You are running inside an isolated Sortie Deck worktree. You MAY create/edit files here.\n"
            f"Workspace (cwd): {workspace}\n"
            f"Write the implementation into this directory (e.g. index.html / src/).\n"
            f"Do not ask the user to approve writes — permission mode is `{mode}`.\n"
            f"Human instruction: {ctx.human_instruction or 'none'}\n\n"
            f"PRD:\n{prd}\n"
            f"{repo_section}"
            f"{mcp_extra}"
        )
        claude = _which("claude")
        log_parts: list[str] = []
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
            log_parts.append(f"cmd={' '.join(args[:4])} … --permission-mode {mode}")
            code, out, err = await _run_cmd(args, cwd=workspace)
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
        prd = ctx.upstream_artifacts.get("prd.md", ctx.brief)
        repo_section, repo_log = await clone_repos(ctx.repos, workspace)
        query = f"{ctx.brief}\n{prd}\n{ctx.human_instruction or ''}"
        kb_meta: dict[str, str] = {}
        try:
            from sortie_deck.knowledge_gateway import gateway_from_settings
            from sortie_deck.knowledge_pack import load_pack_for_cursor
            from sortie_deck.settings import settings as _settings

            gw = gateway_from_settings(_settings)
            kb_meta = load_pack_for_cursor(gw.store, workspace, query, gateway=gw)
        except Exception as exc:  # noqa: BLE001 — soft-fail knowledge load
            kb_meta = {"error": str(exc)}

        mcp_extra, mcp_log = await _mcp_prompt_and_log(ctx)
        kb_hint = ""
        if kb_meta.get("rule"):
            kb_hint = (
                f"\nKnowledge pack loaded for this mission.\n"
                f"- Follow Cursor rule: `{kb_meta['rule']}`\n"
                f"- Full pack: `{kb_meta.get('pack', '')}` "
                f"({kb_meta.get('hits', '0')} docs)\n"
            )
        prompt = (
            f"{ctx.persona}\n\nImplement against PRD in this isolated worktree.\n"
            f"Workspace: {workspace}\n"
            f"You may create/edit files here without asking for write approval.\n"
            f"Instruction: {ctx.human_instruction or 'none'}\n\n{prd}\n"
            f"{repo_section}"
            f"{kb_hint}"
            f"{mcp_extra}"
        )
        binary, prefix = _resolve_cursor_binary()
        log_parts: list[str] = []
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
            log_parts.append(f"cmd={' '.join(args[:4])}…")
            code, out, err = await _run_cmd(args, cwd=workspace)
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
