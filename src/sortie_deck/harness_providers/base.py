"""Agent harness provider abstraction — the system-wide "embed an agent harness" layer.

A *harness provider* is the SDK / server-side integration for one agent harness
(Claude Code, OpenCode, Codex, Cursor, ...). Providers run inside the Sortie
Deck cloud environment and never shell out to a user's local machine.

Contract:
- ``probe()`` → availability / credentials check (cached with a TTL by the registry)
- ``start_session()`` → a named session (multi-session missions are the norm)
- ``send()`` → one conversation turn; returns a :class:`HarnessReply`
- ``close()`` → release provider-side resources

Stage routing does not import vendors directly: the generic
``HarnessStageExecutor`` resolves a provider by the initiative-locked harness
name and degrades to legacy executors when the provider is unavailable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from sortie_deck.models import utc_now

#: All registered harness names, in display order.
HARNESS_NAMES: tuple[str, ...] = ("claude_code", "opencode", "codex", "cursor", "mock")

HARNESS_LABELS: dict[str, str] = {
    "claude_code": "Claude Code",
    "opencode": "OpenCode",
    "codex": "Codex",
    "cursor": "Cursor",
    "mock": "Mock (fallback)",
}


@dataclass
class ProbeStatus:
    """Availability of one harness provider (``ok`` / ``missing`` / ``auth_required`` / ``error``)."""

    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


class HarnessSession(BaseModel):
    """One provider-side conversation. Sessions are disposable; memory persists."""

    id: str = Field(default_factory=lambda: f"hs_{utc_now().strftime('%Y%m%d%H%M%S%f')}")
    provider: str = ""
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    # provider-native session id (claude session_id / opencode session id / ...)
    native_id: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class HarnessReply(BaseModel):
    """One conversation turn's outcome.

    ``status``: ``ok`` | ``fail`` | ``needs_permission`` | ``needs_auth``
    """

    status: str = "ok"
    text: str = ""
    session_id: str = ""
    native_id: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class HarnessProvider(Protocol):
    name: str

    def probe(self) -> ProbeStatus: ...

    async def start_session(
        self, *, initiative_id: str, stage: str, meta: dict[str, Any] | None = None
    ) -> HarnessSession: ...

    async def send(
        self,
        session: HarnessSession,
        prompt: str,
        *,
        cwd: str | None = None,
        context_pack: dict[str, Any] | None = None,
        extra_dirs: list[str] | None = None,
        on_activity: Any = None,
    ) -> HarnessReply: ...

    async def close(self, session: HarnessSession) -> None: ...


class HarnessRegistry:
    """Name → provider registry with a TTL cache around ``probe()``."""

    def __init__(self, probe_ttl: float = 60.0) -> None:
        self._providers: dict[str, HarnessProvider] = {}
        self._probe_ttl = max(1.0, float(probe_ttl))
        self._probe_cache: dict[str, tuple[float, ProbeStatus]] = {}

    def register(self, provider: HarnessProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> HarnessProvider:
        if name not in self._providers:
            raise KeyError(f"Unknown harness provider: {name}")
        return self._providers[name]

    def has(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> list[str]:
        return list(self._providers)

    def probe(self, name: str, *, refresh: bool = False) -> ProbeStatus:
        provider = self.get(name)
        now = time.monotonic()
        if not refresh:
            cached = self._probe_cache.get(name)
            if cached and now - cached[0] < self._probe_ttl:
                return cached[1]
        try:
            status = provider.probe()
        except Exception as exc:  # noqa: BLE001 — probing must never raise
            status = ProbeStatus(status="error", detail=str(exc)[:300])
        self._probe_cache[name] = (now, status)
        return status

    def probe_all(self, *, refresh: bool = False) -> dict[str, ProbeStatus]:
        return {name: self.probe(name, refresh=refresh) for name in self.names()}

    def invalidate(self, name: str | None = None) -> None:
        if name is None:
            self._probe_cache.clear()
        else:
            self._probe_cache.pop(name, None)


_REGISTRY: HarnessRegistry | None = None


def build_harness_registry() -> HarnessRegistry:
    """Fresh registry with every built-in provider registered."""
    from sortie_deck.harness_providers.claude_code import ClaudeCodeProvider
    from sortie_deck.harness_providers.codex import CodexProvider
    from sortie_deck.harness_providers.cursor import CursorProvider
    from sortie_deck.harness_providers.mock import MockHarnessProvider
    from sortie_deck.harness_providers.opencode import OpenCodeProvider
    from sortie_deck.settings import settings

    registry = HarnessRegistry(probe_ttl=settings.harness_probe_ttl)
    for provider in (
        ClaudeCodeProvider(),
        OpenCodeProvider(),
        CodexProvider(),
        CursorProvider(),
        MockHarnessProvider(),
    ):
        registry.register(provider)
    return registry


def get_harness_registry() -> HarnessRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        from sortie_deck.settings import settings

        _REGISTRY = build_harness_registry()
        # Keep probe TTL aligned with live settings changes.
        _REGISTRY._probe_ttl = max(1.0, float(settings.harness_probe_ttl))
    return _REGISTRY


def reset_harness_registry() -> None:
    global _REGISTRY
    _REGISTRY = None


def is_valid_harness(name: str | None) -> bool:
    return bool(name) and name in HARNESS_NAMES


def parse_stage_turns(raw: str) -> dict[str, int]:
    """解析 `intake=2,eng_backend=0` 形式的阶段回合上限映射，非法项直接丢弃。"""
    mapping: dict[str, int] = {}
    for part in str(raw or "").split(","):
        name, _, value = part.partition("=")
        key = name.strip().lower()
        if not key:
            continue
        try:
            turns = int(value.strip())
        except (TypeError, ValueError):
            continue
        if turns >= 0:
            mapping[key] = turns
    return mapping


def resolve_stage_max_turns(stage: str) -> int:
    """该阶段允许的 agent 回合上限。

    优先级：``stage_max_turns`` 里列出的阶段值 > 全局 ``harness_max_turns``。
    返回 0 表示不限制（交给 provider 传 None）。

    为什么要按阶段限：agent loop 的耗时 ≈ 轮次 × 单轮生成时长，而集结
    （intake）这类纯对齐阶段用不着无限工具循环——实测不封顶时集结要 9 分钟。
    """
    from sortie_deck.settings import settings

    mapping = parse_stage_turns(getattr(settings, "stage_max_turns", ""))
    key = str(stage or "").strip().lower()
    if key in mapping:
        return mapping[key]
    return int(getattr(settings, "harness_max_turns", 0) or 0)
