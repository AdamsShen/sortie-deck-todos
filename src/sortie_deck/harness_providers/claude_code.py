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


def _agent_definitions(subagents: dict[str, Any], disallowed: list[str]) -> dict[str, Any]:
    """把编排层的 canonical 子代理计划渲染成 SDK ``AgentDefinition`` 映射。

    输入：``pack["subagents"]``（见 ``sortie_deck.subagents``）+ 本阶段禁用工具。
    输出：``{agent_id: AgentDefinition}``；空 dict 表示本阶段不提供子代理。

    只做两件事：能力标签 → Claude 工具名、剔除被阶段禁用的工具。路由描述与提示词
    原样透传——那是父模型判断"何时委派"的唯一信号，provider 不得改写。
    单条渲染失败只丢该条并告警：子代理是增强项，不能打挂阶段。
    """
    rows = list((subagents or {}).get("agents") or [])
    if not rows:
        return {}
    try:
        from claude_agent_sdk import AgentDefinition  # type: ignore[import-untyped]
    except Exception:  # noqa: BLE001 — 旧 SDK 没有该类型
        logger.info("claude-agent-sdk 不支持 AgentDefinition，跳过子代理注入")
        return {}

    from sortie_deck.subagents import tool_names

    banned = {str(name) for name in disallowed if str(name or "").strip()}
    defs: dict[str, Any] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            tools = [
                name
                for name in tool_names("claude_code", list(row.get("capabilities") or []))
                if name not in banned
            ]
            kwargs: dict[str, Any] = {
                "description": str(row.get("description") or ""),
                "prompt": str(row.get("prompt") or ""),
            }
            if tools:
                kwargs["tools"] = tools
            # SDK 字段是 camelCase（匹配线路格式），Python 侧也保持一致
            if int(row.get("max_turns") or 0) > 0:
                kwargs["maxTurns"] = int(row["max_turns"])
            if str(row.get("model") or "").strip():
                kwargs["model"] = str(row["model"]).strip()
            defs[str(row.get("id") or "")] = AgentDefinition(**kwargs)
        except Exception:  # noqa: BLE001 — 单条定义失败不影响其它
            logger.warning("子代理定义渲染失败: id=%s", row.get("id"), exc_info=True)
    return defs


def _harness_env(settings: Any) -> dict[str, str]:
    """本阶段 SDK env 增量：压缩窗口 + 内置子代理开关（SDK 对 env 是合并语义）。"""
    env = dict(_auto_compact_env(getattr(settings, "harness_auto_compact_window", 0)) or {})
    if bool(getattr(settings, "subagent_disable_builtin", False)):
        # 内置 Explore/general-purpose 会与我们供给的 scout 抢路由；观测到冲突再打开
        env["CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS"] = "1"
    return env


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


