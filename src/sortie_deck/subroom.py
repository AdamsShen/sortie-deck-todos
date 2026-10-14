"""Temporary sub-rooms ("小群") that an executing agent may pull on demand.

Collaboration model this implements:

* an **executing agent** opens a sub-room mid-step, seeded with its own question
  and the context it already holds;
* the sub-room gets a **brand-new manager object** with its own goal, plan and
  budget — it is not the main-chain manager;
* participants discuss inside the sub-room's own room file; that transcript is
  **never** listed with the big room;
* only the **conclusion** travels back, to the agent that opened it, and is
  persisted as ``mission`` scope / ``kind=episode`` memory tagged
  ``stage:<parent_stage>`` so other parallel stages never see it;
* depth is capped at 1 — a sub-room may not open another sub-room.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from sortie_deck.manager import MissionManager
from sortie_deck.memory import CreateMemoryRequest, MemoryStore
from sortie_deck.models import ActorKind, RoomMessage, new_id, role_key, utc_now
from sortie_deck.rooms import MAIN_ROOM, RoomStore

logger = logging.getLogger(__name__)

ReplyFn = Callable[[str, str], Awaitable[str]]
# Sub-room state/turn fan-out. Synchronous on purpose: turns are appended from
# inside the discussion coroutine, and the sink (orchestrator) schedules the
# async SSE delivery itself.
EventSink = Callable[[dict[str, Any]], None]

SubRoomStatus = Literal["open", "closed", "timeout", "rejected"]

# A sub-room may not nest another sub-room.
MAX_SUBROOM_DEPTH = 1
_TURN_TEXT_CAP = 4000


def estimate_tokens(text: str) -> int:
    """Cheap, provider-agnostic token estimate (~4 chars per token)."""
    return max(1, len(text or "") // 4)


class SubRoomTurn(BaseModel):
    idx: int
    role: str
    text: str
    at: str = Field(default_factory=lambda: utc_now().isoformat())


class SubRoom(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sub_"))
    initiative_id: str
    parent_stage: str
    initiator_role: str
    goal: str
    lead_role: str = ""
    participants: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    max_rounds: int = 2
    max_tokens: int = 20000
    timeout_seconds: float = 300.0
    depth: int = 0
    status: SubRoomStatus = "open"
    conclusion: str = ""
    turns: list[SubRoomTurn] = Field(default_factory=list)
    token_estimate: int = 0
    # The sub-room's own manager identity (independent of the main chain)
    manager_digest: str = ""
    reason: str = ""
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    def room_key(self) -> str:
        return f"subroom:{self.id}"


class SubRoomStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[SubRoom]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [SubRoom.model_validate(x) for x in raw]

    def _write(self, rows: list[SubRoom]) -> None:
        self.path.write_text(
            json.dumps([r.model_dump(mode="json") for r in rows], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(self, *, initiative_id: str | None = None) -> list[SubRoom]:
        rows = self._read()
        if initiative_id:
            rows = [r for r in rows if r.initiative_id == initiative_id]
        return sorted(rows, key=lambda r: r.updated_at, reverse=True)

    def get(self, subroom_id: str) -> SubRoom:
        for row in self._read():
            if row.id == subroom_id:
                return row
        raise KeyError(subroom_id)

    def save(self, sub: SubRoom) -> SubRoom:
        sub.updated_at = utc_now().isoformat()
        with self._lock:
            rows = self._read()
            for i, row in enumerate(rows):
                if row.id == sub.id:
                    rows[i] = sub
                    self._write(rows)
                    return sub
            rows.append(sub)
            self._write(rows)
        return sub


class SubRoomService:
    """Opens sub-rooms, runs the discussion, and feeds the conclusion back."""

    def __init__(
        self,
        *,
        store: SubRoomStore,
        rooms: RoomStore | None = None,
        memory: MemoryStore | None = None,
        enabled: bool = False,
        max_tokens: int = 20000,
        max_rounds: int = 2,
        timeout_seconds: float = 300.0,
        max_participants: int = 4,
        guardrails_load: Callable[[str], Any] | None = None,
        guardrails_save: Callable[[str, Any], None] | None = None,
        on_event: EventSink | None = None,
    ) -> None:
        self.store = store
        self.rooms = rooms
        self.memory = memory
        self.enabled = enabled
        self.max_tokens = max_tokens
        self.max_rounds = max_rounds
        self.timeout_seconds = timeout_seconds
        self.max_participants = max_participants
        # Mission-level guardrails are injected so this module stays I/O-light
        self._guardrails_load = guardrails_load
        self._guardrails_save = guardrails_save
        # Mission-page live view: pushes state/turn events to the initiative stream
        self.on_event = on_event

    def _emit(self, payload: dict[str, Any]) -> None:
        """Fan a sub-room event out to the mission page (best-effort).

        Events let the task page follow a discussion while it is still running;
        the transcript endpoint stays the source of truth after a reconnect.
        A missing or broken subscriber must never break the discussion, so
        failures are logged and swallowed.
        """
        if self.on_event is None:
            return
        try:
            self.on_event(payload)
        except Exception:  # noqa: BLE001 — a dead subscriber is not a discussion failure
            logger.warning(
                "sub-room event delivery failed: type=%s subroom=%s",
                payload.get("type"),
                payload.get("subroom_id") or (payload.get("subroom") or {}).get("id"),
                exc_info=True,
            )

    def _emit_state(self, sub: SubRoom) -> None:
        """Broadcast a sub-room record so the page can show new/closed rooms."""
        self._emit(
            {
                "type": "subroom",
                "initiative_id": sub.initiative_id,
                "subroom_id": sub.id,
                "subroom": sub.model_dump(mode="json"),
            }
        )

    def _rails(self, initiative_id: str) -> Any | None:
        if self._guardrails_load is None:
            return None
        try:
            return self._guardrails_load(initiative_id)
        except Exception:  # noqa: BLE001 — missing guardrails must not block a sub-room
            return None

    def _save_rails(self, initiative_id: str, rails: Any) -> None:
        if rails is None or self._guardrails_save is None:
            return
        try:
            self._guardrails_save(initiative_id, rails)
        except Exception:  # noqa: BLE001
            return

    # --- open ----------------------------------------------------------

    async def open(
        self,
        *,
        initiative_id: str,
        parent_stage: str,
        initiator_role: str,
        goal: str,
        questions: list[str] | None = None,
        participants: list[str] | None = None,
        depth: int = 0,
        reply: ReplyFn | None = None,
    ) -> SubRoom | None:
        """Run one sub-room round-trip. Returns ``None`` when it is not allowed.

        Never raises: a rejected or failed sub-room must not break the calling
        agent's step — the caller simply continues with what it already had.
        """
        initiator = role_key(initiator_role)
        roster = [role_key(p) for p in (participants or []) if role_key(p) and role_key(p) != initiator]
        roster = roster[: self.max_participants]
        if not self.enabled or reply is None or not roster:
            return None
        if depth >= MAX_SUBROOM_DEPTH:
            self._record_rejection(
                initiative_id=initiative_id,
                parent_stage=parent_stage,
                initiator_role=initiator,
                goal=goal,
                depth=depth,
                reason=f"depth {depth} exceeds the sub-room nesting cap ({MAX_SUBROOM_DEPTH})",
            )
            return None

        # Mission budget: refuse new sub-rooms once the allowance is spent
        rails = self._rails(initiative_id)
        if rails is not None:
            verdict = rails.check()
            if not verdict.allowed:
                self._record_rejection(
                    initiative_id=initiative_id,
                    parent_stage=parent_stage,
                    initiator_role=initiator,
                    goal=goal,
                    depth=depth,
                    reason=f"mission guardrail: {verdict.reason}",
                )
                return None

        # Each sub-room owns a fresh manager instance (its own goal + plan).
        manager = MissionManager(
            [
                {"name": "subroom_discuss", "role": roster[0]},
                {"name": "subroom_conclude", "role": roster[0]},
                {"name": "done", "role": roster[0]},
            ],
            use_llm=False,
        )
        sub = SubRoom(
            initiative_id=initiative_id,
            parent_stage=parent_stage,
            initiator_role=initiator,
            goal=(goal or "").strip()[:2000],
            lead_role=roster[0],
            participants=roster,
            questions=[q.strip() for q in (questions or []) if q and q.strip()][:4],
            max_rounds=int(self.max_rounds),
            max_tokens=int(self.max_tokens),
            timeout_seconds=float(self.timeout_seconds),
            depth=depth,
            manager_digest=manager.digest,
        )
        self.store.save(sub)
        self._emit_state(sub)
        try:
            await asyncio.wait_for(
                self._discuss(sub, reply=reply),
                timeout=float(self.timeout_seconds),
            )
            sub.status = "closed"
        except TimeoutError:
            sub.status = "timeout"
            sub.reason = f"sub-room exceeded {self.timeout_seconds:.0f}s budget"
        except Exception as exc:  # noqa: BLE001 — a broken discussion must not break the step
            sub.status = "timeout"
            sub.reason = f"sub-room failed: {exc}"
        self.store.save(sub)
        self._emit_state(sub)
        if rails is not None:
            rails.consume(tokens=sub.token_estimate, subrooms=1)
            self._save_rails(initiative_id, rails)
        if sub.conclusion:
            self._write_conclusion_memory(sub)
        return sub

    def _record_rejection(
        self,
        *,
        initiative_id: str,
        parent_stage: str,
        initiator_role: str,
        goal: str,
        depth: int,
        reason: str,
    ) -> None:
        self._emit_state(
            self.store.save(
                SubRoom(
                    initiative_id=initiative_id,
                    parent_stage=parent_stage,
                    initiator_role=initiator_role,
                    goal=(goal or "")[:2000],
                    depth=depth,
                    status="rejected",
                    reason=reason,
                )
            )
        )

    # --- discussion ----------------------------------------------------

    async def _discuss(self, sub: SubRoom, *, reply: ReplyFn) -> None:
        context = sub.goal
        if sub.questions:
            context = f"{sub.goal}\n\nOpen questions:\n" + "\n".join(
                f"- {q}" for q in sub.questions
            )
        answers: list[str] = []
        for _round in range(max(1, sub.max_rounds)):
            if sub.token_estimate >= sub.max_tokens:
                sub.reason = "token budget exhausted"
                break
            for participant in sub.participants:
                if sub.token_estimate >= sub.max_tokens:
                    break
                prompt = (
                    f"Sub-room goal: {sub.goal}\n"
                    f"You are `{participant}`; the requester is `{sub.initiator_role}` "
                    f"(stage `{sub.parent_stage}`).\n"
                    f"{context}\n\n"
                    "Answer in a few concrete lines. Do not restate the question."
                )
                try:
                    text = str(await reply(participant, prompt) or "").strip()
                except Exception as exc:  # noqa: BLE001 — one silent teammate is fine
                    text = f"(no reply: {exc})"
                if not text:
                    continue
                self._add_turn(sub, participant, text)
                answers.append(f"[{participant}] {text[:_TURN_TEXT_CAP]}")
        if answers:
            sub.conclusion = await self._conclude(sub, answers, reply=reply)
        elif not sub.reason:
            sub.reason = "no participant produced an answer"

    async def _conclude(self, sub: SubRoom, answers: list[str], *, reply: ReplyFn) -> str:
        joined = "\n".join(answers)[:8000]
        prompt = (
            f"Sub-room goal: {sub.goal}\n"
            f"You are `{sub.lead_role}` and you own the conclusion.\n"
            f"Discussion:\n{joined}\n\n"
            "Write the single conclusion the requester should act on: 3-6 lines, "
            "decisions only, no transcript."
        )
        try:
            text = str(await reply(sub.lead_role, prompt) or "").strip()
        except Exception:  # noqa: BLE001 — fall back to the raw answers
            text = ""
        if text:
            self._add_turn(sub, sub.lead_role, text)
            return text[:_TURN_TEXT_CAP]
        return joined[:2000]

    def _add_turn(self, sub: SubRoom, role: str, text: str) -> None:
        turn = SubRoomTurn(idx=len(sub.turns), role=role, text=text[: _TURN_TEXT_CAP])
        sub.turns.append(turn)
        sub.token_estimate += estimate_tokens(turn.text)
        stored: RoomMessage | None = None
        if self.rooms is not None:
            stored = self.rooms.append(
                RoomMessage(
                    initiative_id=sub.initiative_id,
                    actor_kind=ActorKind.AGENT,
                    actor_id=role,
                    actor_name=role,
                    role=role,
                    text=turn.text,
                    msg_type="subroom",
                    room_key=sub.room_key(),
                    meta={
                        "subroom_id": sub.id,
                        "parent_stage": sub.parent_stage,
                        "initiator_role": sub.initiator_role,
                        "index": turn.idx,
                    },
                )
            )
        # Streamed turn-by-turn so the task page can follow the discussion live;
        # ``message`` is absent when no room store is wired (pure in-memory runs).
        self._emit(
            {
                "type": "subroom_message",
                "initiative_id": sub.initiative_id,
                "subroom_id": sub.id,
                "parent_stage": sub.parent_stage,
                "turn": turn.model_dump(mode="json"),
                "message": stored.model_dump(mode="json") if stored is not None else None,
            }
        )

    # --- conclusion ----------------------------------------------------

    def _write_conclusion_memory(self, sub: SubRoom) -> None:
        """Only the conclusion enters the big chain — as stage-scoped memory."""
        if self.memory is None:
            return
        self.memory.create(
            CreateMemoryRequest(
                scope="mission",
                scope_id=sub.initiative_id,
                kind="episode",
                content=f"[sub-room · {sub.parent_stage}] {sub.conclusion}",
                tags=[f"stage:{sub.parent_stage}", f"subroom:{sub.id}", "subroom-conclusion"],
                source="subroom",
            ),
            author_id="subroom",
            author_name=f"SubRoom {sub.id}",
        )

    # --- queries -------------------------------------------------------

    def transcript(self, subroom_id: str) -> list[RoomMessage]:
        if self.rooms is None:
            return []
        sub = self.store.get(subroom_id)
        key = sub.room_key()
        if key == MAIN_ROOM:
            return []
        return self.rooms.list(sub.initiative_id, room_key=key)

    def main_room(self, initiative_id: str) -> list[RoomMessage]:
        if self.rooms is None:
            return []
        return self.rooms.list(initiative_id, room_key=MAIN_ROOM)


class StageSessionHost:
    """``SessionHost`` bound to one stage execution.

    Handed to session-capable executors so an agent can pull its teammates into a
    sub-room mid-step, wait for the conclusion, and then continue its own work.
    """

    def __init__(
        self,
        *,
        initiative_id: str,
        stage: str,
        role: str,
        participants: list[str],
        reply: ReplyFn,
        service: SubRoomService,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        escalate_fn: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self.initiative_id = initiative_id
        self.stage = stage
        self.role = role
        self.participants = [role_key(p) for p in participants if role_key(p)]
        self.reply = reply
        self.service = service
        self.on_event = on_event
        self._escalate_fn = escalate_fn
        self.subrooms: list[str] = []

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(payload)

    async def open_subroom(
        self,
        *,
        goal: str,
        questions: list[str] | None = None,
        participants: list[str] | None = None,
    ) -> str:
        """Open a sub-room and return its conclusion (empty string when refused)."""
        roster = [role_key(p) for p in (participants or self.participants) if role_key(p)]
        roster = [p for p in roster if p and p != self.role]
        sub = await self.service.open(
            initiative_id=self.initiative_id,
            parent_stage=self.stage,
            initiator_role=self.role,
            goal=goal,
            questions=questions,
            participants=roster,
            depth=0,
            reply=self.reply,
        )
        if sub is None:
            self._emit(
                {
                    "kind": "subroom",
                    "message": "sub-room request refused",
                    "data": {"subroom": "refused", "goal": (goal or "")[:200]},
                }
            )
            return ""
        self.subrooms.append(sub.id)
        self._emit(
            {
                "kind": "subroom",
                "message": f"sub-room `{sub.id}` {sub.status}",
                "data": {
                    "subroom": "closed",
                    "subroom_id": sub.id,
                    "status": sub.status,
                    "goal": sub.goal[:200],
                    "participants": sub.participants,
                    "conclusion": sub.conclusion[:400],
                    "turns": len(sub.turns),
                    "reason": sub.reason,
                },
            }
        )
        return sub.conclusion

    async def escalate(self, question: str) -> str:
        """Hand an undecidable question up (wired to the human decision centre)."""
        if self._escalate_fn is None:
            return ""
        return await self._escalate_fn(question)


_SERVICE: SubRoomService | None = None
_SERVICE_LOCK = threading.Lock()


def build_subroom_service(
    *,
    rooms: RoomStore | None,
    memory: MemoryStore | None,
    path: Path,
    enabled: bool,
    max_tokens: int = 20000,
    max_rounds: int = 2,
    timeout_seconds: float = 300.0,
    guardrails_load: Callable[[str], Any] | None = None,
    guardrails_save: Callable[[str, Any], None] | None = None,
    on_event: EventSink | None = None,
) -> SubRoomService:
    return SubRoomService(
        store=SubRoomStore(path),
        rooms=rooms,
        memory=memory,
        enabled=enabled,
        max_tokens=max_tokens,
        max_rounds=max_rounds,
        timeout_seconds=timeout_seconds,
        guardrails_load=guardrails_load,
        guardrails_save=guardrails_save,
        on_event=on_event,
    )


def get_subroom_service() -> SubRoomService | None:
    return _SERVICE


def set_subroom_service(service: SubRoomService | None) -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service
