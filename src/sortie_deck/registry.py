from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sortie_deck.models import StageContext, StageResult


@runtime_checkable
class SessionHost(Protocol):
    """Mid-run capabilities handed to a session-capable executor.

    An executor that needs to *pause and resume* — e.g. an agent that pulls the
    other agents into a temporary sub-room and waits for the conclusion before
    continuing its own step — receives this object and calls into it.
    """

    initiative_id: str
    stage: str
    role: str

    async def open_subroom(
        self,
        *,
        goal: str,
        questions: list[str] | None = None,
        participants: list[str] | None = None,
    ) -> str: ...

    async def escalate(self, question: str) -> str: ...


class StageExecutor(Protocol):
    name: str

    async def run(self, ctx: StageContext) -> StageResult: ...


def supports_session(executor: Any) -> bool:
    """True when ``executor`` opts into the session (pause/resume) contract."""
    return callable(getattr(executor, "run_session", None))


async def run_executor(executor: Any, ctx: StageContext, host: SessionHost | None = None) -> StageResult:
    """Run an executor, preferring its session entry point when it has one.

    Executors that only implement ``run()`` (mock / coding / deploy today) keep
    working untouched — ``run_session`` is an opt-in extension.
    """
    session = getattr(executor, "run_session", None)
    if host is not None and callable(session):
        return await session(ctx, host)
    return await executor.run(ctx)


class ExecutorRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, StageExecutor] = {}

    def register(self, executor: StageExecutor) -> None:
        self._executors[executor.name] = executor

    def get(self, name: str) -> StageExecutor:
        if name not in self._executors:
            raise KeyError(f"Unknown executor: {name}")
        return self._executors[name]

    def names(self) -> list[str]:
        return sorted(self._executors)


_REGISTRY: ExecutorRegistry | None = None


def get_registry() -> ExecutorRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        from sortie_deck.plugins.factory import build_default_registry

        _REGISTRY = build_default_registry()
    return _REGISTRY


def reset_registry() -> None:
    global _REGISTRY
    _REGISTRY = None
