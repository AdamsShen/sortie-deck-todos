from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from sortie_deck.artifacts import ArtifactStore
from sortie_deck.graph import _MAX_HANDOFF_PER_TARGET, build_graph_from_template
from sortie_deck.manager import (
    BranchStatus,
    MissionManager,
    plan_digest,
    plan_digest_matches,
)
from sortie_deck.models import StageContext, StageResult
from sortie_deck.registry import ExecutorRegistry


class _Scripted:
    """Executor that always reports the same proposed_next (handoff proposal)."""

    def __init__(self, name: str, *, proposed_next: str | None = None, status: str = "ok") -> None:
        self.name = name
        self.proposed_next = proposed_next
        self.status = status

    async def run(self, ctx: StageContext) -> StageResult:
        return StageResult(
            status=self.status,  # type: ignore[arg-type]
            message=f"{ctx.stage} ran",
            artifacts={f"{ctx.stage}.md": "body"},
            proposed_next=self.proposed_next,
        )


def _registry(script: dict[str, tuple[str | None, str]]) -> ExecutorRegistry:
    reg = ExecutorRegistry()
    for name, (nxt, status) in script.items():
        reg.register(_Scripted(name, proposed_next=nxt, status=status))
    return reg


def _template(stages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": "test", "stages": stages}


def _payload(ini_id: str = "ini_handoff") -> dict[str, Any]:
    return {
        "initiative_id": ini_id,
        "thread_id": f"thr_{ini_id}",
        "brief": "test brief",
        "title": "test",
        "stage": "intake",
        "human_instruction": None,
        "coding_executor": "mock",
        "deploy_executor": "mock",
        "artifact_root": "",
        "workspace_dir": None,
        "upstream": {},
        "qa_attempt": 1,
        "events": [],
        "status": "running",
        "stopped": False,
    }


def _ran_sequence(state: dict[str, Any]) -> list[str]:
    """Every stage execution, in order (repeats kept)."""
    return [
        ev["stage"]
        for ev in state.get("events") or []
        if ev.get("stage") and str(ev.get("message", "")).endswith(" ran")
    ]


def _ran_order(state: dict[str, Any]) -> list[str]:
    """First-visit order of executed stages, plus the terminal node."""
    order: list[str] = []
    for stage in _ran_sequence(state):
        if stage not in order:
            order.append(stage)
    if state.get("stage") == "done" and "done" not in order:
        order.append("done")
    return order


async def _run(tmp_path, tpl: dict[str, Any], script: dict[str, tuple[str | None, str]]):
    registry = _registry(script)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    builder = build_graph_from_template(tpl, registry, artifacts)
    compiled = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}}
    state = await compiled.ainvoke(_payload(), config)
    return state


# --- plan digest ----------------------------------------------------------


def test_plan_digest_is_stable_and_order_sensitive() -> None:
    stages = [
        {"name": "a", "role": "product"},
        {"name": "b", "role": "eng"},
    ]
    assert plan_digest(stages) == plan_digest(list(stages))
    changed = [stages[1], stages[0]]
    assert plan_digest(stages) != plan_digest(changed)


def test_plan_digest_tracks_role_and_parallel_changes() -> None:
    base = [{"name": "a", "role": "product"}]
    assert plan_digest(base) != plan_digest([{"name": "a", "role": "qa"}])
    assert plan_digest(base) != plan_digest([{"name": "a", "role": "product", "parallel_group": "g"}])


def test_plan_digest_matches_allows_legacy_runs() -> None:
    stages = [{"name": "a", "role": "product"}]
    assert plan_digest_matches(None, stages) is True
    assert plan_digest_matches("", stages) is True
    assert plan_digest_matches(plan_digest(stages), stages) is True
    assert plan_digest_matches("deadbeef", stages) is False


# --- manager unit behaviour ----------------------------------------------


def _stages() -> list[dict[str, Any]]:
    return [
        {"name": "intake", "role": "product"},
        {"name": "web", "role": "eng_web", "parallel_group": "g"},
        {"name": "backend", "role": "eng_backend", "parallel_group": "g"},
        {"name": "integrate", "role": "eng"},
        {"name": "done", "role": "product"},
    ]


def test_manager_static_next_and_plan_membership() -> None:
    mgr = MissionManager(_stages())
    assert mgr.static_next("intake") == "web"
    assert mgr.static_next("done") == "done"
    assert mgr.is_plan_node("integrate") is True
    assert mgr.is_plan_node("nope") is False
    assert mgr.role_of("integrate") == "eng"


