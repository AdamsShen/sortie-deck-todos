"""影子仓库：给「没有 git 仓库」的 mission 提供阶段级改动基线。

为什么需要：未绑定 git 的任务，编码 agent 把代码写在 ``workspace/repos/<name>/``，
``_git_diff`` 拿不到任何 diff，只能退化成文件清单（无增删行）。多镜头自审于是
只能靠逐个 ``Read`` 文件了解改动，实测 24 个文件 × 约 60s/步，两轮 480s 预算全部
超时（样本任务 ini_b93e23b70190）。

做法：在 mission 目录下维护一个独立的 ``.shadow.git``，用 ``GIT_DIR`` +
``GIT_WORK_TREE`` 指向 workspace，把它当纯粹的 diff 引擎：
- 阶段开始（clone 之后、agent 首轮编辑之前）提交一次 baseline；
- 阶段结束 ``git add -A`` 后 ``git diff --cached HEAD``，得到标准 unified diff。

隔离约束（重要）：
1. ``GIT_DIR`` / ``GIT_WORK_TREE`` 只注入单次子进程的 env，绝不写 ``os.environ``，
   否则 agent 自己执行 ``git status`` 会看到影子仓；
2. 影子仓放在 ``workspace.parent``（mission 根）下，不在 workspace 内，不会被
   agent 写入、也不会进 diff；
3. ``workspace`` 自身是 git 仓库根时直接放弃（交给 ``_git_diff`` 第 1 路），
   ``repos/<name>`` 已存在 ``.git`` 的子目录写进 exclude（交给第 3 路）；
4. 任何异常都内部降级为「不可用」，由调用方回退文件清单，不阻断阶段。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# 影子仓在 mission 根目录下的固定名字（workspace 的兄弟目录）
_SHADOW_DIRNAME = ".shadow.git"

# 静态排除：依赖 / 缓存 / 编排层注入目录 / 二进制，都不是代码审查对象。
# 二进制进 diff 只会撑爆 prompt，且 review 无法给出有效结论。
_STATIC_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".upstream/",
    ".archive/",
    ".runs/",
    ".sortie-deck/",
    ".write_probe/",
    ".cursor/",
    "node_modules/",
    "__pycache__/",
    ".venv/",
    "venv/",
    "dist/",
    "build/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "*.pyc",
    "*.pyo",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.ico",
    "*.pdf",
    "*.zip",
    "*.gz",
    "*.tar",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.mp4",
    "*.mov",
)

# 扫描嵌套 .git 时要跳过的目录名（避免 rglob 走进 node_modules 这类大目录）
_WALK_SKIP_PARTS = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)

_GIT_TIMEOUT = 60.0


class ShadowRepo:
    """mission 级影子仓库。

    同一 mission 的多个阶段共用一个 ``.shadow.git``，每次 ``ensure()`` 都会把当时
    的工作区状态提交为新的 baseline，因此 ``capture()`` 得到的始终是「本阶段改动」。
    """

    def __init__(self, workspace: Path, *, timeout: float = _GIT_TIMEOUT) -> None:
        self.workspace = Path(workspace)
        # workspace 形如 <mission_root>/workspace，影子仓与之平级，不进 workspace
        self.git_dir = self.workspace.parent / _SHADOW_DIRNAME
        self.timeout = timeout

    # --- 对外接口 ---------------------------------------------------------

    async def ensure(self) -> bool:
        """创建影子仓并提交 baseline；返回是否可用。

        已存在时等价于「重新基线」：把当前工作区快照提交为新的 HEAD，这样即使
        同一 mission 的上一阶段留下文件，也不会被算进本阶段 diff。
        """
        if not _enabled():
            return False
        if not shutil.which("git"):
            logger.info("影子仓跳过：git 不在 PATH")
            return False
        if await self._workspace_is_git_root():
            # 项目自己有仓库：交给 _git_diff 第 1 路，不介入
            return False
        try:
            self.git_dir.mkdir(parents=True, exist_ok=True)
            code, _, err = await self._run(["git", "init", "-q"])
            if code != 0:
                logger.warning("影子仓 init 失败 workspace=%s: %s", self.workspace, err[:300])
                return False
            await self._sync_exclude()
            code, _, err = await self._add_all()
            if code != 0:
                logger.warning("影子仓 baseline add 失败 workspace=%s: %s", self.workspace, err[:300])
                return False
            # --allow-empty：空目录也要有 HEAD，否则 capture 无从比较
            code, _, err = await self._run(
                [
                    "git",
                    "-c",
                    "user.name=Sortie Deck",
                    "-c",
                    "user.email=sortie@local",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "--no-verify",
                    "-m",
                    "baseline",
                ]
            )
            if code != 0:
                logger.warning("影子仓 baseline commit 失败 workspace=%s: %s", self.workspace, err[:300])
                return False
            return True
        except Exception:  # noqa: BLE001 — 影子仓是旁路能力，绝不阻断阶段
            logger.warning("影子仓初始化异常 workspace=%s", self.workspace, exc_info=True)
            return False

    async def capture(self) -> str | None:
        """采集本阶段 diff。

        返回 ``str`` 表示成功（空串 = 确实没有改动）；返回 ``None`` 表示影子仓不可用，
        调用方应回退到文件清单。
        """
        if not (self.git_dir / "HEAD").exists():
            return None
        try:
            await self._sync_exclude()
            code, _, err = await self._add_all()
            if code != 0:
                logger.warning("影子仓 add 失败 workspace=%s: %s", self.workspace, err[:300])
                return None
            code, out, err = await self._run(
                ["git", "diff", "--cached", "--no-ext-diff", "--no-color", "HEAD"]
            )
            if code != 0:
                logger.warning("影子仓 diff 失败 workspace=%s: %s", self.workspace, err[:300])
                return None
            return out
        except Exception:  # noqa: BLE001
            logger.warning("影子仓采集异常 workspace=%s", self.workspace, exc_info=True)
            return None

    async def reset(self) -> None:
        """删除影子仓（mission 清理或测试收尾）。"""
        try:
            shutil.rmtree(self.git_dir, ignore_errors=True)
        except OSError:
            logger.debug("影子仓清理失败 path=%s", self.git_dir)

    # --- 内部实现 ---------------------------------------------------------

    def _env(self) -> dict[str, str]:
        """只注入单次子进程的 git 环境，不改进程级 os.environ。"""
        return {
            "GIT_DIR": str(self.git_dir),
            "GIT_WORK_TREE": str(self.workspace),
            # 屏蔽用户全局/系统配置：避免 commit.gpgsign 或缺 user.email 导致失败
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }

    async def _run(self, args: list[str], *, clean_env: bool = False) -> tuple[int, str, str]:
        """执行 git；``clean_env`` 用于探测项目自身仓库（不能注入影子仓的 GIT_DIR）。"""
        if clean_env:
            env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        else:
            env = {**os.environ, **self._env()}
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(self.workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return 124, "", f"timeout after {self.timeout:.0f}s"
        return (
            int(proc.returncode or 0),
            out_b.decode("utf-8", "replace"),
            err_b.decode("utf-8", "replace"),
        )

    async def _workspace_is_git_root(self) -> bool:
        """workspace 自身是否已是一个 git 仓库根（含「嵌在父仓库里」的否定判断）。

        必须用干净环境探测：影子仓建好后若仍注入 ``GIT_DIR``，``rev-parse`` 会把
        workspace 自己报成仓库根，下一阶段 ``ensure()`` 于是直接放弃、baseline 不前移，
        diff 会累计上一阶段的改动（2026-11-06 实测：阶段 2 期望 2 文件，实际 24 文件）。
        """
        # 先做零成本的文件判断：绝大多数情况直接排除
        if (self.workspace / ".git").exists():
            return True
        try:
            code, out, _ = await self._run(
                ["git", "rev-parse", "--show-toplevel"], clean_env=True
            )
        except OSError:
            return False
        if code != 0:
            return False
        try:
            return Path(out.strip()).resolve() == self.workspace.resolve()
        except OSError:
            return False

    async def _add_all(self) -> tuple[int, str, str]:
        """``git add -A``，并用 pathspec 排除嵌套仓库目录。

        嵌套目录只走 pathspec、不写进 ``info/exclude``：整目录被 ignore 时
        ``git add -A -- .`` 会直接报错（``paths are ignored``），而 pathspec
        排除是静默跳过，且每次调用都重扫，能覆盖阶段中途才出现的 ``.git``。
        """
        args = ["git", "add", "-A", "--ignore-errors", "--", "."]
        for rel in self._nested_repo_dirs():
            args.append(f":(exclude){rel}")
        return await self._run(args)

    async def _sync_exclude(self) -> None:
        """重建 ``info/exclude``：只放静态段（依赖/缓存/二进制）。

        必须每次重建：agent 可能在阶段中途改动依赖目录，重建保证规则始终生效。
        """
        info_dir = self.git_dir / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        (info_dir / "exclude").write_text("\n".join(_STATIC_EXCLUDES) + "\n", encoding="utf-8")

    def _nested_repo_dirs(self) -> list[str]:
        """workspace 内所有含 ``.git`` 的子目录（相对路径），跳过依赖目录。"""
        found: list[str] = []
        for root, dirs, files in os.walk(self.workspace):
            root_path = Path(root)
            if ".git" in dirs or ".git" in files:
                rel = root_path.relative_to(self.workspace)
                if str(rel) != ".":
                    found.append(rel.as_posix())
            # 剪枝：依赖/缓存目录不可能是我们要找的嵌套仓库
            dirs[:] = [d for d in dirs if d not in _WALK_SKIP_PARTS]
        return sorted(set(found))[:50]


def _enabled() -> bool:
    from sortie_deck.settings import settings

    return bool(getattr(settings, "shadow_git_enabled", True))


async def ensure_shadow_baseline(workspace: Path) -> bool:
    """阶段开始打 baseline 的唯一入口（供 clone_repos 调用）。"""
    return await ShadowRepo(workspace).ensure()
