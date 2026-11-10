"""阶段权限模式的统一解析与执行环境自检。

背景：CLI 路径（``plugins/coding.py``）与 SDK 路径（``harness_providers/claude_code.py``）
曾各自决定 ``permission_mode``，导致 HITL 注入的
``[sortie:coding_permission_mode=bypassPermissions]`` 只在旧 CLI 生效，SDK 阶段
仍以 ``acceptEdits`` 重跑并再次被审批阻断（任务 ``ini_88465249e006`` 空转近 3 小时）。
本模块把解析收口到一处，两条路径共用。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

logger = logging.getLogger(__name__)

# Claude Code 支持的权限模式（与 settings.coding_permission_mode 的取值一致）
ALLOWED_PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
)

_PERMISSION_MARKERS = (
    "[sortie:coding_permission_mode=",
    "[rally:coding_permission_mode=",  # legacy
)


def parse_permission_override(instruction: str | None) -> str | None:
    """从人工指令里解析任务级提权标记；无标记或非法值返回 ``None``。

    示例：``[sortie:coding_permission_mode=bypassPermissions]`` → ``bypassPermissions``。
    只接受白名单内的值，避免门禁注入任意字符串传给 SDK。
    """
    text = instruction or ""
    for marker in _PERMISSION_MARKERS:
        if marker not in text:
            continue
        rest = text.split(marker, 1)[1]
        mode = rest.split("]", 1)[0].strip()
        if mode in ALLOWED_PERMISSION_MODES:
            return mode
    return None


def resolve_permission_mode(
    *,
    permissions: dict[str, Any] | None = None,
    human_instruction: str | None = None,
    default: str = "acceptEdits",
) -> str:
    """按「任务级 override > 全局默认」解析权限模式，非法值一律回落默认值。

    ``permissions`` 来自 ``StageContext.permissions``，可含 ``coding_permission_mode``；
    ``human_instruction`` 是 HITL 指令文本，可含提权标记；``default`` 传
    ``settings.coding_permission_mode``。CLI 与 SDK 两条执行路径都必须调用本函数。
    """
    override = (permissions or {}).get("coding_permission_mode")
    if isinstance(override, str) and override.strip() in ALLOWED_PERMISSION_MODES:
        return override.strip()
    tagged = parse_permission_override(human_instruction)
    if tagged:
        return tagged
    fallback = (default or "").strip()
    return fallback if fallback in ALLOWED_PERMISSION_MODES else "acceptEdits"


# harness 无关的公共沙箱档位：claude 用 permission_mode，dsh / codex 用这三档
SANDBOX_MODES = frozenset({"read_only", "workspace_write", "full_access"})

_PERMISSION_TO_SANDBOX = {
    "acceptEdits": "workspace_write",
    "auto": "workspace_write",
    "default": "read_only",
    "plan": "read_only",
    "dontAsk": "read_only",
    "bypassPermissions": "full_access",
}


def resolve_sandbox_mode(
    *,
    permissions: dict[str, Any] | None = None,
    human_instruction: str | None = None,
    default: str = "workspace_write",
) -> str:
    """解析 harness 无关的公共沙箱档位：``read_only`` / ``workspace_write`` / ``full_access``。

    显式 ``permissions["sandbox_mode"]`` 优先；否则把 Claude 的 ``permission_mode``
    映射成公共档位。dsh / codex 直接消费该值，claude_code 仍用自己的 permission_mode。
    """
    explicit = (permissions or {}).get("sandbox_mode")
    if isinstance(explicit, str) and explicit.strip() in SANDBOX_MODES:
        return explicit.strip()
    mode = resolve_permission_mode(permissions=permissions, human_instruction=human_instruction)
    mapped = _PERMISSION_TO_SANDBOX.get(mode)
    if mapped:
        return mapped
    fallback = (default or "").strip()
    return fallback if fallback in SANDBOX_MODES else "workspace_write"


def configured_deny_rules(raw: str | None) -> list[str]:
    """把逗号分隔的 deny 规则配置解析成列表；默认空表示不改变既有工具策略。

    Claude Agent SDK 的 deny 规则在 ``bypassPermissions`` 下仍然生效，运维可据此
    在放行 Bash 的同时挡住危险命令（如 ``Bash(rm -rf /*)``）。留空是为了不
    改变既有阶段的工具策略，也不影响未显式配置的环境。
    """
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


async def permission_preflight(command: list[str] | None = None) -> bool:
    """验证当前环境能否执行解释器命令；失败只告警，不阻断任务启动。

    遵循 SWE-bench PR #594 的教训：预检不用于判定任务成败、也不跳过阶段，
    真正的失败归因由阶段产物（``selftest_log.json`` / ``test_report.json``）事后给出。
    返回 ``True`` 表示命令可执行。
    """
    cmd = command or ["node", "-e", "process.exit(0)"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except (OSError, TimeoutError, RuntimeError) as exc:
        # RuntimeError：单测/嵌入式场景下事件循环已关闭，预检同样只告警不抛错
        logger.warning("权限预检无法执行: cmd=%s error=%s", cmd[0], type(exc).__name__)
        return False
    if proc.returncode != 0:
        logger.warning(
            "权限预检失败（命令被拒绝或不可用）: cmd=%s rc=%s stderr=%s",
            cmd[0],
            proc.returncode,
            (stderr or b"").decode(errors="replace")[:200],
        )
        return False
    return True


# Bash 沙箱（Claude Code sandbox）依赖的二进制；缺失时沙箱无法启用
SANDBOX_BINARIES = ("bwrap", "socat")


def sandbox_preflight() -> dict[str, Any]:
    """检查 Bash 沙箱依赖是否在 PATH，返回 ``{"available": bool, "missing": [...]}``。

    只检查镜像是否装齐，不检查 namespace 权限（那由容器的 seccomp/AppArmor/
    SYS_ADMIN 决定）。缺失只告警，不阻断阶段，真正的失败由阶段归因兜住。
    """
    missing = [name for name in SANDBOX_BINARIES if shutil.which(name) is None]
    return {"available": not missing, "missing": missing}


# 进程级缓存：worker 启动后第一个阶段跑一次即可，避免每阶段都起子进程
_preflight_state: bool | None = None


async def maybe_permission_preflight(
    *, enabled: bool, mode: str, initiative_id: str, stage: str
) -> None:
    """每个进程只自检一次；失败只告警，不阻断阶段执行。

    ``enabled`` 来自 ``settings.permission_preflight``；``mode`` 是实际生效的
    权限模式。预检不判定任务成败，只用于让漏配环境在第一条日志里就暴露。
    """
    global _preflight_state  # 进程级一次性开关，阶段并发最多多跑一次预检，无需引入锁
    if _preflight_state is not None or not enabled:
        return
    _preflight_state = await permission_preflight()
    if not _preflight_state:
        logger.warning(
            "权限预检未通过，阶段可能因命令审批阻断: initiative=%s stage=%s mode=%s",
            initiative_id,
            stage,
            mode,
        )
    sandbox = sandbox_preflight()
    if not sandbox["available"]:
        logger.warning(
            "Bash 沙箱依赖缺失，沙箱将无法启用: initiative=%s stage=%s missing=%s",
            initiative_id,
            stage,
            sandbox["missing"],
        )
