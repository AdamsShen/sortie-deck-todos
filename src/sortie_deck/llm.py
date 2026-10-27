from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, Literal

CodingExecutorName = Literal["mock", "claude_code", "cursor_cli"]


class LlmClient:
    """OpenAI-compatible chat client with optional Claude Code CLI fallback.

    Enabled when ``TDT_LLM_API_KEY`` / ``OPENAI_API_KEY`` is set, or when
    ``cli_fallback`` is true and ``claude`` is on PATH (local agent).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        cli_fallback: bool | None = None,
    ) -> None:
        cfg_key = ""
        cfg_base = ""
        cfg_model = ""
        cfg_cli: bool | None = None
        try:
            from sortie_deck.settings import settings as _settings

            cfg_key = (_settings.llm_api_key or "").strip()
            cfg_base = (_settings.llm_base_url or "").strip()
            cfg_model = (_settings.llm_model or "").strip()
            cfg_cli = bool(_settings.llm_cli_fallback)
        except Exception:
            pass

        self.api_key = (
            api_key
            or os.environ.get("TDT_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or cfg_key
            or ""
        ).strip()
        self.base_url = (
            base_url
            or os.environ.get("TDT_LLM_BASE_URL")
            or cfg_base
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("TDT_LLM_MODEL") or cfg_model or "gpt-4o-mini"
        self.timeout = timeout
        if cli_fallback is None:
            flag = (os.environ.get("TDT_LLM_CLI_FALLBACK") or "").strip().lower()
            if flag:
                cli_fallback = flag not in {"0", "false", "no", "off"}
            elif cfg_cli is not None:
                cli_fallback = cfg_cli
            else:
                cli_fallback = True
        self.cli_fallback = bool(cli_fallback)

    @property
    def enabled(self) -> bool:
        if self.api_key:
            return True
        return self.cli_fallback and bool(shutil.which("claude"))

    @property
    def backend(self) -> str:
        # 有 api_key 一律返回 "http"：该值被 13 处写进 UI 文案与 meta
        # （pipeline_llm / deploy / mission_briefing / skill_install / api），
        # 接入层换成 langchain 后底层实现变了但对外语义必须保持。
        if self.api_key:
            return "http"
        if self.cli_fallback and shutil.which("claude"):
            return "claude_cli"
        return "none"

    async def chat_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        raw = await self.chat_text(
            system=system,
            user=user,
            temperature=temperature,
            json_mode=True,
        )
        # Tolerate Claude wrapping JSON in fences
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
            if "```" in text:
                text = text.split("```", 1)[0]
        return json.loads(text)

    async def chat_text(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
        json_mode: bool = False,
    ) -> str:
        if not self.enabled:
            raise RuntimeError("LLM API key not configured and Claude CLI unavailable")
        if self.api_key:
            return await self._chat_chain(
                system=system, user=user, temperature=temperature, json_mode=json_mode
            )
        return await self._chat_claude_cli(system=system, user=user, json_mode=json_mode)

    async def _chat_chain(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        json_mode: bool,
    ) -> str:
        """LangChain ChatModel 后端（默认）。

        异常在后端已归一化为 ``RuntimeError``，上游 ``except Exception``
        降级逻辑（含启发式/模板回退）无需改动。
        """
        from sortie_deck.llm_chain import get_backend

        backend = get_backend(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            timeout=self.timeout,
        )
        return await backend.complete(
            system=system, user=user, temperature=temperature, json_mode=json_mode
        )

    async def _chat_claude_cli(self, *, system: str, user: str, json_mode: bool) -> str:
        claude = shutil.which("claude")
        if not claude:
            raise RuntimeError("claude CLI not found on PATH")
        prompt = f"{system.strip()}\n\n---\n\n{user.strip()}"
        if json_mode:
            prompt += "\n\nReturn ONLY a valid JSON object. No markdown fences."
        proc = await asyncio.create_subprocess_exec(
            claude,
            "-p",
            prompt,
            "--output-format",
            "text",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"claude CLI timed out after {self.timeout}s") from None
        out = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        err = (stderr_b or b"").decode("utf-8", errors="replace").strip()
        if (proc.returncode or 0) != 0 or not out:
            raise RuntimeError(err or f"claude CLI exit={proc.returncode}")
        return out


def _cursor_agent_ready() -> bool:
    """True when cursor-agent/agent is on PATH and looks authenticated.

    IDE config files under ``~/.cursor/`` do NOT imply Agent CLI login — only
    ``CURSOR_API_KEY`` or an explicit agent auth token file counts.
    """
    if not (shutil.which("cursor-agent") or shutil.which("agent")):
        return False
    if (os.environ.get("CURSOR_API_KEY") or "").strip():
        return True
    home = Path.home()
    # Written by `cursor-agent login` / `agent login` (not the IDE)
    for p in (
        home / ".cursor" / "agent-auth.json",
        home / ".config" / "cursor-agent" / "auth.json",
        home / ".local" / "share" / "cursor-agent" / "auth.json",
    ):
        if p.is_file() and p.stat().st_size > 2:
            return True
    return False


def resolve_coding_executor(
    preferred: str | None = None,
) -> CodingExecutorName:
    """Pick coding agent: explicit preference, else ready Cursor Agent, else Claude Code, else mock."""
    pref = (preferred or os.environ.get("TDT_DEFAULT_CODING_EXECUTOR") or "auto").strip().lower()
    if pref in {"mock", "claude_code", "cursor_cli"}:
        return pref  # type: ignore[return-value]
    if _cursor_agent_ready():
        return "cursor_cli"
    if shutil.which("claude"):
        return "claude_code"
    if shutil.which("cursor-agent") or shutil.which("agent"):
        # Present but not logged in — still return cursor so explicit probes show the gap;
        # prefer Claude above when available so Teams flow stays unblocked.
        return "cursor_cli"
    return "mock"
