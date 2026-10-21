"""in-loop 元工具编排层（graph.run_toolkit_meta）的行为测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sortie_deck.graph import run_toolkit_meta
from sortie_deck.settings import settings
from sortie_deck.toolkit import CreateToolkitRequest, ToolkitStore


@pytest.fixture()
def store(tmp_path: Path) -> ToolkitStore:
    return ToolkitStore(tmp_path / "toolkit.json")


def _add(store: ToolkitStore, **kwargs: Any):
    return store.create(
        CreateToolkitRequest(**kwargs), author_id="tester", author_name="Tester"
    )


@pytest.mark.asyncio()
async def test_search_intent_returns_candidates(store: ToolkitStore) -> None:
    item = _add(
        store,
        kind="mcp",
        name="pg-monitor",
        summary="查询 postgres 慢查询日志",
        transport="stdio",
        command="npx",
    )
    out = await run_toolkit_meta(
        store,
        stage="eng_backend",
        meta={"toolkit_search": {"query": "postgres 慢查询日志"}},
    )
    assert item.id in out.brief_append
    assert out.records[0]["op"] == "search"
    assert out.records[0]["candidates"][0]["id"] == item.id
    assert any(e["kind"] == "toolkit_meta" for e in out.events)


@pytest.mark.asyncio()
async def test_search_no_match_reports(store: ToolkitStore) -> None:
    out = await run_toolkit_meta(
        store, stage="s", meta={"toolkit_search": {"query": "不存在的capability"}}
    )
    assert "found no matching capability" in out.brief_append
    assert out.records[0]["candidates"] == []


@pytest.mark.asyncio()
async def test_pick_success_floats_item(store: ToolkitStore) -> None:
    item = _add(store, kind="tool", name="git-diff", runtime="cli", endpoint="git diff --stat")
    out = await run_toolkit_meta(
        store, stage="eng", meta={"toolkit_pick": {"item_id": item.id}}
    )
    assert "git diff --stat" in out.brief_append
    assert out.records[0]["floated"] is True


@pytest.mark.asyncio()
async def test_pick_rejections(store: ToolkitStore) -> None:
    item = _add(store, kind="tool", name="git-diff", runtime="cli", endpoint="git diff --stat")

    # 不存在
    out = await run_toolkit_meta(store, stage="s", meta={"toolkit_pick": {"item_id": "tk_x"}})
    assert "rejected" in out.brief_append

    # 白名单外
    out = await run_toolkit_meta(
        store,
        stage="s",
        meta={"toolkit_pick": {"item_id": item.id}},
        allowed_ids=set(),
    )
    assert "allowlist" in out.brief_append

    # 预算用尽
    out = await run_toolkit_meta(
        store,
        stage="s",
        meta={"toolkit_pick": {"item_id": item.id}},
        used_picks=8,
    )
    assert "budget exhausted" in out.brief_append

    # 本阶段已上浮过（去重）
    out = await run_toolkit_meta(
        store,
        stage="s",
        meta={"toolkit_pick": {"item_id": item.id}},
        floated=[item.id],
    )
    assert "already floated" in out.brief_append


@pytest.mark.asyncio()
async def test_switch_disabled_noop(store: ToolkitStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _add(store, kind="tool", name="git-diff", runtime="cli", endpoint="git diff --stat")
    monkeypatch.setattr(settings, "toolkit_search", False)
    out = await run_toolkit_meta(
        store, stage="s", meta={"toolkit_search": {"query": "git"}}
    )
    assert out.brief_append == "" and out.records == []


@pytest.mark.asyncio()
async def test_meta_none_is_noop(store: ToolkitStore) -> None:
    out = await run_toolkit_meta(store, stage="s", meta=None)
    assert out.brief_append == "" and out.events == [] and out.records == []


# --- skill_run 意图 -----------------------------------------------------------


def _skill(store: ToolkitStore, **overrides: Any):
    kwargs: dict[str, Any] = {
        "kind": "skill",
        "name": "check-acceptance",
        "summary": "校验 acceptance 结构",
        "executable": True,
        "scripts": [
            {
                "path": "scripts/hello.py",
                "language": "py",
                "content": "print('run-ok')",
            }
        ],
    }
    kwargs.update(overrides)
    return _add(store, **kwargs)


@pytest.mark.asyncio()
async def test_skill_run_success(store: ToolkitStore, tmp_path: Path) -> None:
    item = _skill(store)
    out = await run_toolkit_meta(
        store,
        stage="qa",
        meta={"skill_run": {"item_id": item.id, "script": "scripts/hello.py"}},
        floated=[item.id],  # 已 pick 过才允许执行
        workspace_dir=str(tmp_path),
    )
    assert "run-ok" in out.brief_append
    record = out.records[0]
    assert record["op"] == "skill_run" and record["executed"] is True
    assert record["ok"] is True and record["exit_code"] == 0


@pytest.mark.asyncio()
async def test_skill_run_requires_prior_pick(store: ToolkitStore, tmp_path: Path) -> None:
    item = _skill(store)
    out = await run_toolkit_meta(
        store,
        stage="qa",
        meta={"skill_run": {"item_id": item.id, "script": "scripts/hello.py"}},
        floated=[],  # 未 pick
        workspace_dir=str(tmp_path),
    )
    assert "pick the item first" in out.brief_append
    assert "executed" not in out.records[0]


@pytest.mark.asyncio()
async def test_skill_run_budget_exhausted(store: ToolkitStore, tmp_path: Path) -> None:
    item = _skill(store)
    out = await run_toolkit_meta(
        store,
        stage="qa",
        meta={"skill_run": {"item_id": item.id, "script": "scripts/hello.py"}},
        floated=[item.id],
        skill_used=4,
        workspace_dir=str(tmp_path),
    )
    assert "budget exhausted" in out.brief_append
    assert "executed" not in out.records[0]


@pytest.mark.asyncio()
async def test_skill_run_write_script_needs_confirm(
    store: ToolkitStore, tmp_path: Path
) -> None:
    item = _skill(
        store,
        scripts=[
            {
                "path": "scripts/delete_all.py",
                "language": "py",
                "content": "print('would delete')",
            }
        ],
    )
    out = await run_toolkit_meta(
        store,
        stage="qa",
        meta={"skill_run": {"item_id": item.id, "script": "scripts/delete_all.py"}},
        floated=[item.id],
        workspace_dir=str(tmp_path),
    )
    assert "looks like a write" in out.brief_append
    assert "executed" not in out.records[0]

    # confirmed=true 放行
    out2 = await run_toolkit_meta(
        store,
        stage="qa",
        meta={
            "skill_run": {
                "item_id": item.id,
                "script": "scripts/delete_all.py",
                "confirmed": True,
            }
        },
        floated=[item.id],
        workspace_dir=str(tmp_path),
    )
    assert "would delete" in out2.brief_append
    assert out2.records[0]["ok"] is True
