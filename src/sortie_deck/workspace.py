"""Local workspace / worktree helpers for mission inspection & OS reveal."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Any

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
        if any(part in _SKIP_DIRS for part in rel.parts):
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
    """Open path in the system file manager (folder) or reveal file."""
    target = path.resolve()
    system = platform.system()
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
