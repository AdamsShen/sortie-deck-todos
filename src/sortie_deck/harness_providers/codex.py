"""Codex harness provider — OpenAI Codex agent via its headless JSON protocol.

Runs the ``codex`` binary deployed *inside the Sortie Deck cloud environment*
(``codex exec --json``), which is Codex's supported automation entry point.
This is not a user-machine dependency: the cloud image ships/binds the binary
and its credentials. Session continuity uses ``codex exec resume <id>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from sortie_deck.harness_providers.base import HarnessSession
from sortie_deck.models import utc_now

logger = logging.getLogger(__name__)

# 鉴权失败的关键词：Codex CLI 在未登录/凭据过期时走的是文本报错而不是结构化事件
_AUTH_HINTS = ("not logged in", "codex login", "401 unauthorized", "unauthorized")


def _binary() -> str | None:
    return shutil.which("codex")


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _auth_file() -> Path:
    return _codex_home() / "auth.json"


def _has_login_credentials() -> bool:
    """已登录凭据探测：`codex login` 会把凭据写到 `$CODEX_HOME/auth.json`。

    只认环境变量会让「本机已登录、未导出 OPENAI_API_KEY」的环境被误判为
    auth_required（真机探测实测如此），所以两者都要认。
    """
    path = _auth_file()
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


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
        if not (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("CODEX_API_KEY")
            or _has_login_credentials()
        ):
            return ProbeStatus(
                status="auth_required",
                detail=(
                    "OPENAI_API_KEY / CODEX_API_KEY not set and no Codex login found "
                    f"({_auth_file()})"
                ),
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
        extra_dirs: list[str] | None = None,
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
        from sortie_deck.settings import settings

        args = [binary, "exec", "--json", "--skip-git-repo-check"]
        if session.native_id:
            # 续跑沿用首次会话的沙箱档位（CLI 不接受在 resume 上改沙箱）
            args = [binary, "exec", "resume", session.native_id, "--json", "--skip-git-repo-check"]
        else:
            sandbox = str(getattr(settings, "codex_sandbox", "") or "").strip()
            if sandbox:
                args += ["--sandbox", sandbox]
            # 额外可写目录（阶段产物目录）：codex 首轮通过 --add-dir 扩展沙箱边界；
            # resume 不支持再改沙箱，故只在首轮生效。
            for extra in extra_dirs or []:
                if str(extra or "").strip():
                    args += ["--add-dir", str(extra)]
        args.append(prompt)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd or None,
                stdin=asyncio.subprocess.DEVNULL,  # 服务端无交互终端：避免读到悬空 stdin
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CI": "1"},
            )
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
        text, native_id, errored, usage = _parse_jsonl(stdout)
        if not text.strip() and stderr.strip():
            text = stderr[:4000]
        if _looks_like_auth_failure(stdout, stderr, text):
            # 凭据问题不是阶段失败：交给图转成 HITL（auth_block）等人工处理
            return HarnessReply(
                status="needs_auth",
                text=text or "codex is not authenticated",
                session_id=session.id,
                native_id=native_id or session.native_id,
                meta={"provider": self.name, "exit": proc.returncode or 0, "auth": True},
            )
        status = "ok" if (proc.returncode == 0 and text.strip() and not errored) else "fail"
        return HarnessReply(
            status=status,
            text=text,
            session_id=session.id,
            native_id=native_id or session.native_id,
            meta={"provider": self.name, "exit": proc.returncode or 0, "usage": usage},
        )

    async def close(self, session: HarnessSession) -> None:
        return None


def _parse_jsonl(stdout: str) -> tuple[str, str, bool, dict[str, Any]]:
    """解析 ``codex exec --json`` 的 JSONL，返回（正文, 会话 id, 是否出错, usage）。

    同时兼容两代协议（真机实测新版才有 item.* 事件）：
    - 新版：``{"type":"thread.started","thread_id":...}`` /
      ``{"type":"item.completed","item":{"type":"agent_message","text":...}}`` /
      ``{"type":"turn.completed","usage":{...}}``
    - 旧版：``{"type":"agent_message","message":...}`` 或 `msg` 包装
    只认 agent_message：reasoning 等中间产物不能当正文，否则阶段会被噪声污染。
    """
    text_chunks: list[str] = []
    native_id = ""
    errored = False
    usage: dict[str, Any] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        etype = str(event.get("type") or "")
        if etype == "thread.started":
            native_id = str(event.get("thread_id") or event.get("session_id") or native_id)
            continue
        if etype == "turn.completed":
            if isinstance(event.get("usage"), dict):
                usage = dict(event["usage"])
            continue
        if etype == "item.completed":
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            itype = str(item.get("type") or "")
            if itype == "agent_message":
                text_chunks.append(str(item.get("text") or ""))
            elif itype == "error":
                # 单项错误只告警、不判失败：实测 codex 会把配置告警也发成
                # item.type=error，同一轮通常仍有可用的 agent_message。
                logger.warning("codex item error: %s", str(item.get("message") or "")[:300])
            continue
        if etype in {"error", "stream_error"}:
            errored = True
            logger.warning("codex stream error: %s", str(event.get("message") or "")[:300])
            continue
        # 旧版协议回退
        native_id = str(event.get("session_id") or event.get("thread_id") or native_id) or native_id
        msg = event.get("msg") if isinstance(event.get("msg"), dict) else event
        if str(msg.get("type") or "") == "agent_message":
            text_chunks.append(str(msg.get("message") or ""))
        elif isinstance(msg.get("message"), str) and msg.get("role") in {"assistant", None}:
            text_chunks.append(str(msg["message"]))
    return "\n".join(c for c in text_chunks if c.strip()).strip(), native_id, errored, usage


def _looks_like_auth_failure(stdout: str, stderr: str, text: str) -> bool:
    """判断失败是否源于凭据（而非任务本身），据此返回 needs_auth 走 HITL。"""
    blob = f"{stderr}\n{stdout}\n{text}".lower()
    return any(hint in blob for hint in _AUTH_HINTS)
