"""建队瀑布图的行为契约测试。

锁住三件事（改图之前先看这里）：
1. 节点执行顺序固定为 历史 → 知识库 → 航线 → 编制 → 角色；
2. 事件序列（``EventKind.WATERFALL`` + ``data.step``）与旧实现逐条一致，
   前端瀑布动画依赖它；
3. 工具失败不中断流程，只是不写入 context。
"""

from __future__ import annotations

import pytest

from sortie_deck.harness import (
    EventKind,
    Harness,
    HarnessState,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from sortie_deck.harness.waterfall import (
    apply_tool_result,
    reset_waterfall_graph,
    run_waterfall,
)

_ORDER = (
    "search_history",
    "retrieve_knowledge",
    "propose_pipeline",
    "propose_loadout",
    "ensure_missing_roles",
)

_STEPS = ("history", "knowledge", "pipeline", "loadout", "roles")

_FAKE_DATA: dict[str, dict] = {
    "search_history": {"hits": [{"id": "ini_old"}]},
    "retrieve_knowledge": {"hits": [{"id": "kb_1"}]},
    "propose_pipeline": {"pipeline": {"track": "standard"}, "stages": [{"id": "intake"}]},
    "propose_loadout": {
        "needed_slots": ["product", "qa"],
        "missing_slots": [{"role_key": "pet_funeral"}],
        "role_agents": {"product": "agt_1"},
        "loadout_planner": {"planner": "llm"},
    },
    "ensure_missing_roles": {
        "created": [{"slot": "qa", "id": "agt_2"}],
        "forged": [{"role_key": "pet_funeral"}],
    },
}


@pytest.fixture(autouse=True)
def _reset_graph():
    reset_waterfall_graph()
    yield
    reset_waterfall_graph()


def _bind(
    calls: list[tuple[str, dict]],
    *,
    failing: set[str] | None = None,
) -> tuple[Harness, HarnessState]:
    """构造带 5 个假工具的 Harness；failing 中的工具返回 ok=False。"""
    registry = ToolRegistry()
    failing = failing or set()

    for name in _ORDER:

        async def handler(args, _name=name):
            calls.append((_name, args))
            if _name in failing:
                return ToolResult(ok=False, summary="forced failure")
            return ToolResult(ok=True, data=_FAKE_DATA[_name])

        registry.register(ToolSpec(name, name, handler))

    harness = Harness(registry, name="test-waterfall")
    state = HarnessState(session_id="s1")
    state.status = "planning"
    return harness, state


async def _run(harness: Harness, state: HarnessState) -> list[str]:
    return await run_waterfall(
        harness=harness,
        harness_state=state,
        title="门户健康检查",
        composed="做一个门户预发健康检查",
        discussion="历史对话\n\n做一个门户预发健康检查",
    )


async def test_runs_five_tools_in_fixed_order():
    calls: list[tuple[str, dict]] = []
    harness, state = _bind(calls)

    steps = await _run(harness, state)

    assert [name for name, _ in calls] == list(_ORDER)
    assert steps == list(_ORDER)


async def test_emits_waterfall_steps_in_legacy_order():
    calls: list[tuple[str, dict]] = []
    harness, state = _bind(calls)

    await _run(harness, state)

    waterfall = [e for e in state.events if e.kind == EventKind.WATERFALL]
    assert [e.data.get("step") for e in waterfall] == list(_STEPS)
    assert [e.text for e in waterfall] == [
        "① 查阅历史任务",
        "② 检索知识库",
        "③ 提案交付航线",
        "④ 匹配小队编制",
        "⑤ 补齐缺失角色",
    ]


async def test_later_args_read_previous_context():
    calls: list[tuple[str, dict]] = []
    harness, state = _bind(calls)

    await _run(harness, state)
    args_by_tool = dict(calls)

    # 前两步用的是文档里约定的两个不同文本
    assert args_by_tool["search_history"] == {"query": "历史对话\n\n做一个门户预发健康检查"}
    assert args_by_tool["retrieve_knowledge"] == {"query": "做一个门户预发健康检查"}
    assert args_by_tool["propose_pipeline"]["title"] == "门户健康检查"
    # stages 来自 propose_pipeline 写入的 context
    assert args_by_tool["propose_loadout"]["stages"] == [{"id": "intake"}]
    # slots / missing 来自 propose_loadout 写入的 context
    assert args_by_tool["ensure_missing_roles"] == {
        "slots": ["product", "qa"],
        "missing": [{"role_key": "pet_funeral"}],
    }


async def test_context_mapping_matches_draft_contract():
    calls: list[tuple[str, dict]] = []
    harness, state = _bind(calls)

    await _run(harness, state)

    ctx = state.context
    assert ctx["history_hits"] == [{"id": "ini_old"}]
    assert ctx["knowledge_hits"] == [{"id": "kb_1"}]
    assert ctx["pipeline"] == {"track": "standard"}
    assert ctx["stages"] == [{"id": "intake"}]
    assert ctx["needed_slots"] == ["product", "qa"]
    assert ctx["missing_slots"] == [{"role_key": "pet_funeral"}]
    assert ctx["loadout_planner"] == {"planner": "llm"}
    assert ctx["created_agents"] == [{"slot": "qa", "id": "agt_2"}]
    assert ctx["forged_agents"] == [{"role_key": "pet_funeral"}]
    # created 的角色合并进 loadout 产出的 role_agents，不覆盖已有槽位
    assert ctx["role_agents"] == {"product": "agt_1", "qa": "agt_2"}


async def test_tool_failure_does_not_break_the_chain():
    calls: list[tuple[str, dict]] = []
    harness, state = _bind(calls, failing={"propose_pipeline"})

    steps = await _run(harness, state)

    # 五步仍然全部执行
    assert steps == list(_ORDER)
    assert len(calls) == 5
    # 失败步骤的产物不写入 context，后续节点读到空 stages
    assert "pipeline" not in state.context
    assert dict(calls)["propose_loadout"]["stages"] == []
    # 事件照常产出，前端瀑布不会断
    waterfall = [e for e in state.events if e.kind == EventKind.WATERFALL]
    assert len(waterfall) == 5


async def test_apply_tool_result_ignores_unknown_tool():
    ctx: dict = {}
    apply_tool_result(ctx, "some_new_tool", {"hits": [1]})
    assert ctx == {}