def _max_buffer_size(value: Any) -> int | None:
    """把配置的单条消息上限转成 SDK 参数；0/None/非法值返回 None（用 SDK 默认）。

    SDK 默认 1 MiB，agent Read 图片时 base64 后的消息很容易超过它。
    """
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _buffer_overflow_hint(exc: BaseException, configured: int | None) -> str:
    """识别 SDK 的缓冲溢出错误并给出可执行提示；其它异常返回空串。"""
    if "maximum buffer size" not in str(exc):
        return ""
    limit = configured or 1024 * 1024
    return (
        "（单条消息超过 max_buffer_size，常见诱因是 agent Read 了图片/大文件；"
        f"当前上限 {limit} 字节，可调大 TDT_HARNESS_MAX_BUFFER_BYTES）"
    )


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
            from sortie_deck.settings import settings

            limit = _max_buffer_size(settings.harness_max_buffer_bytes)
            hint = _buffer_overflow_hint(exc, limit)
            logger.exception(
                "claude-agent-sdk 调用失败: initiative=%s stage=%s session=%s "
                "error=%s buffer_overflow=%s limit=%s",
                (session.meta or {}).get("initiative_id"),
                (session.meta or {}).get("stage"),
                session.id,
                type(exc).__name__,
                bool(hint),
                limit,
            )
            return HarnessReply(
                status="fail",
                text=f"claude-agent-sdk run failed: {exc}{hint}",
                session_id=session.id,
                meta={
                    "error": str(exc)[:300],
                    "error_type": type(exc).__name__,
                    "buffer_overflow": bool(hint),
                    "max_buffer_size": limit,
                },
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
        # 任务级 override 优先（HITL 提权 / 阶段策略），否则回落全局配置；
        # 此前恒取全局值，SDK 路径下任务级提权完全失效。
        permission_mode = str(pack.get("permission_mode") or settings.coding_permission_mode)
        # 沙箱配置走 SDK settings 文件：路径存在才传，避免本地未部署配置时报错。
        settings_file = str(getattr(settings, "harness_settings_file", "") or "").strip()
        settings_option = settings_file if settings_file and Path(settings_file).is_file() else None
        if settings_file and settings_option is None:
            logger.warning("沙箱配置文件不存在，忽略: %s", settings_file)
        options_kwargs: dict[str, Any] = {
            "cwd": cwd or None,
            "permission_mode": permission_mode,
            "settings": settings_option,
            "disallowed_tools": disallowed or None,
            "resume": session.native_id or None,
            "setting_sources": _setting_sources(settings.harness_setting_sources),
            # 额外可写目录（阶段产物目录 / 沙盒）：CLI 会把它当 --add-dir 处理。
            # 注意 add_dirs 同时会加载该目录下的 .claude 配置（skills/commands），
            # 阶段产物目录里没有这类文件，影响可忽略。
            "add_dirs": [str(d) for d in (extra_dirs or []) if str(d or "").strip()],
            # strict 模式下这就是本阶段 MCP 的全部；空 dict = 本阶段不挂任何 MCP
            "mcp_servers": dict(pack.get("mcp_servers") or {}),
            "strict_mcp_config": strict,
            "env": _harness_env(settings),
            # 阶段回合上限：intake 等对齐阶段封顶（stage_max_turns），编码阶段默认不限
            "max_turns": resolve_stage_max_turns(str((session.meta or {}).get("stage", "")))
            or None,
            # 单条 CLI 消息上限：默认 1 MiB，agent Read 截图时 base64 后会溢出
            "max_buffer_size": _max_buffer_size(settings.harness_max_buffer_bytes),
        }
        # 子代理池（默认仅只读 scout）：SDK 有 agents 字段才传，旧 SDK 忽略（零回归）
        agent_defs = _agent_definitions(pack.get("subagents") or {}, disallowed)
        if agent_defs and "agents" in getattr(ClaudeAgentOptions, "__dataclass_fields__", {}):
            options_kwargs["agents"] = agent_defs
            logger.info(
                "注入子代理: session=%s agents=%s",
                session.id,
                ",".join(sorted(agent_defs)),
            )
        # 角色级模型：SDK 支持该字段时原样透传；旧版本 SDK 没有则忽略（零回归）
        model = str(pack.get("model") or "").strip()
        if model and "model" in getattr(ClaudeAgentOptions, "__dataclass_fields__", {}):
            options_kwargs["model"] = model
        options = ClaudeAgentOptions(**options_kwargs)
        parts: list[str] = []
        native_id = session.native_id
        is_error = False
        context_meta: dict[str, Any] = {}
        last_activity = 0.0
        # 子代理观测（P0）：calls = 委派次数，steps = 子代理内部的 assistant 消息数，
        # task_tool 来自 system:init 的工具清单，None 表示没收到 init 事件。
        subagent_meta: dict[str, Any] = {"calls": 0, "steps": 0, "task_tool": None, "agents": []}
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                # 子代理内部的 assistant 消息带 parent_tool_use_id：用它区分
                # 「主线程在干活」与「子代理在干活」，否则 142 步的账算不到一起
                if str(getattr(message, "parent_tool_use_id", "") or ""):
                    subagent_meta["steps"] += 1
                for block in getattr(message, "content", []) or []:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        parts.append(text)
                        continue
                    # ToolUseBlock：把「此刻在做什么」推给编排层，转圈才有细节可展示。
                    # 节流 0.8s：一轮里连续读多个文件时不刷屏，也少写事件表。
                    tool = getattr(block, "name", None)
                    if isinstance(tool, str) and tool in {"Task", "Agent"}:
                        # 一次委派：记次数与被选中的子代理类型（不同 SDK 版本叫法不同）
                        params = getattr(block, "input", {}) or {}
                        subagent_meta["calls"] += 1
                        agent_type = str(
                            params.get("subagent_type") or params.get("agent_type") or ""
                        ).strip()
                        if agent_type and agent_type not in subagent_meta["agents"]:
                            subagent_meta["agents"].append(agent_type)
                        logger.info(
                            "子代理委派: session=%s agent=%s purpose=%s",
                            session.id,
                            agent_type or "(default)",
                            str(params.get("description") or "")[:80],
                        )
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
                subtype = str(getattr(message, "subtype", "") or "")
                # init 事件带本次会话的工具清单：据此判断委派能力位是否真的开着，
                # 而不是假设「SDK 装了就有 Task 工具」
                if subtype == "init":
                    data = getattr(message, "data", None) or {}
                    tools = {str(t) for t in (data.get("tools") or []) if isinstance(t, str)}
                    subagent_meta["task_tool"] = bool(tools & {"Task", "Agent"})
                # 压缩边界事件：只记日志与 meta，不把摘要正文带进产物
                if subtype == "compact_boundary":
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
        context_meta["subagents"] = subagent_meta
        if subagent_meta["calls"] or subagent_meta["steps"]:
            logger.info(
                "子代理用量: session=%s calls=%d steps=%d agents=%s",
                session.id,
                subagent_meta["calls"],
                subagent_meta["steps"],
                ",".join(subagent_meta["agents"]) or "-",
            )
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
