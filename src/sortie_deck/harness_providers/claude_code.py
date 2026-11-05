"""Claude Code harness provider — via ``claude-agent-sdk`` (cloud in-process).

The SDK drives Claude Code's agent loop (tools, MCP, skills, permissions) from
inside the Sortie Deck process; nothing is invoked on a user's local machine.
Session continuity uses the SDK ``resume`` option with the native session id.

上下文成本相关（见 docs/plans/2026-10-27-harness-context-cost.md）：
- ``mcp_servers`` 只接收执行器按任务绑定/召回解析出的 MCP；``strict_mcp_config``
  打开时不再从本机 ``~/.claude.json`` 全量加载全局 mcpServers。
- ``env`` 注入 ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``，把「何时压缩」从模型默认阈值
  （约 200K/967K）提前到可配置窗口；SDK 对 ``options.env`` 是合并语义
  （`{**os.environ, **options.env}`），因此只需传增量键。
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import time
from pathlib import Path
from typing import Any

from sortie_deck.harness_providers.base import HarnessSession, resolve_stage_max_turns
from sortie_deck.models import utc_now

logger = logging.getLogger(__name__)

# Claude Code 官方支持的环境变量名（有效区间 100K–1M）
_AUTO_COMPACT_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"


def _sdk_available() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


def _activity_detail(payload: dict[str, Any]) -> str:
    """从工具入参里挑一个人类可读的短目标（文件名 / 命令 / 检索词）。

    前端把它拼成「编辑 src/game.js」「运行 npm test」这类文案；只取第一个
    命中的键，避免把整段 prompt 塞进事件表。
    """
    for key in ("file_path", "notebook_path", "path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).name
    for key in ("command", "pattern", "query", "url", "description", "prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:60]
    return ""


def _auto_compact_env(window: Any) -> dict[str, str] | None:
    """把配置的压缩窗口转成 SDK env；0/None/非法值返回 None（不注入）。

    官方文档：裸数字按 token 解释（``200`` = 200 token，不是 200K），
    有效范围 100_000–1_000_000，超出会被 Claude Code 钳制并告警。
    """
    try:
        value = int(window or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value < 100_000:
        logger.warning(
            "自动压缩窗口 %s 低于官方下限 100000，Claude Code 会按 100K 处理", value
        )
    return {_AUTO_COMPACT_ENV: str(value)}


def _compact_meta(data: dict[str, Any]) -> dict[str, Any]:
    """从 compact_boundary 事件里提取可上报的字段（字段名兼容蛇形/驼峰）。"""
    def pick(*keys: str) -> Any:
        for key in keys:
            if key in data and data[key] is not None:
                return data[key]
        return None

    return {
        "compacted": True,
        "at_token": pick("preTokens", "pre_tokens", "tokensBefore", "tokens_before"),
        "post_token": pick("postTokens", "post_tokens", "tokensAfter", "tokens_after"),
        "trigger": pick("trigger", "reason"),
    }


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
        extra_dirs: list[str] | None = None,
        on_activity: Any = None,
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
            return await self._send_via_sdk(
                session,
                prompt,
                cwd=cwd,
                context_pack=context_pack,
                extra_dirs=extra_dirs,
                on_activity=on_activity,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to a fail reply, never raise
            return HarnessReply(
                status="fail",
                text=f"claude-agent-sdk run failed: {exc}",
                session_id=session.id,
                meta={"error": str(exc)[:300]},
            )

    async def _send_via_sdk(
        self,
        session: HarnessSession,
        prompt: str,
        *,
        cwd: str | None,
        context_pack: dict[str, Any] | None = None,
        extra_dirs: list[str] | None = None,
        on_activity: Any = None,
    ) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore[import-untyped]
        from claude_agent_sdk.types import (  # type: ignore[import-untyped]
            AssistantMessage,
            ResultMessage,
            SystemMessage,
        )

        from sortie_deck.harness_providers.base import HarnessReply
        from sortie_deck.settings import settings

        pack = context_pack or {}
        strict = bool(settings.harness_mcp_strict)
        # 阶段级禁用工具（如 tech_design 禁 Bash）：SDK 会从可用工具列表里摘掉，
        # agent 连尝试调用的机会都没有，避免「写方案」退化成「提前实现」。
        disallowed = [
            str(name).strip()
            for name in (pack.get("disallowed_tools") or [])
            if str(name or "").strip()
        ]
        options = ClaudeAgentOptions(
            cwd=cwd or None,
            permission_mode=settings.coding_permission_mode,
            disallowed_tools=disallowed or None,
            resume=session.native_id or None,
            setting_sources=_setting_sources(settings.harness_setting_sources),
            # 额外可写目录（阶段产物目录 / 沙盒）：CLI 会把它当 --add-dir 处理。
            # 注意 add_dirs 同时会加载该目录下的 .claude 配置（skills/commands），
            # 阶段产物目录里没有这类文件，影响可忽略。
            add_dirs=[str(d) for d in (extra_dirs or []) if str(d or "").strip()],
            # strict 模式下这就是本阶段 MCP 的全部；空 dict = 本阶段不挂任何 MCP
            mcp_servers=dict(pack.get("mcp_servers") or {}),
            strict_mcp_config=strict,
            env=_auto_compact_env(settings.harness_auto_compact_window),
            # 阶段回合上限：intake 等对齐阶段封顶（stage_max_turns），编码阶段默认不限
            max_turns=resolve_stage_max_turns(str((session.meta or {}).get("stage", ""))) or None,
        )
        parts: list[str] = []
        native_id = session.native_id
        is_error = False
        context_meta: dict[str, Any] = {}
        last_activity = 0.0
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in getattr(message, "content", []) or []:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        parts.append(text)
                        continue
                    # ToolUseBlock：把「此刻在做什么」推给编排层，转圈才有细节可展示。
                    # 节流 0.8s：一轮里连续读多个文件时不刷屏，也少写事件表。
                    tool = getattr(block, "name", None)
                    if on_activity is None or not isinstance(tool, str) or not tool.strip():
                        continue
                    now = time.monotonic()
                    if now - last_activity < 0.8:
                        continue
                    last_activity = now
                    try:
                        emitted = on_activity(
                            tool, _activity_detail(getattr(block, "input", {}) or {})
                        )
                        if inspect.isawaitable(emitted):
                            await emitted
                    except Exception:  # noqa: BLE001 — 进度上报失败不影响回合
                        logger.debug("阶段活动上报失败: tool=%s", tool, exc_info=True)
            elif isinstance(message, ResultMessage):
                result_text = getattr(message, "result", "")
                if isinstance(result_text, str) and result_text.strip():
                    parts.append(result_text)
                native_id = getattr(message, "session_id", native_id) or native_id
                is_error = bool(getattr(message, "is_error", False))
                context_meta.update(
                    {
                        "num_turns": int(getattr(message, "num_turns", 0) or 0),
                        "usage": getattr(message, "usage", None) or {},
                        "cost_usd": getattr(message, "total_cost_usd", None),
                        "stop_reason": getattr(message, "stop_reason", None),
                    }
                )
            elif isinstance(message, SystemMessage):
                # 压缩边界事件：只记日志与 meta，不把摘要正文带进产物
                if str(getattr(message, "subtype", "") or "") == "compact_boundary":
                    data = getattr(message, "data", None) or {}
                    context_meta.update(_compact_meta(data))
                    logger.info(
                        "上下文已压缩: session=%s pre=%s post=%s trigger=%s",
                        session.id,
                        context_meta.get("at_token"),
                        context_meta.get("post_token"),
                        context_meta.get("trigger"),
                    )
                continue
        text = "\n".join(p for p in parts if p.strip()).strip()
        return HarnessReply(
            status="fail" if (is_error and not text) else "ok",
            text=text,
            session_id=session.id,
            native_id=native_id,
            meta={"provider": self.name, "context": context_meta},
        )

    async def close(self, session: HarnessSession) -> None:
        return None


def _setting_sources(raw: str | None) -> list[str] | None:
    """把配置值解析成 SDK 的 ``setting_sources``。

    - ``""``（默认）→ ``None``：按 CLI 默认加载 user/project/local；
    - ``"none"`` → ``[]``：SDK 隔离模式，本机 settings / 插件 hooks 都不加载；
    - 其余按逗号分隔取 ``user`` / ``project`` / ``local``。
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text in {"none", "off", "isolated"}:
        return []
    allowed = {"user", "project", "local"}
    picked = [part.strip() for part in text.split(",") if part.strip() in allowed]
    return picked or None


def _has_claude_cli_creds() -> bool:
    """Heuristic: the SDK bundles/uses Claude Code credentials when present."""
    from pathlib import Path

    home = Path.home()
    return (home / ".claude" / ".credentials.json").exists() or (home / ".claude.json").exists()
