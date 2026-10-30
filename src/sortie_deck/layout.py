"""任务目录收敛：一个任务（initiative）一个根目录，对标 CDE 的 workspace 模式。

布局::

    data/missions/<initiative_id>/
    ├── memory/                   任务记忆 + 编码会话（MissionMemoryStore，历史上已在此）
    ├── artifacts/<stage>/        阶段交付产物（对用户展示；原 data/artifacts/<id>/）
    ├── input/                    任务输入快照（原件 + 抽取文本 + manifest + brief.md）
    ├── logs/<stage>/             阶段过程文件（回复全文/沟通记录/工具审计，不对外展示）
    ├── workspace/repos/<name>/   编码沙盒 + mission clone（原 data/worktrees/<id>/）
    └── chat/<file>.jsonl         任务群聊与规划官会话（原 data/rooms/<id>*.jsonl）

集中存储保持原位（initiatives 元数据、checkpoints、runs、RAG、toolkit/agents/users），
与业内 CDE/Agent 产品的 "workspace 即隔离单元 + 元数据集中管理" 一致。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_MISSION_SUBDIRS = ("artifacts", "input", "logs", "workspace", "chat", "memory")


def missions_root(data_dir: Path) -> Path:
    """所有任务目录的父目录：data/missions。"""
    return Path(data_dir) / "missions"


def mission_root(data_dir: Path, initiative_id: str) -> Path:
    """任务唯一根目录：data/missions/<initiative_id>。"""
    return missions_root(data_dir) / initiative_id


def mission_artifacts_dir(data_dir: Path, initiative_id: str) -> Path:
    return mission_root(data_dir, initiative_id) / "artifacts"


def mission_workspace_dir(data_dir: Path, initiative_id: str) -> Path:
    """编码沙盒目录（repos/ clone 挂在其下，push 成功后可整体回收）。"""
    return mission_root(data_dir, initiative_id) / "workspace"


def mission_chat_dir(data_dir: Path, initiative_id: str) -> Path:
    return mission_root(data_dir, initiative_id) / "chat"


def mission_logs_dir(data_dir: Path, initiative_id: str) -> Path:
    """阶段过程文件目录（回复全文 / 沟通记录 / 工具审计）。

    这些是排查与审计材料，不是交付物，因此不放进对用户展示的
    ``artifacts/<stage>/``；布局为 ``missions/<id>/logs/<stage>/``。
    """
    return mission_root(data_dir, initiative_id) / "logs"


def mission_input_dir(data_dir: Path, initiative_id: str) -> Path:
    """任务输入快照根目录：原件、抽取文本、manifest 与 brief 快照。

    与 ``artifacts/`` 的区别：这里放「用户给我的东西」（只读输入），
    artifacts 放「agent 产出的东西」（交付物）。两者生命周期都是随任务保留。
    """
    return mission_root(data_dir, initiative_id) / "input"


def mission_input_files_dir(data_dir: Path, initiative_id: str) -> Path:
    """原件目录：``input/files/<material_id>__<原文件名>``。"""
    return mission_input_dir(data_dir, initiative_id) / "files"


def mission_input_text_dir(data_dir: Path, initiative_id: str) -> Path:
    """抽取文本目录：``input/text/<material_id>.md``，抽取失败/跳过时不存在。"""
    return mission_input_dir(data_dir, initiative_id) / "text"


def mission_knowledge_logs_dir(data_dir: Path, initiative_id: str) -> Path:
    """知识包归档：``logs/knowledge-pack/``。

    知识包同时会写一份到 workspace（供 Cursor 自动加载），但 workspace 会被
    回收，这里保留的任务目录副本才是「当时给 agent 灌了什么」的审计依据。
    """
    return mission_logs_dir(data_dir, initiative_id) / "knowledge-pack"


def _move_if_missing(src: Path, dst: Path) -> bool:
    """幂等迁移：src 存在且 dst 缺失时 move（不复制），返回是否发生迁移。"""
    if not src.exists() or dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    logger.info("mission layout 迁移: %s -> %s", src, dst)
    return True


def _prune_empty(path: Path) -> None:
    """清理空目录（迁移后遗留的旧根目录），非空则保留不动。"""
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
            logger.info("mission layout 迁移: 移除空目录 %s", path)
    except OSError as exc:  # pragma: no cover — 防御性清理失败不应影响启动
        logger.warning("mission layout 迁移: 清理 %s 失败: %s", path, exc)


def migrate_legacy_layout(data_dir: Path) -> list[str]:
    """启动迁移：旧布局（artifacts/ worktrees/ rooms/）move 到 missions/<id>/ 下。

    - 幂等：目标位置已存在则跳过（保留新数据，旧位置留待人工处理）；
    - 仅 move 不复制，避免迁移期间磁盘翻倍；
    - 旧 worktree 中记录的 git worktree 元数据指向旧绝对路径，迁移后按普通
      目录对待即可（git worktree 语义已移除，业务代码在 repos/ clone 中）；
    - 任何异常只 warn 不阻断启动。
    返回发生迁移的 initiative 列表。
    """
    data_dir = Path(data_dir)
    moved: list[str] = []
    try:
        # 新布局根目录先行创建（迁移目标父目录）
        missions_root(data_dir).mkdir(parents=True, exist_ok=True)
        # 1. 阶段产物 data/artifacts/<id> → missions/<id>/artifacts
        legacy_artifacts = data_dir / "artifacts"
        if legacy_artifacts.is_dir():
            for child in sorted(legacy_artifacts.iterdir()):
                if not child.is_dir():
                    continue
                if _move_if_missing(child, mission_artifacts_dir(data_dir, child.name)):
                    moved.append(child.name)
            _prune_empty(legacy_artifacts)

        # 2. 工作区 data/worktrees/<id> → missions/<id>/workspace
        legacy_worktrees = data_dir / "worktrees"
        if legacy_worktrees.is_dir():
            for child in sorted(legacy_worktrees.iterdir()):
                if not child.is_dir():
                    continue
                if _move_if_missing(child, mission_workspace_dir(data_dir, child.name)):
                    moved.append(child.name)
            _prune_empty(legacy_worktrees)

        # 3. 群聊 data/rooms/<id>.jsonl / <id>__<room>.jsonl → missions/<id>/chat/
        legacy_rooms = data_dir / "rooms"
        if legacy_rooms.is_dir():
            for f in sorted(legacy_rooms.glob("*.jsonl")):
                ini = f.stem.split("__", 1)[0]
                if _move_if_missing(f, mission_chat_dir(data_dir, ini) / f.name):
                    moved.append(ini)
            _prune_empty(legacy_rooms)
    except Exception as exc:  # noqa: BLE001 — 迁移失败不阻断启动
        logger.warning("mission layout 迁移异常（忽略，保留旧布局）: %s", exc)
    return sorted(set(moved))


def ensure_mission_dirs(data_dir: Path, initiative_id: str) -> Path:
    """创建任务根目录及标准子目录，返回根目录。"""
    root = mission_root(data_dir, initiative_id)
    for sub in _MISSION_SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
