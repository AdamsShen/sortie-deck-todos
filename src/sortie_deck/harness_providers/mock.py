"""Mock harness provider — always-available fallback for tests and degraded runs."""

from __future__ import annotations

from typing import Any

from sortie_deck.harness_providers.base import HarnessSession
from sortie_deck.models import utc_now


class MockHarnessProvider:
    name = "mock"

    def probe(self) -> Any:
        from sortie_deck.harness_providers.base import ProbeStatus

        return ProbeStatus(status="ok", detail="builtin mock harness")

    async def start_session(
        self, *, initiative_id: str, stage: str, meta: dict[str, Any] | None = None
    ) -> HarnessSession:
        return HarnessSession(
            id=f"hs_mock_{utc_now().strftime('%Y%m%d%H%M%S%f')}",
            provider=self.name,
            meta={"initiative_id": initiative_id, "stage": stage, **(meta or {})},
        )

    async def send(
        self,
        session: HarnessSession,
        prompt: str,
        *,
        cwd: str | None = None,
        context_pack: dict[str, Any] | None = None,
    ) -> Any:
        from sortie_deck.harness_providers.base import HarnessReply

        head = prompt.strip().splitlines()[0] if prompt.strip() else "(empty prompt)"
        return HarnessReply(
            status="ok",
            text=(
                f"[mock harness] acknowledged `{session.provider}` session for "
                f"{session.meta.get('initiative_id', '?')} / {session.meta.get('stage', '?')}.\n"
                f"Prompt head: {head[:200]}"
            ),
            session_id=session.id,
            meta={"mock": True},
        )

    async def close(self, session: HarnessSession) -> None:
        return None
