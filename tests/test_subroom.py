from __future__ import annotations

from pathlib import Path

import pytest

from sortie_deck.memory import MemoryStore
from sortie_deck.rooms import MAIN_ROOM, RoomStore
from sortie_deck.subroom import (
    MAX_SUBROOM_DEPTH,
    StageSessionHost,
    SubRoomService,
    SubRoomStore,
    estimate_tokens,
)


@pytest.fixture()
def rooms(tmp_path: Path) -> RoomStore:
    return RoomStore(tmp_path / "rooms")


@pytest.fixture()
def memory(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory.json")


def _service(
    tmp_path: Path,
    rooms: RoomStore,
    memory: MemoryStore,
    *,
    enabled: bool = True,
    max_tokens: int = 20000,
    max_rounds: int = 2,
    timeout_seconds: float = 5.0,
) -> SubRoomService:
    return SubRoomService(
        store=SubRoomStore(tmp_path / "subrooms.json"),
        rooms=rooms,
        memory=memory,
        enabled=enabled,
        max_tokens=max_tokens,
        max_rounds=max_rounds,
        timeout_seconds=timeout_seconds,
    )


def _reply(prefix: str = "answer"):
    async def _fn(role: str, prompt: str) -> str:
        return f"{prefix}:{role}"

    return _fn


async def test_open_disabled_returns_none(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory, enabled=False)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        participants=["qa"],
        reply=_reply(),
    )
    assert sub is None


async def test_open_without_participants_returns_none(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        participants=[],
        reply=_reply(),
    )
    assert sub is None


async def test_open_excludes_the_initiator_from_the_roster(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        participants=["eng", "qa", "design"],
        reply=_reply(),
    )
    assert sub is not None
    assert sub.participants == ["qa", "design"]


async def test_open_produces_a_conclusion(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        questions=["失败路径怎么定义？"],
        participants=["qa", "design"],
        reply=_reply(),
    )
    assert sub is not None
    assert sub.status == "closed"
    assert sub.conclusion
    assert sub.turns, "expected a transcript"
    assert sub.manager_digest, "sub-room gets its own manager identity"


async def test_depth_cap_refuses_nested_subrooms(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="再来一个小群",
        participants=["qa"],
        depth=MAX_SUBROOM_DEPTH,
        reply=_reply(),
    )
    assert sub is None
    rejected = [r for r in svc.store.list(initiative_id="ini_1") if r.status == "rejected"]
    assert rejected
    assert "nesting cap" in rejected[0].reason


async def test_transcript_stays_out_of_the_main_room(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        participants=["qa"],
        reply=_reply(),
    )
    assert sub is not None
    assert svc.main_room("ini_1") == []
    transcript = svc.transcript(sub.id)
    assert transcript
    assert all(m.room_key != MAIN_ROOM for m in transcript)
    assert all(m.msg_type == "subroom" for m in transcript)


async def test_conclusion_is_written_as_stage_scoped_memory(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        participants=["qa"],
        reply=_reply(),
    )
    assert sub is not None
    entries = [e for e in memory.list(scope="mission", scope_id="ini_1")]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "episode"
    assert "stage:build" in entry.tags
    assert sub.conclusion in entry.content


async def test_stage_scoped_memory_only_reaches_its_own_stage(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory)
    await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        participants=["qa"],
        reply=_reply(),
    )
    own = memory.context_block(mission_id="ini_1", stage="build")
    other = memory.context_block(mission_id="ini_1", stage="verify")
    assert "sub-room" in own
    assert "sub-room" not in other
    # no stage filter at all → stage-tagged memories are hidden
    assert "sub-room" not in memory.context_block(mission_id="ini_1")


async def test_token_budget_stops_the_discussion(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory, max_tokens=1, max_rounds=5)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        participants=["qa", "design"],
        reply=_reply(),
    )
    assert sub is not None
    assert sub.token_estimate >= 1
    assert len(sub.turns) <= 2


async def test_timeout_marks_the_subroom(tmp_path, rooms, memory) -> None:
    async def _slow(role: str, prompt: str) -> str:
        import asyncio

        await asyncio.sleep(0.5)
        return "late"

    svc = _service(tmp_path, rooms, memory, timeout_seconds=0.05)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        participants=["qa"],
        reply=_slow,
    )
    assert sub is not None
    assert sub.status == "timeout"
    assert "budget" in sub.reason


