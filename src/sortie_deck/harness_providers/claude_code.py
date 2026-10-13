"""Claude Code harness provider — via ``claude-agent-sdk`` (cloud in-process).

The SDK drives Claude Code's agent loop (tools, MCP, skills, permissions) from
inside the Sortie Deck process; nothing is invoked on a user's local machine.
Session continuity uses the SDK ``resume`` option with the native session id.
"""

from __future__ import annotations

import importlib.util
from typing import Any

from sortie_deck.harness_providers.base import HarnessSession
from sortie_deck.models import utc_now


def _sdk_available() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


class ClaudeCodeProvider:
    name = "claude_code"

    def probe(self) -> Any:
        from sortie_deck.harness_providers.base import ProbeStatus

        if not _sdk_available():
            return ProbeStatus(
                status="missing",
                detail="claude-agent-sdk not installed — pip install 'sortie-deck[harness-claude]' "
                "(or `pip install claude-agent-sdk`)",
            )
        import os

        if not (os.environ.get("ANTHROPIC_API_KEY") or _has_claude_cli_creds()):
            return ProbeStatus(
                status="auth_required",
                detail="ANTHROPIC_API_KEY not set and no local Claude credentials found",
            )
        return ProbeStatus(status="ok", detail="claude-agent-sdk ready")

    async def start_session(
        self, *, initiative_id: str, stage: str, meta: dict[str, Any] | None = None
    ) -> HarnessSession:
        return HarnessSession(
            id=f"hs_cc_{utc_now().strftime('%Y%m%d%H%M%S%f')}",
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

        if not _sdk_available():
            return HarnessReply(
                status="fail",
                text="claude-agent-sdk is not installed in this environment",
                session_id=session.id,
                meta={"sdk_missing": True},
            )
        try:
            return await self._send_via_sdk(session, prompt, cwd=cwd)
        except Exception as exc:  # noqa: BLE001 — degrade to a fail reply, never raise
            return HarnessReply(
                status="fail",
                text=f"claude-agent-sdk run failed: {exc}",
                session_id=session.id,
                meta={"error": str(exc)[:300]},
            )

    async def _send_via_sdk(self, session: HarnessSession, prompt: str, *, cwd: str | None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore[import-untyped]
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, SystemMessage  # type: ignore[import-untyped]

        from sortie_deck.harness_providers.base import HarnessReply
        from sortie_deck.settings import settings

        options = ClaudeAgentOptions(
            cwd=cwd or None,
            permission_mode=settings.coding_permission_mode,
            resume=session.native_id or None,
        )
        parts: list[str] = []
        native_id = session.native_id
        is_error = False
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in getattr(message, "content", []) or []:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        parts.append(text)
            elif isinstance(message, ResultMessage):
                result_text = getattr(message, "result", "")
                if isinstance(result_text, str) and result_text.strip():
                    parts.append(result_text)
                native_id = getattr(message, "session_id", native_id) or native_id
                is_error = bool(getattr(message, "is_error", False))
            elif isinstance(message, SystemMessage):
                continue
        text = "\n".join(p for p in parts if p.strip()).strip()
        return HarnessReply(
            status="fail" if (is_error and not text) else "ok",
            text=text,
            session_id=session.id,
            native_id=native_id,
            meta={"provider": self.name},
        )

    async def close(self, session: HarnessSession) -> None:
        return None


def _has_claude_cli_creds() -> bool:
    """Heuristic: the SDK bundles/uses Claude Code credentials when present."""
    from pathlib import Path

    home = Path.home()
    return (home / ".claude" / ".credentials.json").exists() or (home / ".claude.json").exists()
