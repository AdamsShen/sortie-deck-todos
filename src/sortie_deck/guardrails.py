"""Mission-level guardrails.

Three things every long-running multi-agent mission needs:

* a **budget** (tokens / wall-clock / sub-rooms / handoffs) so a runaway loop
  stops asking for more instead of burning the account;
* **cycle and deadlock detection** over the handoff and waiting graphs, so an
  A→B→A handoff or two agents waiting on each other surfaces as an explicit
  stop instead of hanging;
* a **causal chain** so "who triggered this, and where did the conclusion go"
  is answerable after the fact.

Everything here is pure bookkeeping — no I/O, no LLM — so it can be consulted
on the hot path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from sortie_deck.models import utc_now

# Generous defaults: they exist to stop runaways, not to ration normal work.
DEFAULT_MAX_TOKENS = 2_000_000
DEFAULT_MAX_SECONDS = 6 * 3600.0
DEFAULT_MAX_SUBROOMS = 20
DEFAULT_MAX_HANDOFFS = 40

GuardrailDimension = Literal["tokens", "wall_clock", "subrooms", "handoffs"]


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


class MissionBudget(BaseModel):
    """Upper bounds for one mission."""

    max_tokens: int = DEFAULT_MAX_TOKENS
    max_seconds: float = DEFAULT_MAX_SECONDS
    max_subrooms: int = DEFAULT_MAX_SUBROOMS
    max_handoffs: int = DEFAULT_MAX_HANDOFFS


class BudgetUsage(BaseModel):
    tokens: int = 0
    subrooms: int = 0
    handoffs: int = 0
    started_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


class BudgetVerdict(BaseModel):
    allowed: bool = True
    dimension: GuardrailDimension | None = None
    reason: str = ""


class TraceEntry(BaseModel):
    trace_id: str
    parent: str = ""
    kind: str = "event"
    stage: str = ""
    detail: str = ""


class Guardrails(BaseModel):
    """Budget + wait-graph + causal chain for one mission (persistable)."""

    budget: MissionBudget = Field(default_factory=MissionBudget)
    usage: BudgetUsage = Field(default_factory=BudgetUsage)
    # waiter -> the node it is blocked on (deadlock detection)
    waiting: dict[str, str] = Field(default_factory=dict)
    trace: list[TraceEntry] = Field(default_factory=list)
    stopped: bool = False
    stop_reason: str = ""

    # --- budget --------------------------------------------------------

    def elapsed_seconds(self, now: datetime | None = None) -> float:
        started = _parse(self.usage.started_at)
        if started is None:
            return 0.0
        return max(0.0, ((now or _now()) - started).total_seconds())

    def check(self, *, now: datetime | None = None) -> BudgetVerdict:
        """Verdict *before* spending more — used to refuse new work."""
        if self.stopped:
            return BudgetVerdict(allowed=False, reason=self.stop_reason or "mission stopped")
        if self.usage.tokens >= self.budget.max_tokens:
            return BudgetVerdict(
                allowed=False,
                dimension="tokens",
                reason=f"token budget exhausted ({self.usage.tokens}/{self.budget.max_tokens})",
            )
        elapsed = self.elapsed_seconds(now)
        if elapsed >= self.budget.max_seconds:
            return BudgetVerdict(
                allowed=False,
                dimension="wall_clock",
                reason=f"wall-clock budget exhausted ({elapsed:.0f}s/{self.budget.max_seconds:.0f}s)",
            )
        if self.usage.subrooms >= self.budget.max_subrooms:
            return BudgetVerdict(
                allowed=False,
                dimension="subrooms",
                reason=f"sub-room budget exhausted ({self.usage.subrooms}/{self.budget.max_subrooms})",
            )
        if self.usage.handoffs >= self.budget.max_handoffs:
            return BudgetVerdict(
                allowed=False,
                dimension="handoffs",
                reason=f"handoff budget exhausted ({self.usage.handoffs}/{self.budget.max_handoffs})",
            )
        return BudgetVerdict(allowed=True)

    def consume(
        self,
        *,
        tokens: int = 0,
        subrooms: int = 0,
        handoffs: int = 0,
        now: datetime | None = None,
    ) -> BudgetVerdict:
        """Record spend, then re-check. Returns the verdict *after* spending."""
        verdict = self.check(now=now)
        if not verdict.allowed:
            return verdict
        self.usage.tokens += max(0, int(tokens))
        self.usage.subrooms += max(0, int(subrooms))
        self.usage.handoffs += max(0, int(handoffs))
        self.usage.updated_at = utc_now().isoformat()
        return self.check(now=now)

    def stop(self, reason: str) -> None:
        self.stopped = True
        self.stop_reason = reason

    # --- waiting graph -------------------------------------------------

    def wait_on(self, waiter: str, waited_on: str) -> None:
        self.waiting[waiter] = waited_on

    def release(self, waiter: str) -> None:
        self.waiting.pop(waiter, None)

    def find_deadlock(self) -> list[str] | None:
        """Return the waiting cycle as a node list, or ``None`` when clear."""
        for start in self.waiting:
            path: list[str] = []
            node = start
            while node in self.waiting:
                if node in path:
                    return path[path.index(node) :] + [node]
                path.append(node)
                node = self.waiting[node]
        return None

    # --- causal chain --------------------------------------------------

    def record(self, *, trace_id: str, kind: str, stage: str = "", parent: str = "", detail: str = "") -> None:
        self.trace.append(
            TraceEntry(trace_id=trace_id, parent=parent, kind=kind, stage=stage, detail=detail[:500])
        )
        # Keep the chain bounded; the head is what matters for root-cause work
        if len(self.trace) > 500:
            del self.trace[: len(self.trace) - 500]

    def children(self, parent: str) -> list[TraceEntry]:
        return [e for e in self.trace if e.parent == parent]


def would_cycle(edges: list[tuple[str, str]], *, node: str, target: str) -> list[str] | None:
    """Cycle introduced by adding ``node → target``, or ``None`` when safe."""
    if node == target:
        return [node, node]
    adjacency: dict[str, list[str]] = {}
    for src, dst in edges:
        adjacency.setdefault(src, []).append(dst)
    stack: list[str] = [target]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == node:
            return [node, target, node]
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency.get(current, []))
    return None


def guardrails_from_meta(meta: dict[str, Any] | None) -> Guardrails:
    """Load guardrails from ``ini.meta`` (creating defaults when absent)."""
    raw = (meta or {}).get("guardrails")
    if isinstance(raw, dict):
        try:
            return Guardrails.model_validate(raw)
        except Exception:  # noqa: BLE001 — a corrupt blob must not break a mission
            return Guardrails()
    return Guardrails()


def guardrails_to_meta(meta: dict[str, Any] | None, guardrails: Guardrails) -> dict[str, Any]:
    return {**(meta or {}), "guardrails": guardrails.model_dump(mode="json")}
