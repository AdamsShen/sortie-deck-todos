from pathlib import Path
import zipfile

import pytest

from sortie_deck.workspace import (
    RevealUnavailable,
    archive_repos_snapshot,
    is_under_allowed,
    list_files,
    mission_workspace,
    resolve_git_repo,
    reveal_in_os,
    reveal_unavailable_reason,
)


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


def test_reveal_in_os_headless_raises_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """无图形会话的容器（云端部署）里：应抛 RevealUnavailable，而非 FileNotFoundError。"""
    monkeypatch.setattr("sortie_deck.workspace.platform.system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    assert reveal_unavailable_reason()
    with pytest.raises(RevealUnavailable) as exc:
        reveal_in_os(tmp_path)
    assert "graphical session" in exc.value.reason


def test_reveal_unavailable_reason_missing_xdg_open(monkeypatch: pytest.MonkeyPatch):
    """有 DISPLAY 但镜像里没有 xdg-open 时同样判定为不可用。"""
    monkeypatch.setattr("sortie_deck.workspace.platform.system", lambda: "Linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("sortie_deck.workspace.shutil.which", lambda _name: None)

    assert "xdg-open" in reveal_unavailable_reason()


# --- archive_repos_snapshot：未绑定仓库任务的代码归档 ------------------------


def _mk_ws(tmp_path: Path, ini: str = "ini_arc") -> tuple[Path, Path]:
    """构造任务沙盒与 artifacts 目录，返回 (workspace, artifacts)。"""
    ws = tmp_path / "missions" / ini / "workspace"
    art = tmp_path / "missions" / ini / "artifacts"
    (ws / "repos").mkdir(parents=True)
    art.mkdir(parents=True)
    return ws, art


def test_archive_repos_snapshot_basics(tmp_path: Path):
    """产出代码打包 zip：含 repos 前缀路径，排除依赖目录与隐藏文件，manifest 落盘。"""
    ws, art = _mk_ws(tmp_path)
    (ws / "repos" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    sub = ws / "repos" / "sub"
    sub.mkdir()
    (sub / "util.py").write_text("x = 1\n", encoding="utf-8")
    nm = ws / "repos" / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("j", encoding="utf-8")
    (ws / "repos" / ".env").write_text("secret", encoding="utf-8")

    result = archive_repos_snapshot(ws, art)
    assert result and result["status"] == "archived"
    assert result["source"] == "repos"
    assert result["file_count"] == 2

    zip_path = art / "code-archive" / result["zip"]
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "repos/app.py" in names and "repos/sub/util.py" in names
    assert not any("node_modules" in n for n in names)
    assert not any(n.endswith(".env") for n in names)

    import json

    manifest = json.loads((art / "code-archive" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["initiative_id"] == "ini_arc"
    assert manifest["file_count"] == 2
    assert manifest["truncated"] is False


def test_archive_repos_snapshot_skips_bound_repos(tmp_path: Path):
    """repos/ 下存在 clone（<name>/.git）→ 绑定任务跳过归档。"""
    ws, art = _mk_ws(tmp_path)
    clone = ws / "repos" / "repo"
    clone.mkdir(parents=True)
    (clone / ".git").mkdir()
    (clone / "app.py").write_text("x=1\n", encoding="utf-8")

    result = archive_repos_snapshot(ws, art)
    assert result == {"status": "skipped", "reason": "bound_repo", "initiative_id": "ini_arc"}
    assert not (art / "code-archive").exists()


def test_archive_repos_snapshot_skips_bound_flag(tmp_path: Path):
    """调用方元数据标记绑定（push 后 clone 已被清理的场景）→ 跳过。"""
    ws, art = _mk_ws(tmp_path)
    (ws / "repos" / "app.py").write_text("x=1\n", encoding="utf-8")
    result = archive_repos_snapshot(ws, art, bound=True)
    assert result["status"] == "skipped" and result["reason"] == "bound_repo"


def test_archive_repos_snapshot_empty_sandbox(tmp_path: Path):
    """空沙盒（只有 README 与空 repos/）→ 跳过，不打空包。"""
    ws, art = _mk_ws(tmp_path)
    (ws / "README.md").write_text("# Workspace\n", encoding="utf-8")
    result = archive_repos_snapshot(ws, art)
    assert result["status"] == "skipped" and result["reason"] == "empty"


def test_archive_repos_snapshot_fallback_root(tmp_path: Path):
    """repos/ 为空但沙盒根有散落文件（agent 未按约定写）→ 兜底打包根文件。"""
    ws, art = _mk_ws(tmp_path)
    (ws / "README.md").write_text("# Workspace\n", encoding="utf-8")
    (ws / "impl.py").write_text("print('x')\n", encoding="utf-8")

    result = archive_repos_snapshot(ws, art)
    assert result and result["status"] == "archived"
    assert result["source"] == "workspace_root"
    with zipfile.ZipFile(art / "code-archive" / result["zip"]) as zf:
        names = set(zf.namelist())
    assert "impl.py" in names
    assert not any(n == "README.md" for n in names)


def test_archive_repos_snapshot_truncation(tmp_path: Path):
    """累计体积超过 max_mb 停止添加并标记 truncated。"""
    ws, art = _mk_ws(tmp_path)
    (ws / "repos" / "big.bin").write_bytes(b"a" * (2 * 1024 * 1024))
    (ws / "repos" / "small.py").write_text("x=1\n", encoding="utf-8")

    result = archive_repos_snapshot(ws, art, max_mb=1)
    assert result and result["status"] == "archived"
    assert result["truncated"] is True
    assert result["file_count"] == 0


def test_archive_repos_snapshot_idempotent(tmp_path: Path):
    """内容未变重复触发跳过（sweep 与终态都会调）；zip 缺失时强制重打。"""
    ws, art = _mk_ws(tmp_path)
    (ws / "repos" / "app.py").write_text("x=1\n", encoding="utf-8")

    first = archive_repos_snapshot(ws, art)
    assert first and first["status"] == "archived"

    second = archive_repos_snapshot(ws, art)
    assert second and second["status"] == "skipped" and second["reason"] == "unchanged"

    # 内容变化 → 新归档
    (ws / "repos" / "app.py").write_text("x=2\n", encoding="utf-8")
    third = archive_repos_snapshot(ws, art)
    assert third and third["status"] == "archived" and third["zip"] != first["zip"]

    # zip 被删（人工清理）→ fingerprint 失效强制重打
    (art / "code-archive" / third["zip"]).unlink()
    fourth = archive_repos_snapshot(ws, art)
    assert fourth and fourth["status"] == "archived"


def test_archive_repos_snapshot_no_workspace(tmp_path: Path):
    """workspace 不存在（终态已清理过）→ no-op。"""
    result = archive_repos_snapshot(
        tmp_path / "missions" / "ini_x" / "workspace",
        tmp_path / "missions" / "ini_x" / "artifacts",
    )
    assert result is None


# --- 终态清理触发归档（orchestrator 集成） -----------------------------------


@pytest.mark.asyncio
async def test_cleanup_mission_workspace_archives_unbound(tmp_path):
    """终态清理链路：未绑定任务回收 workspace 前产出代码已归档。"""
    import zipfile as _zipfile

    from sortie_deck.models import Initiative
    from sortie_deck.orchestrator import Orchestrator
    from sortie_deck.settings import Settings

    cfg = Settings(
        data_dir=tmp_path / "data",
        deploy_executor="mock",
        kb_backend="local",
        pipeline_llm=False,
        room_llm=False,
    )
    orch = Orchestrator(cfg)
    ini = "ini_arc_e2e"
    orch.store.upsert(Initiative(id=ini, title="t", brief="b"))

    ws = cfg.missions_dir / ini / "workspace"
    (ws / "repos").mkdir(parents=True)
    (ws / "repos" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "README.md").write_text("# sandbox\n", encoding="utf-8")

    orch._cleanup_mission_workspace(ini)

    assert not ws.exists()
    zips = list((cfg.missions_dir / ini / "artifacts" / "code-archive").glob("*.zip"))
    assert len(zips) == 1
    with _zipfile.ZipFile(zips[0]) as zf:
        assert "repos/app.py" in zf.namelist()


@pytest.mark.asyncio
async def test_cleanup_mission_workspace_skips_bound(tmp_path):
    """终态清理链路：绑定仓库任务（角色带 repos）不归档，直接回收。"""
    from sortie_deck.models import Initiative, RepoConfig, RoleAgent, RoleId
    from sortie_deck.orchestrator import Orchestrator
    from sortie_deck.settings import Settings

    cfg = Settings(
        data_dir=tmp_path / "data",
        deploy_executor="mock",
        kb_backend="local",
        pipeline_llm=False,
        room_llm=False,
    )
    orch = Orchestrator(cfg)
    ini = "ini_bound_e2e"
    orch.store.upsert(
        Initiative(
            id=ini,
            title="t",
            brief="b",
            roles=[
                RoleAgent(
                    id="eng1",
                    role=RoleId.ENG,
                    title="Dev",
                    persona="p",
                    executor="mock",
                    repos=[RepoConfig(url="https://gitlab.example.com/acme/web.git")],
                )
            ],
        )
    )

    ws = cfg.missions_dir / ini / "workspace"
    (ws / "repos").mkdir(parents=True)
    (ws / "repos" / "app.py").write_text("x = 1\n", encoding="utf-8")

    orch._cleanup_mission_workspace(ini)

    assert not ws.exists()
    code_dir = cfg.missions_dir / ini / "artifacts" / "code-archive"
    assert not code_dir.exists()
