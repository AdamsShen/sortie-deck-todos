"""任务目录收敛的启动迁移测试：旧布局 → data/missions/<id>/，幂等、move 不复制。"""

from pathlib import Path

from sortie_deck.layout import (
    migrate_legacy_layout,
    mission_artifacts_dir,
    mission_chat_dir,
    mission_workspace_dir,
)


def _make_legacy(data_dir: Path) -> None:
    """构造旧布局样例：artifacts/<id>/<stage>/x.md、worktrees/<id>/f、rooms/<id>.jsonl。"""
    art = data_dir / "artifacts" / "ini_a" / "qa"
    art.mkdir(parents=True)
    (art / "report.md").write_text("# report\n", encoding="utf-8")

    wt = data_dir / "worktrees" / "ini_a" / "repos" / "demo"
    wt.mkdir(parents=True)
    (wt / "main.py").write_text("print('hi')\n", encoding="utf-8")

    rooms = data_dir / "rooms"
    rooms.mkdir(parents=True)
    (rooms / "ini_a.jsonl").write_text('{"a":1}\n', encoding="utf-8")
    (rooms / "ini_a__sub.jsonl").write_text('{"b":2}\n', encoding="utf-8")
    # 其他任务的房间文件（归到对应 ini）
    (rooms / "ini_b.jsonl").write_text('{"c":3}\n', encoding="utf-8")


def test_migrate_moves_all_legacy_dirs(tmp_path: Path):
    data = tmp_path / "data"
    _make_legacy(data)

    moved = migrate_legacy_layout(data)
    assert "ini_a" in moved
    # 产物迁移到 missions/<id>/artifacts
    assert (mission_artifacts_dir(data, "ini_a") / "qa" / "report.md").is_file()
    # 工作区迁移到 missions/<id>/workspace
    assert (mission_workspace_dir(data, "ini_a") / "repos" / "demo" / "main.py").is_file()
    # 群聊迁移到 missions/<id>/chat/（子房间跟随）
    assert (mission_chat_dir(data, "ini_a") / "ini_a.jsonl").is_file()
    assert (mission_chat_dir(data, "ini_a") / "ini_a__sub.jsonl").is_file()
    assert (mission_chat_dir(data, "ini_b") / "ini_b.jsonl").is_file()
    # 旧根目录被清理
    assert not (data / "artifacts").exists()
    assert not (data / "worktrees").exists()
    assert not (data / "rooms").exists()


def test_migrate_is_idempotent_and_keeps_new_data(tmp_path: Path):
    data = tmp_path / "data"
    _make_legacy(data)
    migrate_legacy_layout(data)

    # 新布局里已有更新的产物 → 再次迁移不得覆盖
    new_art = mission_artifacts_dir(data, "ini_a") / "qa"
    (new_art / "report.md").write_text("# new\n", encoding="utf-8")
    # 重新放一份旧文件模拟重复迁移
    legacy = data / "artifacts" / "ini_a" / "qa"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "report.md").write_text("# old\n", encoding="utf-8")

    moved = migrate_legacy_layout(data)
    assert "ini_a" not in moved  # 目标已存在 → 跳过
    assert (new_art / "report.md").read_text(encoding="utf-8") == "# new\n"


def test_migrate_noop_on_fresh_data_dir(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    assert migrate_legacy_layout(data) == []
    # 新布局根目录存在
    assert (data / "missions").is_dir()
