from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sortie_deck.artifacts import ArtifactStore
from sortie_deck.graph import build_graph_from_template
from sortie_deck.memory import MemoryStore
from sortie_deck.models import (
    Initiative,
    RoleAgent,
    RoleId,
    StageContext,
    StageResult,
)
from sortie_deck.plugins.mock import MockCollaborativeExecutor
from sortie_deck.registry import ExecutorRegistry, run_executor, supports_session
from sortie_deck.rooms import RoomStore
from sortie_deck.subroom import SubRoomService, SubRoomStore, set_subroom_service


class _SingleShot:
    name = "single"

    async def run(self, ctx: StageContext) -> StageResult:
        return StageResult(status="ok", message="single shot", artifacts={"x.md": "b"})


class _Session:
    name = "session"

    async def run(self, ctx: StageContext) -> StageResult:
        return StageResult(status="ok", message="ran without host", artifacts={"x.md": "no-host"})

    async def run_session(self, ctx: StageContext, host: Any) -> StageResult:
        conclusion = await host.open_subroom(goal="need alignment")
        return StageResult(
            status="ok", message="ran with host", artifacts={"x.md": conclusion or "empty"}
        )


def _ctx() -> StageContext:
    return StageContext(
        initiative_id="ini_1",
        thread_id="thr_1",
        stage="build",
        role=RoleId.ENG,
        brief="brief",
        artifact_dir="/tmp/x",
    )


def test_supports_session_detects_the_opt_in_method() -> None:
    assert supports_session(_SingleShot()) is False
    assert supports_session(_Session()) is True


async def test_run_executor_delegates_to_run_without_a_host() -> None:
    result = await run_executor(_Session(), _ctx(), None)
    assert result.message == "ran without host"


async def test_run_executor_prefers_run_session_when_a_host_is_present() -> None:
    class _Host:
        async def open_subroom(self, **kwargs: Any) -> str:
            return "conclusion"

        async def escalate(self, question: str) -> str:
            return ""

    result = await run_executor(_Session(), _ctx(), _Host())
    assert result.message == "ran with host"
    assert result.artifacts["x.md"] == "conclusion"


async def test_run_executor_keeps_plain_executors_working() -> None:
    result = await run_executor(_SingleShot(), _ctx(), object())
    assert result.message == "single shot"


async def test_mock_collab_executor_falls_back_without_a_host() -> None:
    result = await MockCollaborativeExecutor().run(_ctx())
    assert "single-shot" in result.message


async def test_mock_collab_executor_uses_the_subroom_conclusion() -> None:
    class _Host:
        async def open_subroom(self, **kwargs: Any) -> str:
            return "边界以 PRD 为准"

        async def escalate(self, question: str) -> str:
            return ""

    result = await MockCollaborativeExecutor().run_session(_ctx(), _Host())
    assert "边界以 PRD 为准" in result.artifacts["build.md"]


# --- graph end-to-end ----------------------------------------------------


class _StubOrchestrator:
    def __init__(self, ini: Initiative) -> None:
        self._ini = ini

    def get(self, initiative_id: str) -> Initiative | None:
        return self._ini if initiative_id == self._ini.id else None

    async def _discussion_reply(self, ini, role, question, from_role) -> str:  # noqa: ANN001
        return f"{role.role.value} says: agreed"


def _ini() -> Initiative:
    return Initiative(
        id="ini_collab",
        title="collab",
        brief="brief",
        roles=[
            RoleAgent(
                id="r_eng", role=RoleId.ENG, title="Eng", persona="p", executor="mock_collab"
            ),
            RoleAgent(id="r_qa", role=RoleId.QA, title="QA", persona="p", executor="mock"),
            RoleAgent(
                id="r_design", role=RoleId.DESIGN, title="Design", persona="p", executor="mock"
            ),
        ],
    )


@pytest.fixture()
def subroom_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from sortie_deck import orchestrator as orch_module
    from sortie_deck.settings import settings

    memory = MemoryStore(tmp_path / "memory.json")
    service = SubRoomService(
        store=SubRoomStore(tmp_path / "subrooms.json"),
        rooms=RoomStore(tmp_path / "rooms"),
        memory=memory,
        enabled=True,
    )
    set_subroom_service(service)
    monkeypatch.setattr(settings, "subroom_enabled", True, raising=False)
    monkeypatch.setattr(orch_module, "get_orchestrator", lambda: _StubOrchestrator(_ini()))
    yield service, memory
    set_subroom_service(None)


async def test_graph_stage_runs_the_session_and_returns_the_conclusion(
    tmp_path: Path, subroom_on
) -> None:
    service, memory = subroom_on
    stages = [
        {"name": "build", "role": "eng", "executor": "mock_collab"},
        {"name": "done", "role": "product", "executor": "e_done"},
    ]
    registry = ExecutorRegistry()
    registry.register(MockCollaborativeExecutor())
    builder = build_graph_from_template(
        {"name": "t", "stages": stages}, registry, ArtifactStore(tmp_path / "artifacts")
    )
    compiled = builder.compile(checkpointer=InMemorySaver())
    state = await compiled.ainvoke(
        {
            "initiative_id": "ini_collab",
            "thread_id": "thr_collab",
            "brief": "brief",
            "title": "t",
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
        },
        {"configurable": {"thread_id": "thr_collab"}},
    )

    subroom_events = [
        e for e in state["events"] if (e.get("data") or {}).get("subroom") == "closed"
    ]
    assert subroom_events, "the sub-room should be surfaced as a stage event"
    assert subroom_events[0]["data"]["participants"] == ["qa", "design"]

    # transcript isolated, conclusion in stage-scoped memory
    subs = service.store.list(initiative_id="ini_collab")
    assert len(subs) == 1
    assert service.main_room("ini_collab") == []
    assert service.transcript(subs[0].id)
    brief = memory.context_block(mission_id="ini_collab", stage="build")
    assert "sub-room" in brief
    assert "sub-room" not in memory.context_block(mission_id="ini_collab", stage="verify")
