"""控制平面协议：agent 自主 handoff 与两级升级（Manager → 真人）。"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from sortie_deck.artifacts import ArtifactStore
from sortie_deck.graph import _handoff_next_targets, build_graph_from_template
from sortie_deck.harness_providers.base import HarnessReply, HarnessSession, ProbeStatus
from sortie_deck.harness_providers.executor import (
    HarnessStageExecutor,
    _control_protocol,
    _parse_control_intent,
    _resolve_handoff_target,
    _strip_control_block,
)
from sortie_deck.models import StageContext, StageResult
from sortie_deck.registry import ExecutorRegistry


def _ctx(**overrides: Any) -> StageContext:
    payload: dict[str, Any] = {
        "initiative_id": "ini_ctl",
        "thread_id": "thr_ctl",
        "stage": "build",
        "role": "eng",
        "brief": "test brief",
        "artifact_dir": "/tmp/artifacts",
        "next_targets": [
            {"stage": "qa_verify", "role": "qa", "label": "QA"},
            {"stage": "deploy_preview", "role": "deploy", "label": "Deploy"},
        ],
    }
    payload.update(overrides)
    return StageContext.model_validate(payload)


# --- 协议解析 -------------------------------------------------------------


def test_parse_handoff_block() -> None:
    text = 'work done\n```control\n{"handoff": {"role": "qa", "reason": "needs tests"}}\n```'
    control = _parse_control_intent(text)
    assert control is not None
    assert control["handoff"]["role"] == "qa"


def test_parse_escalate_block() -> None:
    text = '```control\n{"escalate": {"question": "A or B?", "options": ["A", "B"]}}\n```'
    control = _parse_control_intent(text)
    assert control is not None
    assert control["escalate"]["question"] == "A or B?"


def test_parse_returns_none_without_a_valid_block() -> None:
    assert _parse_control_intent("plain reply") is None
    assert _parse_control_intent("```control\nnot json\n```") is None
    assert _parse_control_intent("```control\n{\"other\": 1}\n```") is None


def test_strip_control_block_removes_json() -> None:
    text = 'done\n```control\n{"handoff": {"role": "qa"}}\n```\n'
    assert _strip_control_block(text) == "done"


# --- 角色 → 阶段映射 -------------------------------------------------------


def test_resolve_handoff_by_role() -> None:
    ctx = _ctx()
    target, reason = _resolve_handoff_target(ctx, {"role": "qa"})
    assert target == "qa_verify"
    assert reason == ""


def test_resolve_handoff_by_stage() -> None:
    ctx = _ctx()
    target, _ = _resolve_handoff_target(ctx, {"stage": "deploy_preview"})
    assert target == "deploy_preview"


def test_resolve_handoff_rejects_unknown_role() -> None:
    ctx = _ctx()
    target, reason = _resolve_handoff_target(ctx, {"role": "design"})
    assert target is None
    assert "not in the allowed targets" in reason


def test_control_protocol_lists_targets() -> None:
    text = _control_protocol(_ctx())
    assert "`qa` → `qa_verify`" in text
    assert "`deploy` → `deploy_preview`" in text


def test_control_protocol_without_targets_still_allows_escalation() -> None:
    text = _control_protocol(_ctx(next_targets=[]))
    assert "No autonomous hand-off target" in text
    assert "escalate" in text


# --- 图侧：可交接目标生成 ---------------------------------------------------


def _stages() -> list[dict[str, Any]]:
    return [
        {"name": "start", "role": "product"},
        {"name": "design", "role": "design"},
        {"name": "build", "role": "eng"},
        {"name": "fix", "role": "eng"},
        {"name": "qa", "role": "qa", "hitl_after": True},
        {"name": "deploy", "role": "deploy"},
        {"name": "done", "role": "product"},
    ]


def test_next_targets_follow_contract_and_plan_order() -> None:
    targets = _handoff_next_targets(_stages(), "start", ["eng", "qa", "deploy"])
    # 同角色多个节点只保留最近一个；hitl_after 节点不作为自主目标
    assert targets == [
        {"stage": "build", "role": "eng", "label": "build"},
        {"stage": "deploy", "role": "deploy", "label": "deploy"},
    ]


def test_next_targets_empty_without_contract() -> None:
    assert _handoff_next_targets(_stages(), "start", []) == []


def test_next_targets_empty_for_parallel_member() -> None:
    stages = [
        {"name": "web", "role": "eng_web", "parallel_group": "g"},
        {"name": "api", "role": "eng_backend", "parallel_group": "g"},
        {"name": "qa", "role": "qa"},
        {"name": "done", "role": "product"},
    ]
    assert _handoff_next_targets(stages, "web", ["qa"]) == []


# --- 图侧：升级真人分支 -----------------------------------------------------


class _Escalating:
    """首轮升级真人，人工答复后重入返回 ok。"""

    name = "esc"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, ctx: StageContext) -> StageResult:
        self.calls += 1
        if self.calls == 1:
            return StageResult(
                status="needs_hitl",
                message="agent escalation: A or B?",
                artifacts={"reply.md": "waiting"},
                meta={"kind": "escalation", "question": "A or B?", "options": ["A", "B"]},
            )
        return StageResult(status="ok", message="resumed", artifacts={"reply.md": "ok"})


class _Pass:
    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, ctx: StageContext) -> StageResult:
        return StageResult(status="ok", message=f"{ctx.stage} ran", artifacts={"x.md": "body"})


def _payload() -> dict[str, Any]:
    return {
        "initiative_id": "ini_esc",
        "thread_id": "thr_esc",
        "brief": "test brief",
        "title": "test",
        "stage": "start",
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


def _interrupt_payload(state: dict[str, Any]) -> dict[str, Any]:
    interrupts = state["__interrupt__"]
    first = interrupts[0]
    value = getattr(first, "value", first)
    return dict(value) if isinstance(value, dict) else {"value": value}


async def test_escalation_gate_pauses_with_its_own_kind(tmp_path) -> None:
    stages = [
        {"name": "start", "role": "product", "executor": "esc"},
        {"name": "build", "role": "eng", "executor": "build"},
        {"name": "done", "role": "product", "executor": "done"},
    ]
    registry = ExecutorRegistry()
    registry.register(_Escalating())
    registry.register(_Pass("build"))
    registry.register(_Pass("done"))
    builder = build_graph_from_template(
        {"name": "test", "stages": stages}, registry, ArtifactStore(tmp_path / "artifacts")
    )
    compiled = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t-esc"}}

    state = await compiled.ainvoke(_payload(), config)
    payload = _interrupt_payload(state)
    assert payload["kind"] == "escalation"
    assert payload["question"] == "A or B?"
    assert payload["options"] == ["A", "B"]

    # 真人回答后被当作业务指令重入本阶段：不提权、不触发 publish
    resumed = await compiled.ainvoke(
        Command(resume={"action": "edit_instruction", "instruction": "选 A"}),
        config,
    )
    assert resumed.get("stage") == "done"
    assert "sortie:coding_permission_mode" not in str(resumed.get("human_instruction") or "")
    assert resumed.get("publish_pending") is None
    hitl = [e["message"] for e in resumed.get("events") or [] if e.get("kind") == "hitl"]
    assert "escalation requested — waiting for a human" in hitl
    assert "HITL edit_instruction (escalation)" in hitl


# --- harness 执行器：控制意图 → StageResult --------------------------------


class _FakeProvider:
    def __init__(self, replies: list[HarnessReply]) -> None:
        self.replies = replies
        self.calls: list[str] = []

    async def start_session(self, **kwargs: Any) -> HarnessSession:
        return HarnessSession(provider="fake", meta=dict(kwargs))

    async def send(self, session: Any, prompt: str, **kwargs: Any) -> HarnessReply:
        self.calls.append(prompt)
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


class _FakeRegistry:
    def __init__(self, provider: _FakeProvider) -> None:
        self.provider = provider

    def has(self, name: str) -> bool:
        return True

    def probe(self, name: str) -> ProbeStatus:
        return ProbeStatus(status="ok", detail="fake")

    def get(self, name: str) -> _FakeProvider:
        return self.provider


def _patch_harness(monkeypatch: Any, tmp_path: Any, replies: list[HarnessReply]) -> _FakeProvider:
    import sortie_deck.harness_providers.base as base_module
    import sortie_deck.mission_memory as memory_module
    import sortie_deck.plugins.coding as coding_module

    provider = _FakeProvider(replies)
    monkeypatch.setattr(base_module, "get_harness_registry", lambda: _FakeRegistry(provider))

    async def _pack(self: Any, ctx: StageContext) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(HarnessStageExecutor, "_build_context_pack", _pack)
    monkeypatch.setattr(coding_module, "ensure_worktree", lambda *a, **k: tmp_path)

    async def _clone(*a: Any, **k: Any) -> tuple[str, list[str]]:
        return "", []

    monkeypatch.setattr(coding_module, "clone_repos", _clone)

    class _Memory:
        def record_turn(self, *a: Any, **k: Any) -> None:
            return None

        def append_memory(self, *a: Any, **k: Any) -> None:
            return None

        async def maybe_summarize(self, *a: Any, **k: Any) -> None:
            return None

    monkeypatch.setattr(memory_module, "get_mission_memory", lambda: _Memory())

    async def _summary(**kwargs: Any) -> str:
        return ""

    monkeypatch.setattr(memory_module, "build_stage_summary", _summary)
    return provider


async def test_harness_handoff_becomes_proposed_next(tmp_path, monkeypatch) -> None:
    _patch_harness(
        monkeypatch,
        tmp_path,
        [HarnessReply(text='done\n```control\n{"handoff": {"role": "qa"}}\n```', status="ok")],
    )
    result = await HarnessStageExecutor().run(_ctx(harness="fake"))
    assert result.status == "ok"
    assert result.proposed_next == "qa_verify"
    assert result.meta["control_intent"]["handoff"]["accepted"] == "qa_verify"
    assert "```control" not in result.artifacts["reply.md"]


async def test_harness_handoff_outside_targets_is_rejected(tmp_path, monkeypatch) -> None:
    _patch_harness(
        monkeypatch,
        tmp_path,
        [
            HarnessReply(
                text='implemented\n```control\n{"handoff": {"role": "design"}}\n```', status="ok"
            )
        ],
    )
    result = await HarnessStageExecutor().run(_ctx(harness="fake"))
    assert result.proposed_next == "qa_verify"  # 编码角色的默认落点，不是 design
    assert "not in the allowed targets" in result.meta["control_intent"]["handoff"]["rejected"]


async def test_harness_escalation_is_answered_by_the_manager(tmp_path, monkeypatch) -> None:
    provider = _patch_harness(
        monkeypatch,
        tmp_path,
        [
            HarnessReply(
                text='```control\n{"escalate": {"question": "A or B?"}}\n```', status="ok"
            ),
            HarnessReply(text="final answer", status="ok"),
        ],
    )

    class _Host:
        async def open_subroom(self, **kwargs: Any) -> str:
            return ""

        async def escalate(self, question: str) -> str:
            return "manager: 选 A"

    result = await HarnessStageExecutor().run(_ctx(harness="fake"), host=_Host())
    assert result.status == "ok"
    assert result.meta["control_intent"]["escalation"] == {"level": "manager", "resolution": "选 A"}
    assert len(provider.calls) == 2
    assert "final answer" in result.artifacts["reply.md"]


async def test_harness_escalation_without_manager_answer_waits_for_a_human(
    tmp_path, monkeypatch
) -> None:
    _patch_harness(
        monkeypatch,
        tmp_path,
        [HarnessReply(text='```control\n{"escalate": {"question": "A or B?"}}\n```', status="ok")],
    )

    class _Host:
        async def open_subroom(self, **kwargs: Any) -> str:
            return ""

        async def escalate(self, question: str) -> str:
            return "escalated to a human — decision `dec_1`"

    result = await HarnessStageExecutor().run(_ctx(harness="fake"), host=_Host())
    assert result.status == "needs_hitl"
    assert result.meta["kind"] == "escalation"
    assert result.meta["question"] == "A or B?"
