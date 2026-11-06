"""影子仓库：无 git 任务的阶段改动采集。

回归 2026-11-05 贪吃蛇任务（ini_b93e23b70190）：未绑仓库的任务代码写在
``workspace/repos/`` 下，``_git_diff`` 只能给出文件清单，自审被迫逐个 Read，
两轮 480s 预算全部超时。这些用例锁定影子仓的边界。
"""

from __future__ import annotations

import asyncio
import subprocess

from sortie_deck.shadow_git import ShadowRepo, ensure_shadow_baseline


def _git(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit_all(path) -> None:
    _git("add", "-A", cwd=path)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init", cwd=path)


def _workspace(tmp_path, initiative_id: str = "ini_x"):
    ws = tmp_path / "data" / "missions" / initiative_id / "workspace"
    ws.mkdir(parents=True)
    return ws


# --- 1. 无 git 项目能拿到真实 diff -------------------------------------------


def test_shadow_captures_new_files_without_project_git(tmp_path):
    """从 0 开发：repos/ 下的新文件必须出现在 diff 里，且 workspace 内不产生 .git。"""
    ws = _workspace(tmp_path)
    repo = ShadowRepo(ws)
    assert asyncio.run(repo.ensure()) is True

    game = ws / "repos" / "snake" / "game.py"
    game.parent.mkdir(parents=True)
    game.write_text("def move():\n    return 1\n", encoding="utf-8")

    diff = asyncio.run(repo.capture())

    assert diff is not None
    assert "repos/snake/game.py" in diff
    assert "+def move():" in diff
    # 影子仓在 mission 根，不在 workspace 内：agent 的 git 命令看不到
    assert not (ws / ".git").exists()
    assert (ws.parent / ".shadow.git").is_dir()


def test_git_diff_uses_shadow_repo_for_ungitted_mission(tmp_path, monkeypatch):
    """_git_diff 第 2 路：影子仓有基线时应给出增删行，而不是退化成文件清单。"""
    from sortie_deck.plugins import coding

    ws = _workspace(tmp_path)
    game = ws / "repos" / "snake" / "game.py"
    game.parent.mkdir(parents=True)
    game.write_text("def move():\n    pass\n", encoding="utf-8")
    assert asyncio.run(ensure_shadow_baseline(ws)) is True

    game.write_text("def move():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(coding, "_which", lambda name: "/usr/bin/git")

    diff = asyncio.run(coding._git_diff(ws, tmp_path))

    assert "-    pass" in diff
    assert "+    return 1" in diff
    assert "# worktree files (no git)" not in diff


# --- 2. 与已绑仓库任务隔离 ----------------------------------------------------


def test_shadow_skips_nested_repo_clone(tmp_path):
    """repos/<name> 已有 .git 时跳过该子目录，不能写成 Subproject gitlink。"""
    ws = _workspace(tmp_path)
    clone = ws / "repos" / "web"
    clone.mkdir(parents=True)
    _git("init", "-b", "main", cwd=clone)
    (clone / "app.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(clone)

    repo = ShadowRepo(ws)
    assert asyncio.run(repo.ensure()) is True

    (clone / "app.py").write_text("x = 2\n", encoding="utf-8")
    diff = asyncio.run(repo.capture())

    assert diff is not None
    assert "Subproject commit" not in diff
    # 克隆仓的改动交给 _git_diff 第 3 路，影子仓不重复采集
    assert "repos/web/app.py" not in diff


def test_shadow_skipped_when_workspace_is_git_root(tmp_path):
    """workspace 自身是 git 仓库根时交给第 1 路，影子仓不创建。"""
    ws = _workspace(tmp_path)
    _git("init", "-b", "main", cwd=ws)
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")

    assert asyncio.run(ShadowRepo(ws).ensure()) is False
    assert not (ws.parent / ".shadow.git").exists()


def test_shadow_rescans_git_dir_created_mid_stage(tmp_path):
    """阶段中途 agent 自己 git init：采集前重扫，避免把目录写成 gitlink。"""
    ws = _workspace(tmp_path)
    repo = ShadowRepo(ws)
    assert asyncio.run(repo.ensure()) is True

    late = ws / "repos" / "late"
    late.mkdir(parents=True)
    (late / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git("init", "-b", "main", cwd=late)

    diff = asyncio.run(repo.capture())

    assert diff is not None
    assert "Subproject commit" not in diff


# --- 3. 排除与降级 ------------------------------------------------------------


def test_shadow_rebaselines_on_next_stage(tmp_path):
    """同一 mission 的下一阶段必须重新基线，diff 只含本阶段改动。

    回归 2026-11-06 实测：影子仓建好后 ``rev-parse`` 在注入 GIT_DIR 的环境下会把
    workspace 报成仓库根，第二次 ``ensure()`` 直接返回 False → baseline 不前移，
    阶段 2 的 diff 累计了阶段 1 的全部文件（24 个），review 会审到上一阶段的代码。
    """
    ws = _workspace(tmp_path)
    repo = ShadowRepo(ws)
    assert asyncio.run(repo.ensure()) is True

    for index in range(24):
        path = ws / "repos" / "snake" / f"m{index:02d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def f{index}():\n    return {index}\n", encoding="utf-8")

    first = asyncio.run(repo.capture())
    assert first is not None
    assert first.count("diff --git") == 24

    assert asyncio.run(repo.ensure()) is True

    for name in ("m00.py", "m01.py"):
        (ws / "repos" / "snake" / name).write_text("def patched():\n    return 1\n", encoding="utf-8")
    second = asyncio.run(repo.capture())

    assert second is not None
    assert second.count("diff --git") == 2
    assert "+def patched():" in second


def test_shadow_excludes_binaries_and_deps(tmp_path):
    """二进制与依赖目录不进 diff：review 拿它们没有意义，还会撑爆预算。"""
    ws = _workspace(tmp_path)
    repo = ShadowRepo(ws)
    assert asyncio.run(repo.ensure()) is True

    (ws / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (ws / "data").mkdir()
    (ws / "data" / "snake.db").write_bytes(b"\x00")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "lib.js").write_text("module.exports = 1\n", encoding="utf-8")
    (ws / "app.py").write_text("print(1)\n", encoding="utf-8")

    diff = asyncio.run(repo.capture())

    assert diff is not None
    assert "app.py" in diff
    assert "logo.png" not in diff
    assert "snake.db" not in diff
    assert "node_modules" not in diff


def test_shadow_degrades_when_git_missing(tmp_path, monkeypatch):
    """git 不可用时 ensure/capture 都不能抛异常，由调用方回退文件清单。"""
    from sortie_deck import shadow_git

    ws = _workspace(tmp_path)
    monkeypatch.setattr(shadow_git.shutil, "which", lambda name: None)

    assert asyncio.run(ShadowRepo(ws).ensure()) is False
    assert asyncio.run(ShadowRepo(ws).capture()) is None


def test_shadow_disabled_by_setting(tmp_path, monkeypatch):
    """TDT_SHADOW_GIT_ENABLED=false 是回滚开关：不建影子仓。"""
    from sortie_deck.settings import settings

    ws = _workspace(tmp_path)
    monkeypatch.setattr(settings, "shadow_git_enabled", False)

    assert asyncio.run(ShadowRepo(ws).ensure()) is False
    assert not (ws.parent / ".shadow.git").exists()