async def test_manager_join_all_ok_continues_without_llm() -> None:
    mgr = MissionManager(_stages(), use_llm=False)
    decision = await mgr.decide_join(
        group="g",
        last_member="backend",
        branches=[
            BranchStatus(stage="web", role="eng_web", status="ok"),
            BranchStatus(stage="backend", role="eng_backend", status="ok"),
        ],
    )
    assert decision.trigger == "join"
    assert decision.action == "continue"
    assert decision.target_stage == "integrate"
    assert decision.confidence == 1.0
    assert decision.plan_digest == plan_digest(_stages())


async def test_manager_join_reroutes_to_failed_branch() -> None:
    mgr = MissionManager(_stages(), use_llm=False)
    decision = await mgr.decide_join(
        group="g",
        last_member="backend",
        branches=[
            BranchStatus(stage="web", role="eng_web", status="ok"),
            BranchStatus(stage="backend", role="eng_backend", status="failed"),
        ],
    )
    assert decision.action == "reroute"
    assert decision.target_stage == "backend"


async def test_manager_join_uses_llm_choice_when_ambiguous() -> None:
    class _Llm:
        enabled = True
        backend = "fake"

        async def chat_json(self, *, system: str, user: str) -> dict[str, Any]:
            return {"target_stage": "integrate", "action": "continue", "confidence": 0.9}

    mgr = MissionManager(_stages(), client=_Llm(), use_llm=True)
    decision = await mgr.decide_join(
        group="g",
        last_member="backend",
        branches=[
            BranchStatus(stage="web", role="eng_web", status="failed"),
            BranchStatus(stage="backend", role="eng_backend", status="failed"),
        ],
    )
    assert decision.target_stage == "integrate"
    assert decision.action == "continue"
    assert decision.confidence == 0.75


async def test_manager_join_ignores_llm_target_outside_plan() -> None:
    class _Llm:
        enabled = True
        backend = "fake"

        async def chat_json(self, *, system: str, user: str) -> dict[str, Any]:
            return {"target_stage": "not_a_stage"}

    mgr = MissionManager(_stages(), client=_Llm(), use_llm=True)
    decision = await mgr.decide_join(
        group="g",
        last_member="backend",
        branches=[
            BranchStatus(stage="web", role="eng_web", status="failed"),
            BranchStatus(stage="backend", role="eng_backend", status="failed"),
        ],
    )
    assert decision.target_stage in {"web", "integrate"}


def test_manager_escalate_asks_for_human() -> None:
    mgr = MissionManager(_stages())
    decision = mgr.escalate(stage="integrate", question="conflicting acceptance criteria")
    assert decision.trigger == "escalate"
    assert decision.action == "request_human"
    assert decision.target_stage is None
    assert decision.confidence == 0.0


def test_manager_completion_note_marks_step() -> None:
    mgr = MissionManager(_stages())
    note = mgr.completion_note(stage="integrate", status="ok", role="eng")
    assert "integrate" in note
    assert "eng" in note
    assert "failed" in mgr.completion_note(stage="integrate", status="failed")


# --- graph: autonomous handoff -------------------------------------------


_HANDOFF_STAGES = [
    {"name": "start", "role": "product", "executor": "e_start"},
    {"name": "mid", "role": "design", "executor": "e_mid"},
    {"name": "build", "role": "eng", "executor": "e_build"},
    {"name": "verify", "role": "qa", "executor": "e_verify"},
    {"name": "done", "role": "product", "executor": "e_done"},
]

_HANDOFF_SCRIPT = {
    "e_start": ("build", "ok"),
    "e_mid": ("build", "ok"),
    "e_build": ("verify", "ok"),
    "e_verify": (None, "ok"),
    "e_done": (None, "ok"),
}


async def test_plan_default_edge_when_the_agent_proposes_nothing(tmp_path) -> None:
    """No proposal → the frozen plan's declared next node runs."""
    script = dict(_HANDOFF_SCRIPT)
    script["e_start"] = (None, "ok")
    state = await _run(tmp_path, _template(_HANDOFF_STAGES), script)
    assert _ran_order(state) == ["start", "mid", "build", "verify", "done"]


async def test_agent_handoff_jumps_to_plan_node(tmp_path) -> None:
    state = await _run(tmp_path, _template(_HANDOFF_STAGES), _HANDOFF_SCRIPT)
    order = _ran_order(state)
    # product may hand off to eng → `mid` (design) is skipped
    assert order[0] == "start"
    assert order[1] == "build"
    assert "mid" not in order
    accepted = [e for e in state["events"] if (e.get("data") or {}).get("handoff") == "accepted"]
    assert accepted
    assert accepted[0]["data"]["to"] == "eng"


