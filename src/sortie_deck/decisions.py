"""Human decision centre — the last resort of the escalation ladder.

Escalation ladder: the agent solves it → the manager decides → **a human
decides**. This module owns that final step:

* ``decision_id`` makes every answer idempotent (the same reply arriving twice
  from IM and the UI must not resume a run twice);
* answers are authorised against the bound decider (an unrelated IM member
  replying is just chat);
* an SLA deadline expires a decision and applies a *documented* fallback
  instead of hanging the mission forever;
* nothing leaves the process un-redacted (see :mod:`sortie_deck.notify`).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from sortie_deck.models import new_id, utc_now
from sortie_deck.notify import Notifier, redact

DecisionStatus = Literal["pending", "answered", "expired", "cancelled"]
FallbackAction = Literal["pause", "continue", "abort"]

# Pausing is the safe default: an unanswered question must never silently
# approve itself.
DEFAULT_SLA_SECONDS = 1800.0
DEFAULT_FALLBACK: FallbackAction = "pause"


def _now() -> datetime:
    return datetime.now(UTC)


class HumanDecision(BaseModel):
    id: str = Field(default_factory=lambda: new_id("dcs_"))
    decision_id: str = Field(default_factory=lambda: new_id("dc_"))
    initiative_id: str
    stage: str = ""
    question: str
    context: str = ""
    options: list[str] = Field(default_factory=list)
    # Bound decider. Empty → any authenticated operator may answer.
    assignee: str = ""
    channel: str = ""
    status: DecisionStatus = "pending"
    answer: str = ""
    answered_by: str = ""
    answered_at: str = ""
    sla_seconds: float = DEFAULT_SLA_SECONDS
    deadline_at: str = ""
    fallback_action: FallbackAction = DEFAULT_FALLBACK
    fallback_reason: str = ""
    # De-duplication key so one stage cannot spam the same question
    dedupe_key: str = ""
    notifications: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    def is_overdue(self, now: datetime | None = None) -> bool:
        if not self.deadline_at or self.status != "pending":
            return False
        try:
            deadline = datetime.fromisoformat(self.deadline_at)
        except ValueError:
            return False
        return (now or _now()) >= deadline


class DecisionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[HumanDecision]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [HumanDecision.model_validate(x) for x in raw]

    def _write(self, rows: list[HumanDecision]) -> None:
        self.path.write_text(
            json.dumps([r.model_dump(mode="json") for r in rows], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(
        self, *, initiative_id: str | None = None, status: str | None = None
    ) -> list[HumanDecision]:
        rows = self._read()
        if initiative_id:
            rows = [r for r in rows if r.initiative_id == initiative_id]
        if status:
            rows = [r for r in rows if r.status == status]
        return sorted(rows, key=lambda r: r.updated_at, reverse=True)

    def get(self, decision_id: str) -> HumanDecision:
        for row in self._read():
            if row.decision_id == decision_id or row.id == decision_id:
                return row
        raise KeyError(decision_id)

    def find_pending(self, *, initiative_id: str, stage: str, dedupe_key: str) -> HumanDecision | None:
        for row in self._read():
            if (
                row.status == "pending"
                and row.initiative_id == initiative_id
                and row.stage == stage
                and row.dedupe_key == dedupe_key
            ):
                return row
        return None

    def save(self, decision: HumanDecision) -> HumanDecision:
        decision.updated_at = utc_now().isoformat()
        with self._lock:
            rows = self._read()
            for i, row in enumerate(rows):
                if row.id == decision.id:
                    rows[i] = decision
                    self._write(rows)
                    return decision
            rows.append(decision)
            self._write(rows)
        return decision


class AnswerOutcome(BaseModel):
    decision: HumanDecision
    accepted: bool = False
    duplicate: bool = False
    reason: str = ""


class DecisionService:
    """Raise / answer / expire human decisions."""

    def __init__(
        self,
        *,
        store: DecisionStore,
        notifier: Notifier | None = None,
        default_sla_seconds: float = DEFAULT_SLA_SECONDS,
        default_fallback: FallbackAction = DEFAULT_FALLBACK,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.default_sla_seconds = float(default_sla_seconds)
        self.default_fallback = default_fallback
        self.enabled = bool(enabled)

    # --- raise ---------------------------------------------------------

    async def raise_decision(
        self,
        *,
        initiative_id: str,
        stage: str,
        question: str,
        context: str = "",
        options: list[str] | None = None,
        assignee: str = "",
        sla_seconds: float | None = None,
        fallback_action: FallbackAction | None = None,
        channel: str = "",
        dedupe_key: str = "",
    ) -> HumanDecision:
        """Create (or reuse) the pending decision for this stage and notify."""
        key = dedupe_key or f"{initiative_id}:{stage}:{question[:80]}"
        existing = self.store.find_pending(
            initiative_id=initiative_id, stage=stage, dedupe_key=key
        )
        if existing is not None:
            return existing

        sla = float(sla_seconds if sla_seconds is not None else self.default_sla_seconds)
        decision = HumanDecision(
            initiative_id=initiative_id,
            stage=stage,
            question=question,
            context=redact(context, limit=1200),
            options=list(options or []),
            assignee=assignee,
            channel=channel,
            sla_seconds=sla,
            deadline_at=(utc_now() + timedelta(seconds=sla)).isoformat(),
            fallback_action=fallback_action or self.default_fallback,
            dedupe_key=key,
        )
        self.store.save(decision)
        if self.notifier is not None and self.enabled:
            results = await self.notifier.send(
                title=f"[需要决策] {initiative_id} · {stage or 'mission'}",
                body=self._body(decision),
                meta={"decision_id": decision.decision_id, "initiative_id": initiative_id},
            )
            decision.notifications = [r.model_dump(mode="json") for r in results]
            self.store.save(decision)
        return decision

    @staticmethod
    def _body(decision: HumanDecision) -> str:
        lines = [decision.question]
        if decision.context:
            lines.append(f"背景：{decision.context}")
        if decision.options:
            lines.append("可选：" + " / ".join(decision.options))
        lines.append(
            f"请在 {decision.sla_seconds:.0f}s 内回复（decision_id={decision.decision_id}）；"
            f"超时将执行 fallback=`{decision.fallback_action}`。"
        )
        return "\n".join(lines)

    # --- answer --------------------------------------------------------

    def is_authorized(self, decision: HumanDecision, *, by: str, is_admin: bool = False) -> bool:
        """Only the bound decider (or an admin) may answer."""
        if is_admin:
            return True
        if not decision.assignee:
            return bool((by or "").strip())
        return (by or "").strip() == decision.assignee.strip()

    def answer(
        self,
        decision_id: str,
        *,
        answer: str,
        by: str,
        channel: str = "",
        is_admin: bool = False,
    ) -> AnswerOutcome:
        """Record a human answer. Idempotent: a second reply is a no-op."""
        decision = self.store.get(decision_id)
        if not self.is_authorized(decision, by=by, is_admin=is_admin):
            return AnswerOutcome(
                decision=decision,
                accepted=False,
                reason=f"`{by}` is not the bound decider for this decision",
            )
        if decision.status == "answered":
            return AnswerOutcome(decision=decision, accepted=False, duplicate=True, reason="already answered")
        if decision.status != "pending":
            return AnswerOutcome(
                decision=decision, accepted=False, reason=f"decision is {decision.status}"
            )
        decision.answer = redact(answer, limit=2000)
        decision.answered_by = by
        decision.answered_at = utc_now().isoformat()
        decision.channel = channel or decision.channel
        decision.status = "answered"
        self.store.save(decision)
        return AnswerOutcome(decision=decision, accepted=True)

    # --- SLA -----------------------------------------------------------

    def expire_due(self, *, now: datetime | None = None) -> list[HumanDecision]:
        """Expire overdue decisions and stamp the fallback that will be applied."""
        expired: list[HumanDecision] = []
        moment = now or _now()
        for decision in self.store.list(status="pending"):
            if not decision.is_overdue(moment):
                continue
            decision.status = "expired"
            decision.fallback_reason = (
                f"no answer within {decision.sla_seconds:.0f}s — applying `{decision.fallback_action}`"
            )
            self.store.save(decision)
            expired.append(decision)
        return expired

    def next_fallback(self, initiative_id: str) -> str | None:
        """Fallback action to apply after expiring this initiative's decisions."""
        for decision in self.store.list(initiative_id=initiative_id, status="expired"):
            return decision.fallback_action
        return None

    # --- queries -------------------------------------------------------

    def list(
        self, *, initiative_id: str | None = None, status: str | None = None
    ) -> list[HumanDecision]:
        return self.store.list(initiative_id=initiative_id, status=status)

    def get(self, decision_id: str) -> HumanDecision:
        return self.store.get(decision_id)


_SERVICE: DecisionService | None = None
_SERVICE_LOCK = threading.Lock()


def get_decision_service() -> DecisionService | None:
    return _SERVICE


def set_decision_service(service: DecisionService | None) -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service
