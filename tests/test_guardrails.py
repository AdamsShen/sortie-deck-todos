from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sortie_deck.guardrails import (
    Guardrails,
    MissionBudget,
    guardrails_from_meta,
    guardrails_to_meta,
    would_cycle,
)
from sortie_deck.memory import MemoryStore
from sortie_deck.models import utc_now
from sortie_deck.rooms import RoomStore
from sortie_deck.subroom import SubRoomService, SubRoomStore

# --- budget ---------------------------------------------------------------


def test_default_budget_allows_work() -> None:
    assert Guardrails().check().allowed is True


def test_token_budget_exhaustion_blocks_further_work() -> None:
    rails = Guardrails(budget=MissionBudget(max_tokens=10))
    rails.consume(tokens=10)
    verdict = rails.check()
    assert verdict.allowed is False
    assert verdict.dimension == "tokens"
    assert "token budget" in verdict.reason


def test_subroom_budget_exhaustion() -> None:
    rails = Guardrails(budget=MissionBudget(max_subrooms=1))
    rails.consume(subrooms=1)
    verdict = rails.check()
    assert verdict.allowed is False
    assert verdict.dimension == "subrooms"


def test_handoff_budget_exhaustion() -> None:
    rails = Guardrails(budget=MissionBudget(max_handoffs=2))
    rails.consume(handoffs=2)
    verdict = rails.check()
    assert verdict.allowed is False
    assert verdict.dimension == "handoffs"


def test_wall_clock_budget_exhaustion() -> None:
    rails = Guardrails(budget=MissionBudget(max_seconds=1))
    rails.usage.started_at = (utc_now() - timedelta(seconds=5)).isoformat()
    verdict = rails.check()
    assert verdict.allowed is False
    assert verdict.dimension == "wall_clock"


def test_consume_refuses_once_a_limit_is_hit() -> None:
    """``consume`` reports whether *more* work is still allowed."""
    rails = Guardrails(budget=MissionBudget(max_tokens=5))
    rails.consume(tokens=4)
    assert rails.check().allowed is True

    # reaching the ceiling is reported immediately — no more work
    assert rails.consume(tokens=1).allowed is False
    assert rails.usage.tokens == 5

    # a further attempt must not increase spend
    assert rails.consume(tokens=5).allowed is False
    assert rails.usage.tokens == 5


def test_stop_blocks_everything() -> None:
    rails = Guardrails()
    rails.stop("operator aborted")
    verdict = rails.check()
    assert verdict.allowed is False
    assert "operator aborted" in verdict.reason


def test_elapsed_seconds_is_non_negative() -> None:
    rails = Guardrails()
    assert rails.elapsed_seconds() >= 0


# --- wait graph / cycles --------------------------------------------------


def test_deadlock_is_detected_for_a_waiting_cycle() -> None:
    rails = Guardrails()
    rails.wait_on("a", "b")
    rails.wait_on("b", "a")
    cycle = rails.find_deadlock()
    assert cycle is not None
    assert cycle[0] == cycle[-1]


def test_no_deadlock_for_a_linear_wait_chain() -> None:
    rails = Guardrails()
    rails.wait_on("a", "b")
    rails.wait_on("b", "c")
    assert rails.find_deadlock() is None


def test_release_clears_a_wait_edge() -> None:
    rails = Guardrails()
    rails.wait_on("a", "b")
    rails.wait_on("b", "a")
    rails.release("b")
    assert rails.find_deadlock() is None


def test_would_cycle_flags_a_back_edge() -> None:
    edges = [("b", "c"), ("c", "d")]
    assert would_cycle(edges, node="d", target="b") is not None
    assert would_cycle(edges, node="d", target="e") is None


def test_would_cycle_flags_self_handoff() -> None:
    assert would_cycle([], node="a", target="a") == ["a", "a"]


# --- causal chain ---------------------------------------------------------