async def test_agent_handoff_denied_target_falls_back_to_plan(tmp_path) -> None:
    script = dict(_HANDOFF_SCRIPT)
    # product → qa is not in product's handoff_targets ([design, eng])
    script["e_start"] = ("verify", "ok")
    state = await _run(tmp_path, _template(_HANDOFF_STAGES), script)
    denied = [e for e in state["events"] if (e.get("data") or {}).get("handoff") == "denied"]
    assert denied
    assert denied[0]["data"]["target"] == "verify"
    assert _ran_order(state)[1] == "mid"


async def test_agent_handoff_never_bypasses_hitl_gate(tmp_path) -> None:
    stages = [
        {"name": "start", "role": "product", "executor": "e_start", "hitl_after": True},
        {"name": "mid", "role": "design", "executor": "e_mid"},
        {"name": "build", "role": "eng", "executor": "e_build"},
        {"name": "done", "role": "product", "executor": "e_done"},
    ]
    registry = _registry(_HANDOFF_SCRIPT)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    builder = build_graph_from_template(_template(stages), registry, artifacts)
    compiled = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t-hitl"}}
    state = await compiled.ainvoke(_payload("ini_hitl"), config)
    assert "__interrupt__" in state
    assert "mid" not in _ran_order(state)


async def test_hitl_gate_approve_routes_to_the_plan_next_node(tmp_path) -> None:
    """A gate verdict is a node jump too, through the same router."""
    stages = [
        {"name": "start", "role": "product", "executor": "e_start", "hitl_after": True},
        {"name": "mid", "role": "design", "executor": "e_mid"},
        {"name": "done", "role": "product", "executor": "e_done"},
    ]
    registry = _registry(_HANDOFF_SCRIPT)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    builder = build_graph_from_template(_template(stages), registry, artifacts)
    compiled = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t-gate"}}
    state = await compiled.ainvoke(_payload("ini_gate"), config)
    assert "__interrupt__" in state

    resumed = await compiled.ainvoke(
        Command(resume={"action": "approve"}), {"configurable": {"thread_id": "t-gate"}}
    )
    assert "mid" in _ran_order(resumed)
    assert resumed.get("stage") == "done"
    hitl = [e for e in resumed["events"] if e.get("kind") == "hitl"]
    assert hitl and hitl[0]["message"].startswith("HITL approve")


async def test_agent_handoff_budget_stops_ping_pong(tmp_path) -> None:
    stages = [
        {"name": "start", "role": "product", "executor": "e_start"},
        {"name": "build", "role": "eng", "executor": "e_build"},
        {"name": "verify", "role": "qa", "executor": "e_verify"},
        {"name": "done", "role": "product", "executor": "e_done"},
    ]
    script = {
        "e_start": ("build", "ok"),
        "e_build": ("verify", "ok"),
        # qa → eng is allowed by contract, producing a deliberate ping-pong
        "e_verify": ("build", "ok"),
        "e_done": (None, "ok"),
    }
    state = await _run(tmp_path, _template(stages), script)
    sequence = _ran_sequence(state)
    assert sequence[0] == "start"
    assert "done" in _ran_order(state)
    # the ping-pong must terminate, not run forever
    assert len(sequence) <= 2 + 2 * _MAX_HANDOFF_PER_TARGET


# --- graph: manager join decision ----------------------------------------


_WAVE_STAGES = [
    {"name": "start", "role": "product", "executor": "e_start"},
    {"name": "web", "role": "eng_web", "executor": "e_web", "parallel_group": "g"},
    {"name": "api", "role": "eng_backend", "executor": "e_api", "parallel_group": "g"},
    {"name": "integrate", "role": "eng", "executor": "e_integrate"},
    {"name": "done", "role": "product", "executor": "e_done"},
]

_WAVE_SCRIPT = {
    "e_start": ("web", "ok"),
    "e_web": (None, "ok"),
    "e_api": (None, "ok"),
    "e_integrate": (None, "ok"),
    "e_done": (None, "ok"),
}


async def test_manager_join_records_decision_at_wave(tmp_path) -> None:
    state = await _run(tmp_path, _template(_WAVE_STAGES), _WAVE_SCRIPT)
    joins = [e for e in state["events"] if (e.get("data") or {}).get("trigger") == "join"]
    assert joins, "expected a manager join decision after the parallel wave"
    assert joins[0]["data"]["target_stage"] == "integrate"
    assert "integrate" in _ran_order(state)


async def test_manager_join_matching_the_plan_keeps_the_default_hop(tmp_path) -> None:
    """When the manager agrees with the plan, the default hop runs unchanged."""
    state = await _run(tmp_path, _template(_WAVE_STAGES), _WAVE_SCRIPT)
    joins = [e for e in state["events"] if (e.get("data") or {}).get("trigger") == "join"]
    assert joins[0]["data"]["action"] == "continue"
    assert _ran_order(state) == ["start", "web", "api", "integrate", "done"]
