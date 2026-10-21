"""阶段入口主动召回（graph.run_stage_recall）的行为测试。

覆盖：分数阈值过滤、与绑定/已上浮条目去重、探活失败丢弃、
三类条目（tool/mcp/skill）的落点分类、总开关关闭零行为。
幂等标记（stage_recall_done）位于 graph.run_stage 的嵌套 helper 内，
依赖完整 PipelineState，此处测 helper 层的全部纯行为分支。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sortie_deck.graph import run_stage_recall
from sortie_deck.toolkit import CreateToolkitRequest, ToolkitStore


@pytest.fixture()
def store(tmp_path: Path) -> ToolkitStore:
    return ToolkitStore(tmp_path / "toolkit.json")


def _add(store: ToolkitStore, **kwargs: Any):
    return store.create(
        CreateToolkitRequest(**kwargs), author_id="tester", author_name="Tester"
    )


@pytest.mark.asyncio()
async def test_recall_floats_matching_items_by_kind(store: ToolkitStore) -> None:
    """三类条目按 kind 归入各自落点：tool/mcp/skill 分列，brief 含上浮文本。"""
    tool = _add(store, kind="tool", name="git-diff", runtime="cli", endpoint="git diff --stat")
    mcp = _add(
        store,
        kind="mcp",
        name="pg-monitor",
        summary="查询 postgres 慢查询日志",
        transport="http",
        url="http://127.0.0.1:9999/mcp",
    )
    skill = _add(
        store,
        kind="skill",
        name="slowlog-triage",
        summary="分析 postgres 慢查询日志并给出索引建议",
        tags=["postgres", "慢查询日志"],
        body="# Skill: slowlog triage\n1. 读日志\n2. 归因",
    )
    out = await run_stage_recall(
        store,
        stage="eng_backend",
        queries=["git diff", "postgres 慢查询日志"],
        switch=True,
    )
    assert tool.id in out.tool_ids
    assert mcp.id in out.mcp_ids
    assert skill.id in out.skill_ids
    assert "git diff --stat" in out.brief_append
    assert "Proactively recalled" in out.brief_append


@pytest.mark.asyncio()
async def test_threshold_drops_low_score_results(store: ToolkitStore) -> None:
    """召回分数全部低于阈值时不注入（'无需则不调用'的实现）。"""
    _add(store, kind="tool", name="git-diff", runtime="cli", endpoint="git diff --stat")
    out = await run_stage_recall(
        store,
        stage="s",
        queries=["完全不相关的查询词"],
        min_score=0.9,
        switch=True,
    )
    assert out.brief_append == "" and out.item_ids == []


@pytest.mark.asyncio()
async def test_dedup_skips_bound_and_floated(store: ToolkitStore) -> None:
    """与已有绑定/已上浮重叠的条目跳过（'重叠则使用已有工具'）。"""
    tool = _add(store, kind="tool", name="git-diff", runtime="cli", endpoint="git diff --stat")
    skill = _add(
        store,
        kind="skill",
        name="slowlog-triage",
        summary="分析 postgres 慢查询日志",
        body="body",
    )
    out = await run_stage_recall(
        store,
        stage="eng",
        queries=["git diff", "postgres 慢查询日志"],
        bound_ids={tool.id},
        floated_ids={skill.id},
        switch=True,
    )
    assert tool.id not in out.item_ids
    assert skill.id not in out.item_ids


@pytest.mark.asyncio()
async def test_probe_failure_drops_item(store: ToolkitStore) -> None:
    """探活失败的条目不注入（注入调不上的工具比不注入更糟）。"""
    broken = _add(
        store,
        kind="mcp",
        name="broken-mcp",
        summary="postgres 慢查询日志",
        transport="stdio",
        command="definitely-not-a-real-command-xyz",
    )
    out = await run_stage_recall(
        store,
        stage="eng",
        queries=["postgres 慢查询日志"],
        switch=True,
    )
    # store 初始化含种子条目，可能有其他条目命中；只断言探活失败的条目被丢弃
    assert broken.id not in out.item_ids and out.mcp_ids == []


@pytest.mark.asyncio()
async def test_switch_off_is_noop(store: ToolkitStore) -> None:
    """总开关关闭时零行为（与 loadout_stage_recall=False 对应）。"""
    _add(store, kind="tool", name="git-diff", runtime="cli", endpoint="git diff --stat")
    out = await run_stage_recall(
        store, stage="s", queries=["git diff"], switch=False
    )
    assert out.brief_append == "" and out.item_ids == []


@pytest.mark.asyncio()
async def test_store_none_is_noop() -> None:
    """工具箱存储缺失时安静返回空结果。"""
    out = await run_stage_recall(None, stage="s", queries=["anything"], switch=True)
    assert out.brief_append == "" and out.item_ids == []


@pytest.mark.asyncio()
async def test_never_raises_on_recall_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """召回层抛异常时 fail-soft：返回空结果而非中断阶段。"""
    from sortie_deck import graph as graph_mod

    def _boom(*args: Any, **kwargs: Any):
        raise RuntimeError("weknora down")

    monkeypatch.setattr(graph_mod, "recall", _boom)
    out = await run_stage_recall(
        object(), stage="s", queries=["q"], switch=True  # store 不会被用到（recall 被 mock）
    )
    assert out.brief_append == "" and out.item_ids == []
