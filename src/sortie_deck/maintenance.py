"""任务目录维护：回收「开了规划会话但从未建队」的孤儿目录。

规划官会话一开启就会预生成任务 id 并建好 ``missions/<id>/``，用户中途放弃时
这些目录永远不会被 ``delete_initiative`` 收走（任务表里根本没有它）。这里按
活动时间做 TTL 兜底，避免它们长期占盘。
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

# 规划会话的活动痕迹：任一文件更新即视为「这个 intake 还活着」
_PLANNER_FILES = (
    "chat/planner.state.json",
    "chat/planner.jsonl",
    "chat/planner.events.jsonl",
)


def last_activity(mission_dir: Path) -> float:
    """目录最后活动时间：取规划会话文件与目录自身 mtime 的最大值。

    目录 mtime 只在增删条目时变化，追加消息不会更新它，所以必须单独看
    ``planner.jsonl`` 这类被持续写入的文件，否则一个聊了两周才放弃的会话
    会被误判为「很早就不活跃」。
    """
    stamps = [mission_dir.stat().st_mtime]
    for rel in _PLANNER_FILES:
        try:
            stamps.append((mission_dir / rel).stat().st_mtime)
        except OSError:
            continue
    return max(stamps)


def sweep_orphan_missions(
    data_dir: Path,
    known_ids: Iterable[str],
    *,
    ttl_days: float = 7.0,
    now: float | None = None,
) -> list[str]:
    """删除满足全部条件的 ``missions/<id>``，返回被删除的 id 列表。

    条件：
    1. 任务表里没有该 id（从未建队，或任务已被删除）；
    2. 存在规划会话状态文件（确认是 intake 目录，不是别的东西）；
    3. 最后活动时间早于 ``ttl_days``；
    4. ``workspace/repos`` 为空（有内容说明可能正在跑，交给 workspace TTL）。
    """
    root = Path(data_dir) / "missions"
    if not root.is_dir():
        return []
    known = set(known_ids)
    cutoff = (now if now is not None else time.time()) - ttl_days * 86400.0
    removed: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in known:
            continue
        if not (child / "chat" / "planner.state.json").exists():
            # 没有会话状态：不是 intake 目录，保持原样交由人工处理
            continue
        repos = child / "workspace" / "repos"
        try:
            if repos.is_dir() and any(repos.iterdir()):
                continue
        except OSError:
            continue
        try:
            if last_activity(child) > cutoff:
                continue
        except OSError:
            continue
        try:
            shutil.rmtree(child)
        except OSError as exc:
            logger.warning("孤儿任务目录回收失败: %s: %s", child, exc)
            continue
        removed.append(child.name)
    if removed:
        logger.info("孤儿 intake 目录回收 %s 个: %s", len(removed), removed)
    return removed
