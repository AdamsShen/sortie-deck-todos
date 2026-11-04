"""Local workspace / worktree helpers for mission inspection & OS reveal."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class RevealUnavailable(RuntimeError):
    """运行服务的环境没有桌面文件管理器，无法在 OS 里打开路径。

    典型场景：Sortie Deck 部署在云端容器（无 DISPLAY、无 xdg-open），
    用户却在浏览器里点「打开 Worktree」。此时不存在可打开的窗口，
    由调用方降级为「把路径回传给前端复制」而不是抛 500。
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(reason)
        self.path = path
        self.reason = reason


def reveal_unavailable_reason() -> str:
    """返回当前环境无法在 OS 中打开路径的原因；能力可用时返回空串。

    浏览器与 API 进程可能不在同一台机器上（云端部署时 API 跑在容器里），
    因此以服务端自身环境判断，而不是以请求来源判断。
    """
    system = platform.system()
    if system in {"Darwin", "Windows"}:
        return ""
    # Linux 需要图形会话与 xdg-open 同时具备，否则 Popen 只会抛 FileNotFoundError
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "server has no graphical session (DISPLAY/WAYLAND_DISPLAY unset)"
    if shutil.which("xdg-open") is None:
        return "xdg-open is not installed on server"
    return ""

_SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".turbo",
    ".cache",
}


def list_files(root: Path, *, limit: int = 80, max_depth: int = 4) -> list[dict[str, Any]]:
    """Shallow file listing under root (relative paths), newest-ish order."""
    if not root.exists() or not root.is_dir():
        return []
    root = root.resolve()
    found: list[tuple[float, Path]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        # 隐藏目录（阶段产物目录下的 .runs / .upstream）不是交付物，索引里不展示；
        # 软链（workspace → 任务沙盒）也不能递归，否则会把代码仓库文件重复列一遍。
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if path.is_symlink():
            continue
        # 一级目录是软链（文档型阶段产物目录里的 workspace → 任务沙盒）时不展开，
        # 否则会把代码仓库文件当成档案产物重复列出。
        if rel.parts and (root / rel.parts[0]).is_symlink():
            continue
        if len(rel.parts) > max_depth:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        found.append((mtime, path))
        if len(found) >= limit * 3:
            break
    found.sort(key=lambda x: x[0], reverse=True)
    out: list[dict[str, Any]] = []
    for mtime, path in found[:limit]:
        rel = path.relative_to(root)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        out.append(
            {
                "path": str(rel).replace("\\", "/"),
                "name": path.name,
                "size": size,
                "mtime": mtime,
            }
        )
    return out


def resolve_git_repo(path: Path) -> Path | None:
    """Return the git toplevel for path, or None."""
    cur = path.resolve()
    if cur.is_file():
        cur = cur.parent
    # Prefer git rev-parse when available
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cur if cur.is_dir() else cur.parent,
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Walk parents for .git
    probe = cur if cur.is_dir() else cur.parent
    for parent in [probe, *probe.parents]:
        if (parent / ".git").exists():
            return parent
        if parent.parent == parent:
            break
    return None


def allowed_roots(data_dir: Path, repo_root: Path) -> list[Path]:
    roots = [data_dir.resolve(), repo_root.resolve()]
    # Dedupe while preserving order
    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def is_under_allowed(target: Path, roots: list[Path]) -> bool:
    resolved = target.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def reveal_in_os(path: Path) -> None:
    """Open path in the system file manager (folder) or reveal file.

    环境不具备该能力时抛 RevealUnavailable（而非 FileNotFoundError），
    便于 API 层区分「环境不支持」与「真故障」，并给前端可读的降级提示。
    """
    target = path.resolve()
    reason = reveal_unavailable_reason()
    if reason:
        raise RevealUnavailable(target, reason)
    system = platform.system()
    try:
        if system == "Darwin":
            if target.is_file():
                subprocess.Popen(["open", "-R", str(target)])  # noqa: S603
            else:
                subprocess.Popen(["open", str(target)])  # noqa: S603
        elif system == "Windows":
            if target.is_file():
                subprocess.Popen(["explorer", "/select,", str(target)])  # noqa: S603
            else:
                subprocess.Popen(["explorer", str(target)])  # noqa: S603
        else:
            folder = target if target.is_dir() else target.parent
            subprocess.Popen(["xdg-open", str(folder)])  # noqa: S603
    except OSError as exc:
        # 兜底：环境判断通过但拉起进程仍失败（权限、被裁剪的镜像等）
        raise RevealUnavailable(target, str(exc)) from exc


def mission_workspace(
    *,
    initiative_id: str,
    data_dir: Path,
    repo_root: Path,
) -> dict[str, Any]:
    """任务工作区视图（数据源为收敛后的任务目录 data/missions/<id>/）。

    输出字段名保持历史形状（artifacts_dir/worktree_dir/...），前端无需改动；
    语义变化：worktree_dir 现指向任务沙盒 `workspace/`（repos clone 挂其下，
    push 成功后可被回收），不再有 "git worktree" 含义——git_from_worktree
    仅在沙盒内恰好存在 clone 时为 True。
    """
    from sortie_deck.layout import mission_artifacts_dir, mission_workspace_dir

    art_dir = mission_artifacts_dir(data_dir, initiative_id)
    wt_dir = mission_workspace_dir(data_dir, initiative_id)
    wt_exists = wt_dir.is_dir()
    git_from_wt = resolve_git_repo(wt_dir) if wt_exists else None
    git_from_repo = resolve_git_repo(repo_root)
    git_repo = git_from_wt or git_from_repo
    return {
        "initiative_id": initiative_id,
        "artifacts_dir": str(art_dir),
        "artifacts_exists": art_dir.is_dir(),
        "worktree_dir": str(wt_dir),
        "worktree_exists": wt_exists,
        "git_repo": str(git_repo) if git_repo else None,
        "git_from_worktree": bool(git_from_wt),
        "artifact_files": list_files(art_dir, limit=100, max_depth=5) if art_dir.is_dir() else [],
        "worktree_files": list_files(wt_dir, limit=80, max_depth=4) if wt_exists else [],
    }


# 归档打包的排除目录：依赖/构建产物与平台注入物（.tdt=MCP 配置、.cursor /
# .sortie-deck=知识包）不是 agent 产出代码；隐藏目录（. 开头）整体跳过。
_ARCHIVE_SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".turbo",
    ".cache",
    ".tdt",
    ".cursor",
    ".sortie-deck",
}


