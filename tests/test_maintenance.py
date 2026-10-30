"""孤儿 intake 目录回收：只删「会话开了但从未建队、且长期无活动」的目录。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from sortie_deck.maintenance import last_activity, sweep_orphan_missions


def _intake_dir(root: Path, mission_id: str, *, age_days: float) -> Path:
    mission = root / "missions" / mission_id
    (mission / "chat").mkdir(parents=True)
    state = mission / "chat" / "planner.state.json"
    state.write_text("{}", encoding="utf-8")
    stamp = time.time() - age_days * 86400
    os.utime(state, (stamp, stamp))
    os.utime(mission / "chat", (stamp, stamp))
    os.utime(mission, (stamp, stamp))
    return mission


def test_sweeps_only_old_orphans(tmp_path: Path):
    old = _intake_dir(tmp_path, "ini_old", age_days=10)
    fresh = _intake_dir(tmp_path, "ini_fresh", age_days=1)

    removed = sweep_orphan_missions(tmp_path, set(), ttl_days=7)

    assert removed == ["ini_old"]
    assert not old.exists()
    assert fresh.exists()


def test_keeps_missions_that_exist_in_store(tmp_path: Path):
    mission = _intake_dir(tmp_path, "ini_known", age_days=30)

    removed = sweep_orphan_missions(tmp_path, {"ini_known"}, ttl_days=7)

    assert removed == []
    assert mission.exists()


def test_keeps_dirs_with_workspace_checkouts(tmp_path: Path):
    """有 repos 内容说明可能正在跑，交给 workspace TTL，不能在 intake 扫描里删。"""
    mission = _intake_dir(tmp_path, "ini_running", age_days=30)
    repos = mission / "workspace" / "repos" / "svc"
    repos.mkdir(parents=True)
    (repos / "a.py").write_text("print(1)\n", encoding="utf-8")

    removed = sweep_orphan_missions(tmp_path, set(), ttl_days=7)

    assert removed == []
    assert mission.exists()


def test_skips_dirs_without_planner_state(tmp_path: Path):
    """没有规划会话状态的目录不是 intake（可能是历史残留），不动。"""
    legacy = tmp_path / "missions" / "ini_legacy"
    (legacy / "chat").mkdir(parents=True)
    stamp = time.time() - 60 * 86400
    os.utime(legacy, (stamp, stamp))

    assert sweep_orphan_missions(tmp_path, set(), ttl_days=7) == []
    assert legacy.exists()


def test_last_activity_uses_appended_message_mtime(tmp_path: Path):
    """目录 mtime 不随追加消息变化，必须看 planner.jsonl 的 mtime。"""
    mission = _intake_dir(tmp_path, "ini_chat", age_days=30)
    chat = mission / "chat" / "planner.jsonl"
    chat.write_text('{"text":"hi"}\n', encoding="utf-8")

    assert last_activity(mission) > time.time() - 60
    assert sweep_orphan_missions(tmp_path, set(), ttl_days=7) == []
    assert mission.exists()
