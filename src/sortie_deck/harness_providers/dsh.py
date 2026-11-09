"""DSH（DeepSeek Harness）harness provider —— 官方 Python SDK 接入。

SDK 形态：``deepseek-harness-sdk`` 以子进程方式启动随包附带的 dsh 运行时，走
stdio 上的换行分隔 JSON-RPC；因此 provider 不需要系统 Node，也不调用用户机器。

生命周期：执行器不会调用 ``provider.close()``（见 HarnessProvider 协议），而
``Session.run()`` 阻塞且没有 cancel，所以**每轮新建进程、跑完即关**，多轮之间
靠 ``resume_session(native_id)`` 续跑（PoC 实测跨进程恢复可用，冷启动约 0.73s）。
这样每次 send 都能按阶段生成 patch（工具禁用 / MCP 挂载），不会把上一阶段的
能力带进下一阶段。

与 claude_code 的已知差异（均在日志/文档显式标注，不静默）：
- 一个会话只有一个可写根（sandbox 策略不支持额外可写目录），``extra_dirs`` 只
  能靠「读不受限 + 软链」间接使用，产物落盘走既有归档兜底；
- 没有回合上限配置，长回合只能靠请求超时兜底；
- 工具只能整行禁用（``tool-fs`` 同注册 read/write/edit），无法只禁 ``Read``；
- 事件里没有 token 用量/成本字段。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from sortie_deck.harness_providers.base import HarnessSession
from sortie_deck.models import utc_now

logger = logging.getLogger(__name__)

# 工具级进度节流：与 claude_code provider / executor 保持同一节奏，避免刷屏。
_ACTIVITY_MIN_INTERVAL = 0.8

# 随包发布的静态 patch：默认关闭会话日志上传、切断宿主机用户级技能根。
_BASE_PATCH = Path(__file__).with_name("dsh_patches") / "base.yml"

# MCP server 名在 DSH 侧有字符与长度约束（[A-Za-z0-9_-]{1,32}）
_SERVER_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

# Claude Code 工具名 → DSH profile 里的**工具行 id**。
# 注意：DSH 只能禁用整行，没有按工具粒度的开关（已核对 dsh-tools / dsh-tool-fs 配置）。
_TOOL_ROW_BY_NAME: dict[str, str] = {
    "Bash": "tool-bash",
    "Grep": "tool-fs-search",
    "Glob": "tool-fs-search",
    "Task": "tool-subagent",
    "WebFetch": "tool-web",
    "WebSearch": "tool-web",
    "TodoWrite": "tool-todo",
    "Write": "tool-fs",
    "Edit": "tool-fs",
}

# 无法精确映射的工具：记 warn 后忽略（不能因为禁用 Read 把 Write/Edit 一起关掉）
_UNMAPPED_TOOL_HINTS: dict[str, str] = {
    "Read": "tool-fs 同时注册 read/write/edit，DSH 无法只禁用 read",
    "NotebookEdit": "DSH 没有 notebook 工具",
}

# 从工具入参里挑人类可读短目标的键序（与 claude_code provider 一致）
_DETAIL_FILE_KEYS = ("file_path", "notebook_path", "path")
_DETAIL_TEXT_KEYS = ("command", "pattern", "query", "url", "description", "prompt")


def _sdk_available() -> bool:
    return importlib.util.find_spec("deepseek_harness") is not None


def _api_key() -> str:
    """凭据：settings 优先，其次进程环境（SDK 也认后者，这里提前用于 probe）。"""
    from sortie_deck.settings import settings

    return str(
        getattr(settings, "dsh_api_key", "") or os.environ.get("DEEPSEEK_API_KEY", "")
    ).strip()


def _permission_mode() -> str:
    from sortie_deck.settings import settings

    mode = str(getattr(settings, "dsh_permission_mode", "") or "workspace-write").strip()
    return (
        mode
        if mode in {"read-only", "workspace-write", "danger-full-access"}
        else "workspace-write"
    )


def _dsh_home(initiative_id: str) -> Path:
    """任务级 DSH_HOME：会话、设置、agents 根都落在这里，禁止使用 ~/.dsh。"""
    from sortie_deck.settings import settings

    root = str(getattr(settings, "dsh_home_root", "") or "").strip()
    base = Path(root) if root else Path(settings.data_dir) / "dsh_home"
    home = base / (initiative_id or "unknown")
    home.mkdir(parents=True, exist_ok=True)
    return home


def _request_timeout() -> float:
    from sortie_deck.settings import settings

    configured = float(getattr(settings, "dsh_request_timeout_seconds", 0.0) or 0.0)
    if configured > 0:
        return configured
    return float(getattr(settings, "coding_timeout_seconds", 900.0) or 900.0)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """tool/call 的 arguments 是 JSON 字符串（实测），解析失败按空 dict 处理。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _activity_detail(payload: dict[str, Any]) -> str:
    """从工具入参里挑一个短目标（文件名 / 命令 / 检索词），与 claude 侧同款取键顺序。"""
    for key in _DETAIL_FILE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).name
    for key in _DETAIL_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:60]
    return ""


