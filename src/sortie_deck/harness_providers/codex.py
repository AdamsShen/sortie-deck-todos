"""Codex harness provider — OpenAI Codex agent via its headless JSON protocol.

Runs the ``codex`` binary deployed *inside the Sortie Deck cloud environment*
(``codex exec --json``), which is Codex's supported automation entry point.
This is not a user-machine dependency: the cloud image ships/binds the binary
and its credentials. Session continuity uses ``codex exec resume <id>``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any

from sortie_deck.harness_providers.base import HarnessSession
from sortie_deck.models import utc_now


def _binary() -> str | None:
    return shutil.which("codex")


class CodexProvider:
    name = "codex"

    def probe(self) -> Any:
        from sortie_deck.harness_providers.base import ProbeStatus

        binary = _binary()
        if not binary:
            return ProbeStatus(
                status="missing",
                detail="codex binary not found in the cloud environment — install Codex CLI in the image",
            )
        if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")):
            return ProbeStatus(
                status="auth_required",
                detail="OPENAI_API_KEY / CODEX_API_KEY not set for the codex runtime",
            )
        return ProbeStatus(status="ok", detail=binary)

    async def start_session(
        self, *, initiative_id: str, stage: str, meta: dict[str, Any] | None = None
    ) -> HarnessSession:
        return HarnessSession(
            id=f"hs_cx_{utc_now().strftime('%Y%m%d%H%M%S%f')}",
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

        binary = _binary()
        if not binary:
            return HarnessReply(
                status="fail",
                text="codex binary not available in this environment",
                session_id=session.id,
                meta={"missing": True},
            )
        args = [binary, "exec", "--json", "--skip-git-repo-check"]
        if session.native_id:
            args = [binary, "exec", "resume", session.native_id, "--json", "--skip-git-repo-check"]
        args.append(prompt)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd or None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CI": "1"},
            )
            from sortie_deck.settings import settings

            timeout = float(getattr(settings, "coding_timeout_seconds", 900) or 900)
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return HarnessReply(
                    status="fail",
                    text=f"codex timed out after {timeout:.0f}s",
                    session_id=session.id,
                    meta={"timeout": True},
                )
        except Exception as exc:  # noqa: BLE001 — degrade to a fail reply
            return HarnessReply(
                status="fail",
                text=f"codex exec failed: {exc}",
                session_id=session.id,
                meta={"error": str(exc)[:300]},
            )
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        text, native_id, errored = _parse_jsonl(stdout)
        status = "ok" if (proc.returncode == 0 and text.strip() and not errored) else "fail"
        if not text.strip() and stderr.strip():
            text = stderr[:4000]
        return HarnessReply(
            status=status,
            text=text,
            session_id=session.id,
            native_id=native_id or session.native_id,
            meta={"provider": self.name, "exit": proc.returncode or 0},
        )

    async def close(self, session: HarnessSession) -> None:
        return None


def _parse_jsonl(stdout: str) -> tuple[str, str, bool]:
    """Extract the last agent message + session id from ``codex exec --json`` output."""
    text_chunks: list[str] = []
    native_id = ""
    errored = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        native_id = str(event.get("session_id") or event.get("thread_id") or native_id) or native_id
        if event.get("type") in {"error", "stream_error"}:
            errored = True
        msg = event.get("msg") or event
        if str(msg.get("type") or "") == "agent_message":
            text_chunks.append(str(msg.get("message") or ""))
        elif isinstance(msg.get("message"), str) and msg.get("role") in {"assistant", None}:
            text_chunks.append(str(msg["message"]))
    return "\n".join(c for c in text_chunks if c.strip()).strip(), native_id, errored
