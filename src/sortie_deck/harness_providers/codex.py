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
import re
import shutil
from pathlib import Path
from typing import Any

from sortie_deck.harness_providers.base import HarnessSession
from sortie_deck.models import utc_now

logger = logging.getLogger(__name__)

# 鉴权失败的关键词：Codex CLI 在未登录/凭据过期时走的是文本报错而不是结构化事件
_AUTH_HINTS = ("not logged in", "codex login", "401 unauthorized", "unauthorized")

# codex 的项目级 agent 目录（相对 cwd）；个人目录在 $CODEX_HOME/agents。
# 写项目级而不是 $CODEX_HOME：后者是宿主机全局状态，会污染其它任务。
_SUBAGENT_DIR = Path(".codex") / "agents"

# codex 的 agent_name 只接受小写字母/数字/下划线
_AGENT_ID_RE = re.compile(r"[^a-z0-9]+")


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


def _codex_agent_name(raw: str) -> str:
    """把子代理 id 收敛成 codex 可接受的 agent_name（``[a-z0-9_]``）。

    实测（codex-cli 0.162.1）：name 带连字符时 spawn 直接报
    ``agent_name must use only lowercase letters, digits, and underscores``，
    模板里的 ``web-researcher`` 必须落成 ``web_researcher``。
    """
    slug = _AGENT_ID_RE.sub("_", str(raw or "").strip().lower()).strip("_")
    return slug or "subagent"


def _toml_str(value: str) -> str:
    """TOML 基本字符串转义：反斜杠、双引号、换行/制表符。"""
    text = str(value or "")
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return (
        text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    )


def _subagent_toml(spec: dict[str, Any]) -> str:
    """canonical spec → codex agent 文件内容（name/description/instructions）。"""
    name = _codex_agent_name(str(spec.get("id") or ""))
    description = str(spec.get("description") or "").strip() or name
    instructions = str(spec.get("prompt") or description).strip()
    if str(spec.get("mode") or "readonly") == "readonly":
        # codex 的自定义 agent 无法按工具裁剪（官方只支持继承父级），
        # 只读约束只能写进指令，并在观测里标注"无法硬保证"
        instructions += "\n只读约束：禁止创建、修改、删除任何文件；需要变更时只报告建议。"
    return (
        f'name = "{_toml_str(name)}"\n'
        f'description = "{_toml_str(description)}"\n'
        f'developer_instructions = "{_toml_str(instructions)}"\n'
    )