def _tool_off_patch(disallowed: list[str]) -> tuple[str, list[str]]:
    """把 Claude 风格的工具名转成 DSH 的「禁用整行」patch。

    返回 (patch 文本, 未映射说明)；没有可禁用行时返回空文本。
    """
    rows: list[str] = []
    notes: list[str] = []
    for raw in disallowed:
        name = str(raw or "").strip()
        if not name:
            continue
        row = _TOOL_ROW_BY_NAME.get(name) or _TOOL_ROW_BY_NAME.get(name.lower().capitalize(), "")
        if row:
            if row not in rows:
                rows.append(row)
            continue
        hint = _UNMAPPED_TOOL_HINTS.get(name)
        notes.append(f"{name}: {hint}" if hint else f"{name}: DSH 无对应工具行，已忽略")
    if not rows:
        return "", notes
    lines: list[str] = []
    for row in rows:
        lines.extend([f"- id: {row}", "  disabled: true"])
    return "\n".join(lines) + "\n", notes


def _mcp_patch(servers: dict[str, Any]) -> tuple[str, list[str]]:
    """把执行器解析出的 MCP 配置转成 dsh-mcp-client 的 insert patch。

    输入形如 ``{"poc": {"type": "stdio", "command": "...", "args": [...]}}``；
    输出 (patch 文本, 跳过说明)。env/headers 里的凭据只写入临时 patch 文件，不记日志。
    """
    entries: list[str] = []
    skipped: list[str] = []
    used: set[str] = set()
    for raw_name, raw_cfg in (servers or {}).items():
        if not isinstance(raw_cfg, dict):
            continue
        name = _SERVER_NAME_RE.sub("_", str(raw_name or "")).strip("_")[:32] or "mcp"
        while name in used:
            name = f"{name[:28]}_{len(used)}"
        used.add(name)
        transport = str(raw_cfg.get("type") or "stdio").lower()
        if transport == "sse":
            # DSH 只支持 stdio / streamable-http，sse 需上游提供 http 端点
            skipped.append(f"{raw_name}: DSH 不支持 sse transport")
            continue
        block = [
            f"    - id: mcp-{name}",
            "      name: '@deepseek-ai/dsh-mcp-client'",
            "      config:",
            f"        serverName: {json.dumps(name)}",
        ]
        if transport == "stdio":
            command = str(raw_cfg.get("command") or "").strip()
            if not command:
                skipped.append(f"{raw_name}: stdio 缺少 command")
                continue
            block.append("        transport: stdio")
            block.append(f"        command: {json.dumps(command)}")
            args = [str(a) for a in (raw_cfg.get("args") or []) if str(a or "").strip()]
            if args:
                block.append("        args:")
                block.extend(f"          - {json.dumps(arg)}" for arg in args)
            env = raw_cfg.get("env") if isinstance(raw_cfg.get("env"), dict) else {}
            if env:
                block.append("        env:")
                block.extend(
                    f"          {key}: {json.dumps(str(value))}" for key, value in env.items()
                )
        else:
            url = str(raw_cfg.get("url") or "").strip()
            if not url:
                skipped.append(f"{raw_name}: {transport} 缺少 url")
                continue
            block.append("        transport: streamable-http")
            block.append(f"        url: {json.dumps(url)}")
            headers = raw_cfg.get("headers") if isinstance(raw_cfg.get("headers"), dict) else {}
            if headers:
                block.append("        headers:")
                block.extend(
                    f"          {key}: {json.dumps(str(value))}" for key, value in headers.items()
                )
        entries.append("\n".join(block))
    if not entries:
        return "", skipped
    return "- insert:\n" + "\n".join(entries) + "\n", skipped


