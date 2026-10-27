"""建队规划的五步确定性瀑布 —— LangGraph 图编排。

为什么是固定边而不是 ``create_react_agent``：
前端 ``MissionBriefingChat`` 依赖固定的事件序列（``step=history/knowledge/
pipeline/loadout/roles``）渲染瀑布动画，``draft`` 的构造也假设这五步必然跑完。
让模型自主决定调哪些工具、调几次属于行为变更，会同时打破前端契约与
``ensure_missing_roles``（有建角色副作用）的确定性，收益为负。

移植自 ``mission_briefing.py`` 中原本硬编码在接口层的五步调用，逐行对齐：
- 事件顺序：先 ``emit`` 再 ``call_tool``，文案一字不改
- 工具参数：后一步依赖前一步写入的 ``context``，因此参数在节点执行时才求值
- 失败容忍：工具失败不中断流程（``call_tool`` 已把异常收成 ``ok=False``），
  只是不写 ``context``，与旧实现一致

不配置 checkpointer：会话持久化仍由 ``BriefingStore`` 负责，图是一次性 DAG，
双写会带来两份真相。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypedDict

from langchain_core.runnables import RunnableConfig

from sortie_deck.harness import EventKind

if TYPE_CHECKING:
    from sortie_deck.harness import Harness, HarnessState


class BriefingWaterfallState(TypedDict):
    """瀑布图状态。

    刻意不放 ``context``：它始终通过 ``config["configurable"]["harness_state"]``
    取用，保证节点内写入的就是 ``HarnessState.context`` 那个 dict 对象本身，
    避免图状态复制导致 ``draft`` 读不到工具结果。
    """

    title: str
    composed: str
    discussion: str
    steps: list[str]


def apply_tool_result(context: dict[str, Any], name: str, data: dict[str, Any]) -> None:
    """把工具返回写入会话上下文。

    ``data`` 为 ``ToolResult.data``；只做字段搬运，不做业务判断，
    保证 ``mission_briefing`` 后续构造 ``draft`` 时读到的键名与旧实现一致。
    """
    if name == "search_history":
        context["history_hits"] = data.get("hits") or []
    elif name == "retrieve_knowledge":
        context["knowledge_hits"] = data.get("hits") or []
    elif name == "propose_pipeline":
        context["pipeline"] = data.get("pipeline")
        context["stages"] = data.get("stages") or []
    elif name == "propose_loadout":
        context["needed_slots"] = data.get("needed_slots") or []
        # 目录外角色（待 forge + 人工审核），随编制位一起进入建队
        context["missing_slots"] = data.get("missing_slots") or []
        context["role_agents"] = data.get("role_agents") or {}
        context["loadout_planner"] = data.get("loadout_planner") or {}
        context["stages"] = context.get("stages") or []
    elif name == "ensure_missing_roles":
        created = data.get("created") or []
        context["created_agents"] = created
        context["forged_agents"] = data.get("forged") or []
        role_agents = dict(context.get("role_agents") or {})
        for c in created:
            role_agents[c["slot"]] = c["id"]
        context["role_agents"] = role_agents


def _bindings(config: RunnableConfig | None) -> tuple[Harness, HarnessState]:
    """从 config 取出 harness 与其状态。

    走 ``configurable`` 而不是图状态，避免每次调用重新编译图。
    """
    conf = (config or {}).get("configurable") or {}
    harness = conf.get("harness")
    harness_state = conf.get("harness_state")
    if harness is None or harness_state is None:
        raise RuntimeError(
            "waterfall graph requires `harness` and `harness_state` "
            "in config['configurable']"
        )
    return harness, harness_state


BuildArgs = Callable[[BriefingWaterfallState, "HarnessState"], dict[str, Any]]


def _node(
    *,
    step: str,
    label: str,
    tool: str,
    build_args: BuildArgs,
) -> Callable[[BriefingWaterfallState, dict[str, Any]], Awaitable[dict[str, Any]]]:
    """生成一个节点：emit 事件 → 调工具 → 写上下文。

    ``step``/``label`` 是前端契约（瀑布动画的序号与文案），改动需同步前端。
    """

    async def run(
        state: BriefingWaterfallState, config: RunnableConfig
    ) -> dict[str, Any]:
        harness, harness_state = _bindings(config)
        harness.emit(harness_state, EventKind.WATERFALL, label, step=step)
        result = await harness.call_tool(harness_state, tool, build_args(state, harness_state))
        # 工具失败时 call_tool 返回 ok=False，这里跳过写入但不中断后续步骤
        if result.ok and result.data:
            apply_tool_result(harness_state.context, tool, result.data)
        return {"steps": [*state["steps"], tool]}

    return run


def build_briefing_waterfall_graph() -> Any:
    """构造并编译五步瀑布图（纯函数，便于测试里独立构建）。"""
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(BriefingWaterfallState)
    graph.add_node(
        "search_history",
        _node(
            step="history",
            label="① 查阅历史任务",
            tool="search_history",
            build_args=lambda s, hs: {"query": s["discussion"]},
        ),
    )
    graph.add_node(
        "retrieve_knowledge",
        _node(
            step="knowledge",
            label="② 检索知识库",
            tool="retrieve_knowledge",
            build_args=lambda s, hs: {"query": s["composed"]},
        ),
    )
    graph.add_node(
        "propose_pipeline",
        _node(
            step="pipeline",
            label="③ 提案交付航线",
            tool="propose_pipeline",
            build_args=lambda s, hs: {"title": s["title"], "brief": s["discussion"]},
        ),
    )
    graph.add_node(
        "propose_loadout",
        _node(
            step="loadout",
            label="④ 匹配小队编制",
            tool="propose_loadout",
            # stages 来自上一步 propose_pipeline 写入的 context
            build_args=lambda s, hs: {
                "brief": s["discussion"],
                "stages": hs.context.get("stages") or [],
            },
        ),
    )
    graph.add_node(
        "ensure_missing_roles",
        _node(
            step="roles",
            label="⑤ 补齐缺失角色",
            tool="ensure_missing_roles",
            build_args=lambda s, hs: {
                "slots": hs.context.get("needed_slots") or [],
                # 目录外角色的标题/能力描述：forge 草稿据此生成，比裸 slot 名可读
                "missing": hs.context.get("missing_slots") or [],
            },
        ),
    )

    graph.add_edge(START, "search_history")
    graph.add_edge("search_history", "retrieve_knowledge")
    graph.add_edge("retrieve_knowledge", "propose_pipeline")
    graph.add_edge("propose_pipeline", "propose_loadout")
    graph.add_edge("propose_loadout", "ensure_missing_roles")
    graph.add_edge("ensure_missing_roles", END)
    return graph.compile()


_GRAPH: Any | None = None


def briefing_waterfall_graph() -> Any:
    """取编译缓存（图结构固定，进程内只需编译一次）。"""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_briefing_waterfall_graph()
    return _GRAPH


def reset_waterfall_graph() -> None:
    """清空编译缓存（测试用）。"""
    global _GRAPH
    _GRAPH = None


async def run_waterfall(
    *,
    harness: Harness,
    harness_state: HarnessState,
    title: str,
    composed: str,
    discussion: str,
) -> list[str]:
    """按固定顺序跑完五步瀑布，返回实际执行的工具名序列。

    调用前 ``harness_state.status`` 应为 ``planning``（由调用方设置）。
    """
    result = await briefing_waterfall_graph().ainvoke(
        {
            "title": title,
            "composed": composed,
            "discussion": discussion,
            "steps": [],
        },
        config={
            "configurable": {
                "harness": harness,
                "harness_state": harness_state,
            }
        },
    )
    return list(result.get("steps") or [])
