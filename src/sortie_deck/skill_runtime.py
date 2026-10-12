"""Skill script execution.

``toolkit.py`` *stores* skill scripts; this module actually runs them — with a
language whitelist, a hard timeout, a working-directory sandbox, and a
confirmation gate for anything that looks like a write operation.

Everything fails closed: a disabled runtime, a non-executable item, an
unsupported language, or an unconfirmed write all return a blocked result
instead of raising.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sortie_deck.toolkit import SkillScript, ToolkitItem

# language alias -> (runner executable or None for sys.executable, temp suffix)
_RUNNERS: dict[str, tuple[str | None, str]] = {
    "py": (None, ".py"),
    "python": (None, ".py"),
    "sh": ("bash", ".sh"),
    "bash": ("bash", ".sh"),
    "js": ("node", ".js"),
    "node": ("node", ".js"),
}

# Heuristic write detector — deliberately conservative and documented as such.
_WRITE_RE = re.compile(
    r"(>>?"
    r"|\brm\s|\bmv\s|\bcp\s|\bmkdir\s|\btouch\s"
    r"|open\([^)]*['\"][wa]"
    r"|shutil\.(rmtree|move|copy)"
    r"|\bgit\s+(commit|push|reset|checkout)"
    r"|\bcurl\b[^\n]*-X\s*(POST|PUT|PATCH|DELETE)"
    r"|\bwget\b)"
    r"|\b(drop|truncate)\s+table\b",
    re.IGNORECASE,
)


class SkillRunResult(BaseModel):
    ok: bool = False
    blocked: bool = False
    reason: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    write_detected: bool = False
    language: str = ""


def detect_write(text: str) -> bool:
    """True when the script looks like it mutates state (heuristic)."""
    return bool(_WRITE_RE.search(text or ""))


def resolve_language(script: SkillScript) -> str:
    lang = (script.language or "").strip().lower()
    if lang:
        return lang
    suffix = Path(script.path or "").suffix.lstrip(".").lower()
    return suffix


class SkillRuntime:
    """Runs skill scripts inside a bounded sandbox."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        timeout_seconds: float = 30.0,
        workdir: Path | None = None,
        allowed_languages: tuple[str, ...] = ("py", "python", "sh", "bash"),
        max_output_chars: int = 20000,
        require_confirm_for_write: bool = True,
    ) -> None:
        self.enabled = enabled
        self.timeout_seconds = float(timeout_seconds)
        self.workdir = workdir
        self.allowed_languages = tuple(allowed_languages)
        self.max_output_chars = int(max_output_chars)
        self.require_confirm_for_write = bool(require_confirm_for_write)

    def availability(self, item: ToolkitItem) -> tuple[bool, str]:
        """Whether this item could run at all (used by the loadout probe)."""
        if not self.enabled:
            return False, "skill runtime is disabled"
        if not item.executable:
            return False, "skill is not marked executable"
        if not item.scripts:
            return False, "skill has no scripts"
        for script in item.scripts:
            if resolve_language(script) not in self.allowed_languages:
                return False, f"language `{resolve_language(script)}` is not allowed"
        return True, f"{len(item.scripts)} runnable script(s)"

    def find_script(self, item: ToolkitItem, path: str) -> SkillScript | None:
        for script in item.scripts:
            if script.path == path:
                return script
        return None

    async def run(
        self,
        item: ToolkitItem,
        script: SkillScript,
        *,
        confirmed: bool = False,
        env: dict[str, str] | None = None,
    ) -> SkillRunResult:
        language = resolve_language(script)
        result = SkillRunResult(language=language)
        if not self.enabled:
            result.blocked = True
            result.reason = "skill runtime is disabled"
            return result
        if not item.executable:
            result.blocked = True
            result.reason = f"skill `{item.name}` is not marked executable"
            return result
        if language not in self.allowed_languages:
            result.blocked = True
            result.reason = f"language `{language}` is not allowed (allowed: {self.allowed_languages})"
            return result
        runner, suffix = _RUNNERS.get(language, (None, ".txt"))
        if runner is None and language not in {"py", "python"}:
            result.blocked = True
            result.reason = f"language `{language}` has no runner configured"
            return result
        binary = sys.executable if runner is None else shutil.which(runner)
        if not binary:
            result.blocked = True
            result.reason = f"runner `{runner}` not found on PATH"
            return result

        writes = detect_write(script.content)
        result.write_detected = writes
        if writes and self.require_confirm_for_write and not confirmed:
            result.blocked = True
            result.reason = "script looks like a write operation — confirmation required"
            return result

        workdir = Path(self.workdir) if self.workdir else Path(tempfile.mkdtemp(prefix="sortie-skill-"))
        workdir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(suffix=suffix, dir=str(workdir))
        os.close(fd)
        tmp_path = Path(tmp_name)
        started = time.monotonic()
        try:
            tmp_path.write_text(script.content, encoding="utf-8")
            merged_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
            proc = await asyncio.create_subprocess_exec(
                str(binary),
                str(tmp_path),
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_seconds
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                result.reason = f"script timed out after {self.timeout_seconds:.0f}s"
                return result
            result.exit_code = proc.returncode
            result.stdout = stdout.decode("utf-8", "replace")[: self.max_output_chars]
            result.stderr = stderr.decode("utf-8", "replace")[: self.max_output_chars]
            result.ok = proc.returncode == 0
            if not result.ok:
                result.reason = f"script exited with {proc.returncode}"
            return result
        except Exception as exc:  # noqa: BLE001 — a broken script must not break the stage
            result.reason = f"script failed to start: {exc}"
            return result
        finally:
            result.duration_ms = int((time.monotonic() - started) * 1000)
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)

    async def run_path(
        self,
        item: ToolkitItem,
        path: str,
        *,
        confirmed: bool = False,
        env: dict[str, str] | None = None,
    ) -> SkillRunResult:
        script = self.find_script(item, path)
        if script is None:
            return SkillRunResult(blocked=True, reason=f"script `{path}` not found in skill")
        return await self.run(item, script, confirmed=confirmed, env=env)

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "timeout_seconds": self.timeout_seconds,
            "allowed_languages": list(self.allowed_languages),
            "require_confirm_for_write": self.require_confirm_for_write,
        }
