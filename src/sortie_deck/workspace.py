"""Local workspace / worktree helpers for mission inspection & OS reveal."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any


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