def _write_patch(directory: Path, name: str, text: str) -> Path | None:
    """把动态 patch 落到 0600 临时文件（里面可能有 MCP 凭据）。"""
    if not text.strip():
        return None
    path = directory / name
    path.write_text(text, encoding="utf-8")
    # Windows 等不支持 chmod 的场景不影响功能
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def _build_patches(
    patch_dir: Path, context_pack: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """组装本轮 patch 列表：静态 base（默认安全姿态）+ 动态工具禁用 + 动态 MCP。"""
    from sortie_deck.settings import settings

    patches: list[str] = []
    meta: dict[str, Any] = {}
    if _BASE_PATCH.is_file():
        patches.append(str(_BASE_PATCH))
    else:  # 打包漏了数据文件时至少要保住「关闭日志上传」这条红线
        logger.warning("DSH base patch 缺失，回退为内联 patch: %s", _BASE_PATCH)
        fallback = _write_patch(
            patch_dir, "base.yml", "- id: session-log-deepseek\n  disabled: true\n"
        )
        if fallback is not None:
            patches.append(str(fallback))

    # 用户显式要求上传会话日志时，用后置 patch 覆盖 base 的 disabled: true
    if bool(getattr(settings, "dsh_session_log_upload", False)) and _BASE_PATCH.is_file():
        override = _write_patch(
            patch_dir, "log-on.yml", "- id: session-log-deepseek\n  disabled: false\n"
        )
        if override is not None:
            patches.append(str(override))
    # 同类覆盖：允许恢复宿主机技能根（默认隔离）
    if not bool(getattr(settings, "dsh_isolate_skills", True)) and _BASE_PATCH.is_file():
        override = _write_patch(
            patch_dir,
            "skills-on.yml",
            "- id: skill-filesystem\n  config:\n    includeDefaultRoots: true\n",
        )
        if override is not None:
            patches.append(str(override))

    tools_patch, tool_notes = _tool_off_patch(list(context_pack.get("disallowed_tools") or []))
    tools_path = _write_patch(patch_dir, "tools-off.yml", tools_patch)
    if tools_path is not None:
        patches.append(str(tools_path))
    if tool_notes:
        meta["tool_notes"] = tool_notes
        logger.info("DSH 工具禁用部分未映射: %s", "; ".join(tool_notes))

    mcp_patch, mcp_skipped = _mcp_patch(dict(context_pack.get("mcp_servers") or {}))
    mcp_path = _write_patch(patch_dir, "mcp.yml", mcp_patch)
    if mcp_path is not None:
        patches.append(str(mcp_path))
    if mcp_skipped:
        meta["mcp_skipped"] = mcp_skipped
        logger.warning("DSH MCP 部分未挂载: %s", "; ".join(mcp_skipped))
    return patches, meta


def _native_session_id(session: HarnessSession) -> str:
    """DSH 侧会话 id：沿用 provider 侧 id（毫秒时间戳保证唯一），非法字符替换。"""
    raw = str(session.id or "").strip() or f"hs_dsh_{utc_now().strftime('%Y%m%d%H%M%S%f')}"
    return _SERVER_NAME_RE.sub("-", raw)[:64]


def _error_of(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """取最后一个 turn/end 的 error 描述（凭据/配额等结构化信息在这里）。"""
    for event in reversed(events or []):
        if event.get("type") != "turn/end":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
        if str(reason.get("kind") or "") != "error":
            return None
        error = reason.get("error")
        return error if isinstance(error, dict) else {}
    return None


def _is_auth_error(error: dict[str, Any] | None) -> bool:
    if not isinstance(error, dict):
        return False
    code = str(error.get("code") or "").upper()
    status = error.get("status")
    message = str(error.get("message") or "").lower()
    if code in {"AUTH", "UNAUTHORIZED", "INVALID_API_KEY"}:
        return True
    if isinstance(status, int) and status in {401, 403}:
        return True
    return "authentication" in message or "invalid api key" in message


class DshProvider:
    """DSH provider：每轮一个子进程，靠 resume 续跑同一会话。"""

    name = "dsh"
    # 一个会话只有一个可写根：额外目录拿不到写权限，执行器据此不传 extra_dirs
    supports_extra_write_dirs = False

    def probe(self) -> Any:
        from sortie_deck.harness_providers.base import ProbeStatus

        if not _sdk_available():
            return ProbeStatus(
                status="missing",
                detail="deepseek-harness-sdk not installed — pip install 'sortie-deck[harness-dsh]' "
                "（需要 --prerelease / 精确 pin 0.2.1rc2）",
            )
        if not _api_key():
            return ProbeStatus(
                status="auth_required",
                detail="DEEPSEEK_API_KEY not set and TDT_DSH_API_KEY empty",
            )
        detail = f"deepseek-harness-sdk ready (profile={_profile()}, model={_model()})"
        return ProbeStatus(status="ok", detail=detail)

    async def start_session(
        self, *, initiative_id: str, stage: str, meta: dict[str, Any] | None = None
    ) -> HarnessSession:
        return HarnessSession(
            id=f"hs_dsh_{utc_now().strftime('%Y%m%d%H%M%S%f')}",
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
                text="deepseek-harness-sdk is not installed in this environment",
                session_id=session.id,
                meta={"sdk_missing": True},
            )
        if extra_dirs:
            # 不静默：DSH 无法给额外目录写权限，读不受限，产物由既有归档兜底
            logger.info(
                "DSH 不支持额外可写目录，extra_dirs 不会生效（走归档兜底）: initiative=%s stage=%s dirs=%s",
                (session.meta or {}).get("initiative_id"),
                (session.meta or {}).get("stage"),
                [Path(str(d)).name for d in extra_dirs if str(d or "").strip()],
            )

        initiative_id = str((session.meta or {}).get("initiative_id") or "unknown")
        home = _dsh_home(initiative_id)
        patch_dir = home / "_patches" / f"{session.id}-{uuid.uuid4().hex[:8]}"
        patch_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            patch_dir.chmod(0o700)
        try:
            patches, patch_meta = _build_patches(patch_dir, context_pack or {})
            loop = asyncio.get_running_loop()
            reply = await asyncio.to_thread(
                self._run_once,
                session,
                prompt,
                cwd,
                home,
                patches,
                loop,
                on_activity,
                patch_meta,
            )
            return reply
        except Exception as exc:  # noqa: BLE001 — 降级为 fail，绝不把异常抛给编排层
            logger.exception(
                "DSH 调用失败: initiative=%s stage=%s session=%s error=%s",
                initiative_id,
                (session.meta or {}).get("stage"),
                session.id,
                type(exc).__name__,
            )
            return HarnessReply(
                status="fail",
                text=f"deepseek-harness-sdk run failed: {exc}",
                session_id=session.id,
                native_id=session.native_id,
                meta={"error": str(exc)[:300], "error_type": type(exc).__name__},
            )
        finally:
            shutil.rmtree(patch_dir, ignore_errors=True)

    def _run_once(
        self,
        session: HarnessSession,
        prompt: str,
        cwd: str | None,
        home: Path,
        patches: list[str],
        loop: Any,
        on_activity: Any,
        patch_meta: dict[str, Any],
    ) -> Any:
        """线程内同步跑一轮：建进程 → resume/start → run → 关进程。"""
        from deepseek_harness import DeepSeekHarness

        from sortie_deck.harness_providers.base import HarnessReply
        from sortie_deck.settings import settings

        timeout = _request_timeout()
        max_tokens = int(getattr(settings, "dsh_max_tokens", 0) or 0)
        env: dict[str, str] = {
            "DSH_PERMISSION_MODE": _permission_mode(),
            # 隔离用户级技能根：不设的话会扫 ~/.agents/skills 并注入任务提示
            "DSH_AGENTS_HOME": str(home / "agents"),
        }
        if bool(getattr(settings, "dsh_isolate_home", False)):
            env["HOME"] = str(home / "home")
            Path(env["HOME"]).mkdir(parents=True, exist_ok=True)

        harness = DeepSeekHarness(
            provider=_llm_provider(),
            model=_model(),
            max_tokens=max_tokens or None,
            reasoning_effort=str(getattr(settings, "dsh_reasoning_effort", "") or "") or None,
            cwd=str(cwd) if cwd else None,
            profile=_profile(),
            patches=tuple(patches),
            dsh_home=str(home),
            api_key=_api_key() or None,
            base_url=str(getattr(settings, "dsh_base_url", "") or "") or None,
            env=env,
            # 注意：SDK 的请求超时只管单次 RPC，run 的等待要靠下面的看门狗
            request_timeout_seconds=None,
        )

        # 看门狗：Session.run() 无 cancel，超时只能从外部关掉子进程逼它退出
        fired = {"timeout": False}

        def _on_timeout() -> None:
            fired["timeout"] = True
            logger.warning(
                "DSH 单轮超时，强制关闭运行时: initiative=%s stage=%s session=%s timeout=%.0fs",
                (session.meta or {}).get("initiative_id"),
                (session.meta or {}).get("stage"),
                session.id,
                timeout,
            )
            try:
                harness.close()
            except Exception:  # noqa: BLE001 — 关进程失败不影响上层判定
                logger.debug("DSH 超时关闭运行时失败", exc_info=True)

        timer = threading.Timer(timeout, _on_timeout)
        timer.daemon = True
        _notify = _make_notifier(on_activity, loop)

        native_id = session.native_id
        try:
            timer.start()
            try:
                if native_id:
                    dsh_session = harness.resume_session(native_id)
                else:
                    dsh_session = harness.start_session(_native_session_id(session))
            except Exception as exc:  # noqa: BLE001 — 会话丢失/被占用时回退新会话
                logger.warning(
                    "DSH 恢复会话失败，回退新会话: session=%s native=%s error=%s",
                    session.id,
                    native_id,
                    type(exc).__name__,
                )
                dsh_session = harness.start_session(
                    f"{_native_session_id(session)}-{uuid.uuid4().hex[:6]}"
                )
                native_id = ""
            result = dsh_session.run(prompt, on_notification=_notify)
        except Exception as exc:  # noqa: BLE001 — 进程被杀/协议断开都在这里收敛
            if fired["timeout"]:
                return HarnessReply(
                    status="fail",
                    text=f"DSH 单轮超时（{timeout:.0f}s）已终止",
                    session_id=session.id,
                    native_id=native_id or "",
                    meta={**patch_meta, "timeout": True, "error_type": type(exc).__name__},
                )
            raise
        finally:
            timer.cancel()
            try:
                harness.close()
            except Exception:  # noqa: BLE001 — 收尾失败只记日志
                logger.debug("DSH 关闭运行时失败", exc_info=True)

        events = list(getattr(result, "events", None) or [])
        text = str(getattr(result, "final_response", "") or "").strip()
        native_id = str(getattr(result, "session_id", "") or native_id or "")
        finish = str(getattr(result, "finish_reason", "") or "")
        error = _error_of(events)
        meta: dict[str, Any] = {
            "provider": self.name,
            "finish_reason": finish,
            # 下游只读 meta["context"] 的压缩信息；DSH 不提供 token 口径，留空
            "context": {},
            **patch_meta,
        }
        if error is not None:
            meta["error"] = str(error.get("message") or "")[:300]
            meta["error_code"] = str(error.get("code") or "")
            meta["error_status"] = error.get("status")
            if _is_auth_error(error):
                # 凭据问题不是阶段失败：交给图转 HITL（auth_block）等人工处理
                return HarnessReply(
                    status="needs_auth",
                    text=str(error.get("message") or "") or "DSH 鉴权失败",
                    session_id=session.id,
                    native_id=native_id,
                    meta={**meta, "auth": True},
                )
            # 非鉴权类错误一律判失败：正文可能是 agent 的自述，不能当成功交付
            return HarnessReply(
                status="fail",
                text=text or str(error.get("message") or "DSH turn failed"),
                session_id=session.id,
                native_id=native_id,
                meta=meta,
            )
        status = "ok" if (finish == "completed" or text) else "fail"
        return HarnessReply(
            status=status,
            text=text,
            session_id=session.id,
            native_id=native_id,
            meta=meta,
        )

    async def close(self, session: HarnessSession) -> None:
        """无状态 provider：进程已在 send 内关闭，这里保持幂等空实现。"""


def _make_notifier(on_activity: Any, loop: Any) -> Any:
    """构造本轮的通知回调：把 tool/call 转成 on_activity（在 worker 线程内执行）。

    ``on_activity`` 由执行器提供且是协程函数，必须投递回事件循环执行；节流放在
    这里，避免为一个被丢弃的进度多造一个协程。状态按回调实例隔离，不跨轮共享。
    """
    last = {"at": 0.0}

    def _notify(notification: Any) -> None:
        if on_activity is None:
            return
        if str(getattr(notification, "method", "")) != "session.event":
            return
        payload = getattr(notification, "payload", None) or {}
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict) or event.get("type") != "tool/call":
            return
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        tool = str(data.get("name") or "").strip()
        if not tool:
            return
        now = time.monotonic()
        if now - last["at"] < _ACTIVITY_MIN_INTERVAL:
            return
        last["at"] = now
        detail = _activity_detail(_parse_arguments(data.get("arguments")))
        try:
            emitted = on_activity(tool, detail)
            if inspect.isawaitable(emitted):
                asyncio.run_coroutine_threadsafe(_await_quietly(emitted), loop)
        except Exception:  # noqa: BLE001 — 进度上报失败不能影响回合
            logger.debug("DSH 阶段活动上报失败: tool=%s", tool, exc_info=True)

    return _notify


async def _await_quietly(awaitable: Any) -> None:
    try:
        await awaitable
    except Exception:  # noqa: BLE001
        logger.debug("DSH 活动回调执行失败", exc_info=True)


def _llm_provider() -> str:
    from sortie_deck.settings import settings

    return str(getattr(settings, "dsh_llm_provider", "") or "deepseek-official").strip()


def _model() -> str:
    from sortie_deck.settings import settings

    return str(getattr(settings, "dsh_model", "") or "deepseek-v4-flash").strip()


def _profile() -> str:
    from sortie_deck.settings import settings

    return str(getattr(settings, "dsh_profile", "") or "sdk").strip()