def test_trace_records_parent_links() -> None:
    rails = Guardrails()
    rails.record(trace_id="t1", kind="stage", stage="build", detail="started")
    rails.record(trace_id="t2", parent="t1", kind="subroom", stage="build", detail="conclusion")
    assert [e.trace_id for e in rails.children("t1")] == ["t2"]
    assert rails.trace[1].stage == "build"


def test_trace_is_bounded() -> None:
    rails = Guardrails()
    for i in range(700):
        rails.record(trace_id=f"t{i}", kind="stage")
    assert len(rails.trace) == 500
    assert rails.trace[-1].trace_id == "t699"


def test_trace_detail_is_truncated() -> None:
    rails = Guardrails()
    rails.record(trace_id="t", kind="stage", detail="x" * 2000)
    assert len(rails.trace[0].detail) == 500


# --- persistence ----------------------------------------------------------


def test_guardrails_round_trip_through_meta() -> None:
    rails = Guardrails(budget=MissionBudget(max_tokens=42))
    rails.consume(tokens=7, handoffs=1)
    meta = guardrails_to_meta({"other": 1}, rails)

    restored = guardrails_from_meta(meta)
    assert restored.budget.max_tokens == 42
    assert restored.usage.tokens == 7
    assert restored.usage.handoffs == 1
    assert meta["other"] == 1


def test_corrupt_guardrails_blob_falls_back_to_defaults() -> None:
    restored = guardrails_from_meta({"guardrails": {"budget": "not-a-budget"}})
    assert restored.budget.max_tokens == MissionBudget().max_tokens


def test_missing_guardrails_meta_yields_defaults() -> None:
    assert guardrails_from_meta(None).usage.tokens == 0


# --- sub-room integration -------------------------------------------------


class _Rails:
    def __init__(self, allowed: bool, reason: str = "budget exhausted") -> None:
        self.allowed = allowed
        self.reason = reason
        self.consumed: list[dict] = []

    def check(self):
        from sortie_deck.guardrails import BudgetVerdict

        return BudgetVerdict(allowed=self.allowed, reason=self.reason if not self.allowed else "")

    def consume(self, **kwargs):
        self.consumed.append(kwargs)
        from sortie_deck.guardrails import BudgetVerdict

        return BudgetVerdict(allowed=True)


def _service(tmp_path: Path, rails: _Rails | None) -> SubRoomService:
    saved: list[_Rails] = []
    return SubRoomService(
        store=SubRoomStore(tmp_path / "subrooms.json"),
        rooms=RoomStore(tmp_path / "rooms"),
        memory=MemoryStore(tmp_path / "memory.json"),
        enabled=True,
        guardrails_load=(lambda _ini: rails) if rails else None,
        guardrails_save=(lambda _ini, r: saved.append(r)) if rails else None,
    )


async def _reply(role: str, prompt: str) -> str:
    return f"{role} ok"


async def test_budget_exhaustion_refuses_a_new_subroom(tmp_path: Path) -> None:
    rails = _Rails(allowed=False)
    service = _service(tmp_path, rails)
    sub = await service.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐",
        participants=["qa"],
        reply=_reply,
    )
    assert sub is None
    rejected = service.store.list(initiative_id="ini_1")
    assert rejected
    assert "mission guardrail" in rejected[0].reason


async def test_subroom_spend_is_recorded(tmp_path: Path) -> None:
    rails = _Rails(allowed=True)
    service = _service(tmp_path, rails)
    sub = await service.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐",
        participants=["qa"],
        reply=_reply,
    )
    assert sub is not None
    assert rails.consumed
    assert rails.consumed[0]["subrooms"] == 1


async def test_no_guardrails_provider_keeps_behaviour_unchanged(tmp_path: Path) -> None:
    service = _service(tmp_path, None)
    sub = await service.open(
        initiative_id="ini_1",
        parent_stage="build",
        initiator_role="eng",
        goal="对齐",
        participants=["qa"],
        reply=_reply,
    )
    assert sub is not None
