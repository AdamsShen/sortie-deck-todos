"""OpenCode harness provider — talks HTTP to a deployed ``opencode serve``.

OpenCode is client-server: the agent kernel lives on its local server (Hono
HTTP). The provider consumes that REST surface via ``httpx`` — the server is
deployed alongside Sortie Deck in the cloud (``TDT_OPENCODE_BASE_URL``); no
local CLI is spawned.
"""

from __future__ import annotations

from typing import Any

from sortie_deck.harness_providers.base import HarnessSession
from sortie_deck.models import utc_now

_PROMPT_PART = {"type": "text"}


class OpenCodeProvider:
    name = "opencode"

    def probe(self) -> Any:
        from sortie_deck.harness_providers.base import ProbeStatus
        from sortie_deck.settings import settings

        if not settings.opencode_base_url:
            return ProbeStatus(
                status="missing",
                detail="TDT_OPENCODE_BASE_URL not set — deploy `opencode serve` and point the app at it",
            )
        try:
            import httpx

            resp = httpx.get(
                f"{settings.opencode_base_url.rstrip('/')}/project",
                headers=self._headers(),
                timeout=settings.opencode_http_timeout,
            )
            if resp.status_code < 500:
                return ProbeStatus(
                    status="ok", detail=f"opencode server reachable at {settings.opencode_base_url}"
                )
            return ProbeStatus(
                status="error", detail=f"opencode server HTTP {resp.status_code}"
            )
        except Exception as exc:  # noqa: BLE001 — probing degrades, never raises
            return ProbeStatus(status="error", detail=f"opencode server unreachable: {exc}")

    def _headers(self) -> dict[str, str]:
        from sortie_deck.settings import settings

        headers = {"Content-Type": "application/json"}
        if settings.opencode_api_key:
            headers["Authorization"] = f"Bearer {settings.opencode_api_key}"
        return headers

    async def start_session(
        self, *, initiative_id: str, stage: str, meta: dict[str, Any] | None = None
    ) -> HarnessSession:
        return HarnessSession(
            id=f"hs_oc_{utc_now().strftime('%Y%m%d%H%M%S%f')}",
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
        extra_dirs: list[str] | None = None,
    ) -> Any:
        from sortie_deck.harness_providers.base import HarnessReply

        # opencode 的会话绑定单个 directory（HTTP API 无额外目录参数），
        # extra_dirs 在这里不可兑现，交由阶段收尾归档兜底。
        _ = extra_dirs

        try:
            return await self._send_http(session, prompt)
        except Exception as exc:  # noqa: BLE001 — degrade to a fail reply
            return HarnessReply(
                status="fail",
                text=f"opencode request failed: {exc}",
                session_id=session.id,
                meta={"error": str(exc)[:300]},
            )

    async def _send_http(self, session: HarnessSession, prompt: str) -> Any:
        import httpx

        from sortie_deck.harness_providers.base import HarnessReply
        from sortie_deck.settings import settings

        base = (settings.opencode_base_url or "").rstrip("/")
        if not base:
            return HarnessReply(
                status="fail",
                text="TDT_OPENCODE_BASE_URL is not configured",
                session_id=session.id,
            )
        session_id = session.native_id
        async with httpx.AsyncClient(
            base_url=base, headers=self._headers(), timeout=settings.opencode_timeout
        ) as client:
            if not session_id:
                created = await client.post("/session", json={"title": session.meta.get("stage", "")})
                created.raise_for_status()
                body = created.json()
                session_id = str(body.get("id") or body.get("sessionID") or "")
            resp = await client.post(
                f"/session/{session_id}/message",
                json={"parts": [{**_PROMPT_PART, "text": prompt}]},
            )
            resp.raise_for_status()
            payload = resp.json()
        text = _extract_text(payload)
        return HarnessReply(
            status="ok" if text.strip() else "fail",
            text=text,
            session_id=session.id,
            native_id=session_id,
            meta={"provider": self.name},
        )

    async def close(self, session: HarnessSession) -> None:
        return None


def _extract_text(payload: Any) -> str:
    """Pull the assistant text out of an opencode message response (shape tolerant)."""
    parts = payload.get("parts") if isinstance(payload, dict) else None
    chunks: list[str] = []
    for part in parts or []:
        if isinstance(part, dict) and part.get("type") == "text":
            chunks.append(str(part.get("text") or ""))
    if not chunks and isinstance(payload, dict):
        info = payload.get("info") or {}
        for part in (info.get("parts") if isinstance(info, dict) else None) or []:
            if isinstance(part, dict) and part.get("type") == "text":
                chunks.append(str(part.get("text") or ""))
    return "\n".join(c for c in chunks if c.strip()).strip()