def ensure_subagent_files(subagents: dict[str, Any], cwd: str | Path) -> tuple[str, list[str]]:
    """把子代理池写成 ``<cwd>/.codex/agents/*.toml``（codex 项目级发现）。

    返回 ``(support, notes)``：``native`` 表示文件已就位（含内容未变），
    ``prompt_only`` 表示写盘失败，只能靠提示词引导。
    幂等：内容相同不重写，避免每轮刷新 mtime 被误当成产出改动。
    """
    rows = [row for row in ((subagents or {}).get("agents") or []) if isinstance(row, dict)]
    notes: list[str] = []
    if not rows:
        return "unsupported", notes
    root = Path(str(cwd or "") or ".") / _SUBAGENT_DIR
    try:
        root.mkdir(parents=True, exist_ok=True)
        # 让 agent 自己的 git 操作看不到这些编排层文件：避免被 `git add -A` 提交进业务仓库
        gitignore = root.parent / ".gitignore"
        if not gitignore.is_file():
            gitignore.write_text("agents/\n", encoding="utf-8")
        for row in rows:
            text = _subagent_toml(row)
            path = root / f"{_codex_agent_name(str(row.get('id') or ''))}.toml"
            if path.is_file():
                try:
                    if path.read_text(encoding="utf-8", errors="replace") == text:
                        continue
                except OSError:
                    pass
            path.write_text(text, encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 — 写不了就退化为提示词引导
        notes.append(f"子代理定义写盘失败（{exc.__class__.__name__}），退化为提示词引导")
        return "prompt_only", notes
    return "native", notes


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

        # 子代理池：首轮把定义落到项目级 .codex/agents（后续轮次复用同一 workspace）
        subagent_support = ""
        subagents = (context_pack or {}).get("subagents") or {}
        if subagents.get("agents") and not (session.meta or {}).get("subagents_materialized"):
            subagent_support, notes = ensure_subagent_files(subagents, cwd or ".")
            session.meta["subagents_materialized"] = subagent_support
            session.meta["subagents_notes"] = notes
            if subagent_support == "native":
                logger.info(
                    "codex 子代理定义已就位: session=%s agents=%s",
                    session.id,
                    ",".join(_codex_agent_name(str(r.get("id") or "")) for r in subagents["agents"]),
                )
            else:
                logger.warning(
                    "codex 子代理定义未生效(%s): %s", subagent_support, "；".join(notes) or "-"
                )
        else:
            subagent_support = str((session.meta or {}).get("subagents_materialized") or "")

        if session.native_id:
            # 续跑沿用首次会话的沙箱档位（CLI 不接受在 resume 上改沙箱）
            args = [binary, "exec", "resume", session.native_id, "--json", "--skip-git-repo-check"]
        else:
            # 首轮按任务级公共档位定档；额外可写目录同样只在首轮生效
            args = _first_turn_args(binary, settings, context_pack, extra_dirs)
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
        meta: dict[str, Any] = {
            "provider": self.name,
            "exit": proc.returncode or 0,
            "usage": usage,
        }
        if subagent_support:
            meta["subagents_support"] = subagent_support
        collab = _collab_stats(stdout)
        if collab:
            meta["context"] = {"subagents": collab}
        return HarnessReply(
            status=status,
            text=text,
            session_id=session.id,
            native_id=native_id or session.native_id,
            meta=meta,
        )

    async def close(self, session: HarnessSession) -> None:
        return None


_SANDBOX_TO_CODEX = {
    "read_only": "read-only",
    "workspace_write": "workspace-write",
    "full_access": "danger-full-access",
}


def _sandbox_flag(context_pack: dict[str, Any] | None, fallback: str) -> str:
    """公共档位优先，否则回落 ``TDT_CODEX_SANDBOX``；非法值统一收敛到 workspace-write。"""
    mode = str(((context_pack or {}).get("sandbox") or {}).get("mode") or "").strip()
    mapped = _SANDBOX_TO_CODEX.get(mode)
    if mapped:
        return mapped
    value = (fallback or "").strip()
    return (
        value
        if value in {"read-only", "workspace-write", "danger-full-access"}
        else "workspace-write"
    )


def _first_turn_args(
    binary: str,
    settings: Any,
    context_pack: dict[str, Any] | None,
    extra_dirs: list[str] | None,
) -> list[str]:
    """首轮参数：沙箱档位、审批策略、网络开关、额外可写目录。

    ``codex exec`` 是非交互命令；不显式给 ``--ask-for-approval never`` 时，
    需要审批的动作会被拒（与 Claude 的 acceptEdits 是同一类问题）。
    """
    sandbox = _sandbox_flag(context_pack, getattr(settings, "codex_sandbox", ""))
    args = [binary, "exec", "--json", "--skip-git-repo-check", "--sandbox", sandbox]
    approval = str(getattr(settings, "codex_ask_for_approval", "") or "").strip()
    if approval:
        args += ["--ask-for-approval", approval]
    if bool(getattr(settings, "codex_network_access", False)):
        # workspace-write 默认断网；装依赖需要联网（域名级收紧见 network_proxy）
        args += ["-c", "sandbox_workspace_write.network_access=true"]
    for extra in extra_dirs or []:
        if str(extra or "").strip():
            args += ["--add-dir", str(extra)]
    return args


def _collab_stats(stdout: str) -> dict[str, Any]:
    """统计 codex 的协作工具调用，作为子代理观测。

    实测（0.162.1）：``spawn_agent`` 本身不出现在 ``--json`` 流里，可见的只有
    ``item.* collab_tool_call``（tool=wait/send_message...，且 ``receiver_thread_ids``
    常为空）。因此只能给出"委派活动次数 + 用到的协作工具"，**拿不到被委派 agent
    的名字**——这个限制必须在指标里显式保留，不能假装有完整归因。
    """
    seen: set[str] = set()
    tools: list[str] = []
    calls = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict) or item.get("type") != "collab_tool_call":
            continue
        # started/completed 是同一次调用，按 item id 去重
        item_id = str(item.get("id") or "")
        if item_id and item_id in seen:
            continue
        if item_id:
            seen.add(item_id)
        calls += 1
        tool = str(item.get("tool") or "").strip()
        if tool and tool not in tools:
            tools.append(tool)
    if not calls:
        return {}
    return {"calls": calls, "tools": tools, "source": "collab_tool_call"}


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