async def test_failing_participant_does_not_break_the_subroom(tmp_path, rooms, memory) -> None:
    async def _flaky(role: str, prompt: str) -> str:
        if role == "qa":
            raise RuntimeError("teammate offline")
        return f"ok:{role}"

    svc = _service(tmp_path, rooms, memory)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        participants=["qa", "design"],
        reply=_flaky,
    )
    assert sub is not None
    assert sub.status == "closed"
    assert any("no reply" in t.text for t in sub.turns)


def test_estimate_tokens_is_positive() -> None:
    assert estimate_tokens("") >= 1
    assert estimate_tokens("abcd" * 25) == 25


# --- session host ---------------------------------------------------------


async def test_host_open_subroom_returns_conclusion_and_emits_event(
    tmp_path, rooms, memory
) -> None:
    svc = _service(tmp_path, rooms, memory)
    events: list[dict] = []
    host = StageSessionHost(
        initiative_id="ini_1",
        stage="build",
        role="eng",
        participants=["qa", "design"],
        reply=_reply(),
        service=svc,
        on_event=events.append,
    )
    conclusion = await host.open_subroom(goal="对齐验收", questions=["失败路径？"])
    assert conclusion
    assert host.subrooms
    assert events and events[0]["kind"] == "subroom"
    assert events[0]["data"]["subroom"] == "closed"


async def test_host_never_invites_the_initiator(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory)
    host = StageSessionHost(
        initiative_id="ini_1",
        stage="build",
        role="eng",
        participants=["eng", "qa"],
        reply=_reply(),
        service=svc,
    )
    await host.open_subroom(goal="对齐验收")
    sub = svc.store.list(initiative_id="ini_1")[0]
    assert sub.participants == ["qa"]


async def test_host_refusal_is_reported_as_an_event(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory, enabled=False)
    events: list[dict] = []
    host = StageSessionHost(
        initiative_id="ini_1",
        stage="build",
        role="eng",
        participants=["qa"],
        reply=_reply(),
        service=svc,
        on_event=events.append,
    )
    assert await host.open_subroom(goal="对齐验收") == ""
    assert events and events[0]["data"]["subroom"] == "refused"


async def test_host_escalate_is_optional(tmp_path, rooms, memory) -> None:
    svc = _service(tmp_path, rooms, memory)
    host = StageSessionHost(
        initiative_id="ini_1",
        stage="build",
        role="eng",
        participants=["qa"],
        reply=_reply(),
        service=svc,
    )
    assert await host.escalate("conflict") == ""

    async def _escalate(question: str) -> str:
        return f"manager says: {question}"

    host2 = StageSessionHost(
        initiative_id="ini_1",
        stage="build",
        role="eng",
        participants=["qa"],
        reply=_reply(),
        service=svc,
        escalate_fn=_escalate,
    )
    assert await host2.escalate("conflict") == "manager says: conflict"


# --- live event stream ----------------------------------------------------


def _live_service(tmp_path: Path, rooms: RoomStore, memory: MemoryStore, sink) -> SubRoomService:
    return SubRoomService(
        store=SubRoomStore(tmp_path / "subrooms.json"),
        rooms=rooms,
        memory=memory,
        enabled=True,
        timeout_seconds=5.0,
        on_event=sink,
    )


async def test_service_streams_state_and_turns_to_the_event_sink(
    tmp_path, rooms, memory
) -> None:
    """The mission page follows a discussion live, so both states and turns fan out."""
    events: list[dict] = []
    svc = _live_service(tmp_path, rooms, memory, events.append)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        participants=["qa"],
        reply=_reply(),
    )
    assert sub is not None
    states = [e for e in events if e["type"] == "subroom"]
    assert [e["subroom"]["status"] for e in states] == ["open", "closed"]
    assert all(e["initiative_id"] == "ini_1" for e in events)
    turns = [e for e in events if e["type"] == "subroom_message"]
    assert turns, "turns must stream while the discussion is still running"
    assert turns[0]["subroom_id"] == sub.id
    assert turns[0]["turn"]["role"] == "qa"
    assert turns[0]["message"]["msg_type"] == "subroom"


async def test_broken_event_sink_never_breaks_the_subroom(tmp_path, rooms, memory) -> None:
    def _boom(_payload: dict) -> None:
        raise RuntimeError("subscriber gone")

    svc = _live_service(tmp_path, rooms, memory, _boom)
    sub = await svc.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐验收",
        participants=["qa"],
        reply=_reply(),
    )
    assert sub is not None
    assert sub.status == "closed"
    assert sub.conclusion
