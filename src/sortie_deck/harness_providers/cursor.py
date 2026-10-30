"""Cursor harness provider — ``cursor-agent`` headless mode (cloud container).

The binary runs inside the Sortie Deck cloud environment (never the user's
machine). Auth is via ``CURSOR_API_KEY`` or a prior ``cursor-agent login`` on
the container. Session continuity uses ``--resume <session-id>``.
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
    # `cursor` alone is the IDE shim and would hang on -p; only the agent CLI counts.
    for name in ("cursor-agent", "agent"):
        path = shutil.which(name)
        if path:
            return path
    return None


class CursorProvider:
    name = "cursor"

    def probe(self) -> Any:
        from sortie_deck.harness_providers.base import ProbeStatus

        binary = _binary()
        if not binary:
            return ProbeStatus(
                status="missing",
                detail="cursor-agent not found in the cloud environment — "
                "install with `curl -sS https://cursor.com/install | bash`",
            )
        if not _ready():
            return ProbeStatus(
                status="auth_required",
                detail="run `cursor-agent login` in the container or set CURSOR_API_KEY",
            )
        return ProbeStatus(status="ok", detail=binary)

    async def start_session(
        self, *, initiative_id: str, stage: str, meta: dict[str, Any] | None = None
    ) -> HarnessSession:
        return HarnessSession(
            id=f"hs_cu_{utc_now().strftime('%Y%m%d%H%M%S%f')}",
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

        binary = _binary()
        if not binary:
            return HarnessReply(
                status="fail",
                text="cursor-agent not available in this environment",
                session_id=session.id,
                meta={"missing": True},
            )
        # cursor-agent CLI 只有单个 --workspace（无 --add-dir），额外目录无法授权；
        # 忽略 extra_dirs，阶段收尾会从 cwd 里回收契约文件归档。
        _ = extra_dirs
        args = [binary, "-p", prompt, "--output-format", "json"]
        if session.native_id:
            args += ["--resume", session.native_id]
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
                    text=f"cursor-agent timed out after {timeout:.0f}s",
                    session_id=session.id,
                    meta={"timeout": True},
                )
        except Exception as exc:  # noqa: BLE001 — degrade to a fail reply
            return HarnessReply(
                status="fail",
                text=f"cursor-agent failed: {exc}",
                session_id=session.id,
                meta={"error": str(exc)[:300]},
            )
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        combined = f"{stdout}\n{stderr}"
        text, native_id = _parse_output(stdout)
        if not text.strip():
            text = combined[:4000]
        status = "ok" if (proc.returncode == 0 and native_id) or proc.returncode == 0 else "fail"
        if _looks_like_auth(combined):
            status = "needs_auth"
        elif _looks_like_permission(combined):
            status = "needs_permission"
        return HarnessReply(
            status=status,
            text=text,
            session_id=session.id,
            native_id=native_id or session.native_id,
            meta={"provider": self.name, "exit": proc.returncode or 0},
        )

    async def close(self, session: HarnessSession) -> None:
        return None


def _ready() -> bool:
    if os.environ.get("CURSOR_API_KEY"):
        return True
    from pathlib import Path

    return (Path.home() / ".config" / "cursor-agent" / "auth.json").exists() or (
        Path.home() / ".cursor" / "credentials.json"
    ).exists()


def _looks_like_auth(text: str) -> bool:
    import re

    return bool(
        re.search(
            r"(authentication required|not (?:logged|authenticated)|login required|unauthorized|CURSOR_API_KEY)",
            text or "",
            re.IGNORECASE,
        )
    )


def _looks_like_permission(text: str) -> bool:
    import re

    return bool(
        re.search(
            r"(haven'?t granted|permission(?:s)? blocker|write permission|needs? approval|"
            r"授权|尚未授予|权限被拒绝)",
            text or "",
            re.IGNORECASE,
        )
    )


def _parse_output(stdout: str) -> tuple[str, str]:
    """Extract (text, session_id) from cursor-agent JSON output (single object or JSONL)."""
    texts: list[str] = []
    session_id = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = str(event.get("session_id") or event.get("sessionId") or session_id) or session_id
        if isinstance(event.get("text"), str):
            texts.append(event["text"])
        elif isinstance(event.get("result"), str):
            texts.append(event["result"])
    if not texts:
        try:
            event = json.loads(stdout)
            if isinstance(event, dict):
                session_id = str(event.get("session_id") or event.get("sessionId") or session_id) or session_id
                if isinstance(event.get("text"), str):
                    texts.append(event["text"])
        except json.JSONDecodeError:
            pass
    return "\n".join(t for t in texts if t.strip()).strip(), session_id
