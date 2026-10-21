"""skill 脚本执行沙箱（skill_runner.run_skill_script）的行为测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sortie_deck.settings import settings
from sortie_deck.skill_runner import run_skill_script
from sortie_deck.toolkit import CreateToolkitRequest, ToolkitStore


@pytest.fixture()
def store(tmp_path: Path) -> ToolkitStore:
    return ToolkitStore(tmp_path / "toolkit.json")


def _add(store: ToolkitStore, **kwargs: Any):
    return store.create(
        CreateToolkitRequest(**kwargs), author_id="tester", author_name="Tester"
    )


def _skill_item(store: ToolkitStore, **overrides: Any):
    kwargs: dict[str, Any] = {
        "kind": "skill",
        "name": "check-acceptance",
        "summary": "校验 acceptance 结构",
        "executable": True,
        "scripts": [
            {
                "path": "scripts/hello.py",
                "language": "py",
                "content": "print('hello from sandbox')",
            }
        ],
    }
    kwargs.update(overrides)
    return _add(store, **kwargs)


@pytest.mark.asyncio()
async def test_runs_registered_script(store: ToolkitStore, tmp_path: Path) -> None:
    item = _skill_item(store)
    out = await run_skill_script(
        item, "scripts/hello.py", workspace_dir=str(tmp_path)
    )
    assert out["ok"] is True
    assert out["exit_code"] == 0
    assert "hello from sandbox" in out["stdout"]


@pytest.mark.asyncio()
async def test_rejects_non_executable_skill(store: ToolkitStore, tmp_path: Path) -> None:
    item = _skill_item(store, executable=False)
    out = await run_skill_script(item, "scripts/hello.py", workspace_dir=str(tmp_path))
    assert out["ok"] is False
    assert "not marked executable" in out["reason"]


@pytest.mark.asyncio()
async def test_rejects_unregistered_path(store: ToolkitStore, tmp_path: Path) -> None:
    item = _skill_item(store)
    out = await run_skill_script(item, "../etc/passwd", workspace_dir=str(tmp_path))
    assert out["ok"] is False
    assert "not registered" in out["reason"]


@pytest.mark.asyncio()
async def test_rejects_when_switch_off(
    store: ToolkitStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _skill_item(store)
    monkeypatch.setattr(settings, "skill_run_enabled", False)
    out = await run_skill_script(item, "scripts/hello.py", workspace_dir=str(tmp_path))
    assert out["ok"] is False
    assert "disabled" in out["reason"]


@pytest.mark.asyncio()
async def test_rejects_unsupported_language(store: ToolkitStore, tmp_path: Path) -> None:
    item = _skill_item(
        store,
        scripts=[{"path": "scripts/tool.rb", "language": "rb", "content": "puts 1"}],
    )
    out = await run_skill_script(item, "scripts/tool.rb", workspace_dir=str(tmp_path))
    assert out["ok"] is False
    assert "unsupported script language" in out["reason"]


@pytest.mark.asyncio()
async def test_timeout_kills_process(store: ToolkitStore, tmp_path: Path) -> None:
    item = _skill_item(
        store,
        scripts=[
            {
                "path": "scripts/slow.py",
                "language": "py",
                "content": "import time; time.sleep(10)",
            }
        ],
    )
    out = await run_skill_script(
        item, "scripts/slow.py", workspace_dir=str(tmp_path), timeout=0.3
    )
    assert out["ok"] is False
    assert out["timed_out"] is True


@pytest.mark.asyncio()
async def test_truncates_large_output(store: ToolkitStore, tmp_path: Path) -> None:
    item = _skill_item(
        store,
        scripts=[
            {
                "path": "scripts/big.py",
                "language": "py",
                "content": "print('x' * 50000)",
            }
        ],
    )
    out = await run_skill_script(item, "scripts/big.py", workspace_dir=str(tmp_path))
    assert out["ok"] is True
    assert len(out["stdout"]) < 9000
    assert "truncated" in out["stdout"]


@pytest.mark.asyncio()
async def test_stderr_and_nonzero_exit(store: ToolkitStore, tmp_path: Path) -> None:
    item = _skill_item(
        store,
        scripts=[
            {
                "path": "scripts/fail.py",
                "language": "py",
                "content": "import sys; print('oops', file=sys.stderr); sys.exit(3)",
            }
        ],
    )
    out = await run_skill_script(item, "scripts/fail.py", workspace_dir=str(tmp_path))
    assert out["ok"] is False
    assert out["exit_code"] == 3
    assert "oops" in out["stderr"]
