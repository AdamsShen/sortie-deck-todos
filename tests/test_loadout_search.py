"""in-loop 元工具（toolkit_search / toolkit_pick）的召回、摘要与上浮单测。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sortie_deck.loadout import (
    PICK_BUDGET_PER_STAGE,
    SKILL_BODY_FLOAT_LIMIT,
    pick_candidate,
    recall,
    summarize_candidates,
    unfreeze_item,
)
from sortie_deck.toolkit import CreateToolkitRequest, ToolkitStore


@pytest.fixture()
def store(tmp_path: Path) -> ToolkitStore:
    return ToolkitStore(tmp_path / "toolkit.json")


def _add(store: ToolkitStore, **kwargs: Any):
    return store.create(
        CreateToolkitRequest(**kwargs), author_id="tester", author_name="Tester"
    )


def _disable(store: ToolkitStore, item_id: str) -> None:
    """直接改存储层把条目置为停用（绕开 UpdateToolkitRequest 的字段拼装）。"""
    items = [i.model_dump(mode="json") for i in store._read()]  # noqa: SLF001 — 测试便利
    for data in items:
        if data["id"] == item_id:
            data["enabled"] = False
    store.path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


# --- recall：自由文本 query 与白名单过滤 -------------------------------------


def test_recall_free_text_query_matches(store: ToolkitStore) -> None:
    _add(
        store,
        kind="mcp",
        name="pg-monitor",
        summary="查询 postgres 慢查询日志与指标",
        transport="stdio",
        command="npx",
    )
    _add(store, kind="tool", name="git-diff", summary="查看工作区 diff", runtime="cli")

    rows = recall(store, query="postgres 慢查询日志")
    assert rows, "自由文本 query 应能命中 summary 关键词"
    assert rows[0][0].name == "pg-monitor"


def test_recall_disabled_and_allowlist_excluded(store: ToolkitStore) -> None:
    allowed_item = _add(store, kind="tool", name="secret-tool", summary="敏感操作", runtime="cli")
    disabled = _add(store, kind="tool", name="off-tool", summary="敏感操作工具", runtime="cli")
    _disable(store, disabled.id)

    rows = recall(store, query="敏感操作")
    assert all(r[0].id != disabled.id for r in rows), "停用条目不得进入召回"

    rows = recall(store, query="敏感操作", allowed_ids={allowed_item.id})
    assert [r[0].id for r in rows] == [allowed_item.id], "白名单外的条目必须被硬过滤"


def test_recall_empty_returns_empty(store: ToolkitStore) -> None:
    assert recall(store, query="") == []
    assert recall(None, query="anything") == []


# --- summarize_candidates ----------------------------------------------------


def test_summarize_candidates_truncates_summary(store: ToolkitStore) -> None:
    item = _add(store, kind="mcp", name="fs", summary="很长的摘要" * 100, transport="stdio")
    rows = summarize_candidates([(item, 1.0)])
    assert rows[0]["id"] == item.id
    assert rows[0]["kind"] == "mcp"
    assert len(rows[0]["summary"]) <= 200


# --- unfreeze_item -----------------------------------------------------------


def test_unfreeze_tool_and_mcp(store: ToolkitStore) -> None:
    tool = _add(store, kind="tool", name="git-diff", runtime="cli", endpoint="git diff --stat")
    mcp = _add(
        store,
        kind="mcp",
        name="filesystem",
        transport="stdio",
        command="npx",
        env={"SECRET_TOKEN": "plain"},
    )
    assert "git diff --stat" in unfreeze_item(tool)
    mcp_text = unfreeze_item(mcp)
    assert "npx" in mcp_text
    assert "SECRET_TOKEN" not in mcp_text, "env 永不展开明文"


def test_unfreeze_skill_body_truncated(store: ToolkitStore) -> None:
    body = "步骤 " * (SKILL_BODY_FLOAT_LIMIT // 2 + 10)
    skill = _add(store, kind="skill", name="prd-tighten", body=body)
    text = unfreeze_item(skill)
    assert "[已截断" in text
    assert len(text) < len(body)


# --- pick_candidate ----------------------------------------------------------


@pytest.mark.asyncio()
async def test_pick_candidate_validation_chain(store: ToolkitStore) -> None:
    ok = _add(store, kind="tool", name="health-probe", runtime="cli", endpoint="echo hi")
    off = _add(store, kind="tool", name="disabled-one", runtime="cli", endpoint="echo hi")
    _disable(store, off.id)

    item, reason = await pick_candidate(store, ok.id, probe=False)
    assert item is not None and reason == ""

    item, reason = await pick_candidate(store, "tk_missing", probe=False)
    assert item is None and "not found" in reason

    item, reason = await pick_candidate(store, off.id, probe=False)
    assert item is None and "disabled" in reason

    item, reason = await pick_candidate(store, ok.id, allowed_ids=set(), probe=False)
    assert item is None and "allowlist" in reason


def test_budget_constant_matches_plan() -> None:
    assert PICK_BUDGET_PER_STAGE == 8
