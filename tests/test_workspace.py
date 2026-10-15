from pathlib import Path

from sortie_deck.workspace import is_under_allowed, list_files, mission_workspace, resolve_git_repo


def test_list_files_skips_node_modules(tmp_path: Path):
    (tmp_path / "ok.md").write_text("x", encoding="utf-8")
    nested = tmp_path / "node_modules" / "pkg"
    nested.mkdir(parents=True)
    (nested / "index.js").write_text("y", encoding="utf-8")
    files = list_files(tmp_path)
    assert any(f["name"] == "ok.md" for f in files)
    assert not any("node_modules" in f["path"] for f in files)


def test_mission_workspace_paths(tmp_path: Path):
    data = tmp_path / "data"
    ini = "ini_test"
    # 任务目录收敛布局：data/missions/<id>/{artifacts,workspace}
    art_dir = data / "missions" / ini / "artifacts" / "qa"
    art_dir.mkdir(parents=True)
    (art_dir / "test_cases.md").write_text("# cases\n", encoding="utf-8")
    ws = data / "missions" / ini / "workspace"
    ws.mkdir(parents=True)
    (ws / "index.html").write_text("<html></html>", encoding="utf-8")

    info = mission_workspace(
        initiative_id=ini,
        data_dir=data,
        repo_root=tmp_path,
    )
    assert info["worktree_exists"] is True
    assert info["artifacts_exists"] is True
    assert any(f["name"] == "test_cases.md" for f in info["artifact_files"])
    assert any(f["name"] == "index.html" for f in info["worktree_files"])


def test_allowed_roots(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    target = data / "artifacts" / "x"
    target.mkdir(parents=True)
    assert is_under_allowed(target, [data.resolve()])
    assert not is_under_allowed(tmp_path / "elsewhere", [data.resolve()])


def test_resolve_git_repo_none(tmp_path: Path):
    assert resolve_git_repo(tmp_path) is None or resolve_git_repo(tmp_path).exists()