def _collect_archive_files(base: Path, *, strip_repos: bool = False) -> list[tuple[Path, str]]:
    """收集归档文件：返回 (绝对路径, zip 内 arcname)，已按 arcname 排序。"""
    entries: list[tuple[Path, str]] = []
    for path in sorted(base.rglob("*")):
        # symlink 不进包：既避免软链循环，软链目标通常也不是产出代码
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(base)
        if any(part in _ARCHIVE_SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if strip_repos:
            # 兜底打包沙盒根时跳过 repos/ 自身（作为主体单独打包）与根级 README
            # （ensure_worktree 写的沙盒说明，非 agent 产出）
            if rel.parts[0] == "repos":
                continue
            if rel.name == "README.md" and len(rel.parts) == 1:
                continue
        entries.append((path, rel.as_posix()))
    entries.sort(key=lambda e: e[1])
    return entries


def archive_repos_snapshot(
    workspace_dir: Path,
    artifacts_dir: Path,
    *,
    max_mb: int = 500,
    bound: bool = False,
) -> dict[str, Any] | None:
    """任务沙盒回收前，把未绑定仓库任务的产出代码打包归档到 artifacts。

    只处理未绑定 git 仓库的任务：绑定任务的 repos/ 下是业务 clone（特征
    ``repos/<name>/.git``），代码已 push 远端分支，无需归档。未绑定任务约定
    agent 把代码写进 ``workspace/repos/``（见 plugins.coding.clone_repos），
    回收前整个 repos/ 打成 zip 落到 ``artifacts/code-archive/``，前端可下载。

    输入输出示例：workspace=data/missions/<id>/workspace，
    artifacts=data/missions/<id>/artifacts →
    {"status": "archived", "zip": "code-archive-20261103T080000Z.zip",
     "file_count": 12, "total_bytes": 45678, "truncated": False, ...}；
    绑定任务 → {"status": "skipped", "reason": "bound_repo"}。

    行为要点：
    - agent 不听话把文件写在沙盒根时，兜底打包根目录散落文件（source=workspace_root）；
    - 两者皆空（没跑到编码阶段的空沙盒）跳过，不打空包；
    - fingerprint 幂等：终态与 TTL sweep 重复触发不重复打包（zip 缺失时强制重打）；
    - 单包累计超过 max_mb 停止添加，manifest 标记 truncated。

    返回归档摘要 dict；workspace 不存在返回 None。
    """
    ws = Path(workspace_dir)
    if not ws.is_dir():
        return None
    artifacts_dir = Path(artifacts_dir)
    ini_id = ws.parent.name
    repos_root = ws / "repos"

    # 绑定判定：调用方元数据优先（push 成功后 clone 已被 cleanup_repos 删除，
    # 文件系统特征失效，必须靠元数据兜住）；再叠加 clone 特征双保险
    clone_exists = repos_root.is_dir() and any(
        (child / ".git").exists() for child in repos_root.iterdir() if child.is_dir()
    )
    if bound or clone_exists:
        return {"status": "skipped", "reason": "bound_repo", "initiative_id": ini_id}

    entries: list[tuple[Path, str]]
    if repos_root.is_dir():
        entries = [(p, f"repos/{arc}") for p, arc in _collect_archive_files(repos_root)]
        source = "repos"
    else:
        entries = []
        source = "workspace_root"
    if not entries:
        # 兜底：agent 未按约定写进 repos/（或没跑编码阶段），打包沙盒根散落文件
        entries = _collect_archive_files(ws, strip_repos=True)
        source = "workspace_root"
    if not entries:
        return {"status": "skipped", "reason": "empty", "initiative_id": ini_id}

    # fingerprint：路径 + 内容 sha1（mtime 在部分文件系统上只有秒级精度，
    # 同秒内内容变化会漏检）；与上次一致且 zip 仍在则跳过
    digest = hashlib.sha1()
    for path, arc in entries:
        content_hash = hashlib.sha1()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                content_hash.update(chunk)
        digest.update(f"{arc}\x00{content_hash.hexdigest()}\n".encode("utf-8"))
    fingerprint = digest.hexdigest()

    archive_dir = artifacts_dir / "code-archive"
    state_path = archive_dir / ".last-archive.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            prev_zip = str(state.get("zip") or "")
            if (
                state.get("fingerprint") == fingerprint
                and prev_zip
                and (archive_dir / prev_zip).is_file()
            ):
                return {
                    "status": "skipped",
                    "reason": "unchanged",
                    "initiative_id": ini_id,
                    "zip": prev_zip,
                }
        except (OSError, ValueError):
            pass  # 状态文件损坏按无历史处理，重打一次

    limit = max(int(max_mb), 1) * 1024 * 1024
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # 文件名带内容指纹短哈希：同秒内重复归档不互相覆盖，内容变则名变
    zip_name = f"code-archive-{stamp}-{fingerprint[:8]}.zip"
    zip_path = archive_dir / zip_name
    archive_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    written = 0
    truncated = False
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, arc in entries:
                size = path.stat().st_size
                if total + size > limit:
                    truncated = True
                    break
                zf.write(path, arc)
                total += size
                written += 1
    except OSError as exc:
        logger.warning("代码归档 zip 写入失败 ini=%s: %s", ini_id, exc)
        zip_path.unlink(missing_ok=True)
        return {"status": "skipped", "reason": f"zip_error: {exc}", "initiative_id": ini_id}

    manifest = {
        "initiative_id": ini_id,
        "archived_at": stamp,
        "zip": zip_name,
        "source": source,
        "file_count": written,
        "total_bytes": total,
        "truncated": truncated,
    }
    try:
        (archive_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        state_path.write_text(
            json.dumps({"fingerprint": fingerprint, "zip": zip_name}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        # zip 已落地，manifest 写失败只影响审计完整性，不算归档失败
        logger.warning("归档 manifest 写入失败 ini=%s: %s", ini_id, exc)
    logger.info(
        "任务代码归档: ini=%s zip=%s source=%s files=%d bytes=%d truncated=%s",
        ini_id,
        zip_name,
        source,
        written,
        total,
        truncated,
    )
    return {"status": "archived", **manifest}
