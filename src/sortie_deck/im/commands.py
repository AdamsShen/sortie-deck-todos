"""IM 命令层：把聊天文本映射到 Service 调用，能力与 CLI / MCP 一致。

三条设计约束：

1. **不做第二套权限**。所有操作都走 ``MissionService`` + ``Principal``，
   与 ``sortie mission`` 和 MCP 工具同源；IM 不是特权通道，也不能绕过
   ``ensure_access`` / ``ensure_control``。
2. **不要求"会话绑定任务"**。任务由命令参数指定；省略时落到「我最近一个
   等待门禁的任务」。一个 IM 会话可以操作我名下的任意任务。
3. **输出必须是纯文本**。微信不支持 markdown，反引号、表格、加粗都会原样
   显示成噪点，所以统一用短行 + 中文状态。

群里多人时按**发言人各自的权限**执行：谁发的命令就用谁的 Principal，
所以同一条命令在不同人手里能看到的任务不同。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sortie_deck.models import Initiative, InitiativeStatus
from sortie_deck.service import MissionService, Principal, ServiceError

logger = logging.getLogger(__name__)

# 命令名（不含前缀 /）
COMMANDS: tuple[str, ...] = (
    "help",
    "new",
    "confirm",
    "list",
    "running",
    "status",
    "approve",
    "reject",
    "stop",
    "artifacts",
)

_STATUS_CN: dict[str, str] = {
    "draft": "草稿",
    "running": "运行中",
    "waiting_hitl": "待审批",
    "stopped": "已停止",
    "failed": "失败",
    "done": "已完成",
}

HELP_TEXT = """可用命令：
/help                 查看帮助
/new <一句话需求>      创建任务（先出航线，不自动开跑）
/confirm [任务ID]      确认并启动（省略=最近一个草稿）
/list                 我最近的 10 个任务
/running              运行中 / 待审批的任务
/status [任务ID]       任务详情（省略=最新）
/approve [任务ID] [备注]  放行门禁（省略=最近一个待审批）
/reject [任务ID] <原因>   打回
/stop [任务ID]         停止任务
/artifacts [任务ID]    产物列表
/bind <6位绑定码>      把当前 IM 账号绑定到工作台账号

任务 ID 形如 ini_abc123，也可以只写后 6 位。"""


@dataclass
class CommandResult:
    ok: bool
    text: str
    mission_id: str = ""
    # 列表类命令按展示顺序回传 ID，让「第一个」「最后一个」能落到具体任务上
    mission_ids: list[str] = field(default_factory=list)


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """解析 ``/cmd args``，兼容 ``/cmd@botName``（群聊 @ 机器人后会带后缀）。

    非命令（不以 / 开头）返回 ``None``，交回普通聊天流程。
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    first, _, rest = stripped[1:].partition(" ")
    name = first.split("@", 1)[0].strip().lower()
    if not name:
        return None
    return name, [part for part in rest.split(" ") if part]


def _status(ini: Initiative) -> str:
    value = ini.status.value if isinstance(ini.status, InitiativeStatus) else str(ini.status)
    return _STATUS_CN.get(value, value)


def _line(ini: Initiative) -> str:
    """单行任务摘要。微信是纯文本，控制在 60 字内避免折行。"""
    title = (ini.title or "").strip()
    if len(title) > 20:
        title = title[:20] + "…"
    return f"- {ini.id} [{_status(ini)}] {ini.current_stage} · {title}"


def _find_mission(service: MissionService, principal: Principal, token: str) -> str:
    """支持只写 ID 后缀（``ini_abc123`` 的 ``abc123``），减少 IM 里打字。"""
    if token.lower().startswith("ini_"):
        return token
    for ini in service.list_mine(principal):
        if ini.id.lower().endswith(token.lower()):
            return ini.id
    return token


def _pick_mission(
    service: MissionService,
    principal: Principal,
    args: list[str],
    *,
    fallback_status: str = "waiting_hitl",
    fallback_hint: str = "没有指定任务，且你名下当前没有待审批的任务",
) -> tuple[str, list[str]]:
    """取任务 ID 与剩余参数；未指定时按状态回退到最近一个。

    返回 ``(mission_id, rest_args)``，方便调用方把剩余参数当备注/原因。
    """
    if args and args[0].lower().startswith("ini_"):
        return _find_mission(service, principal, args[0]), args[1:]
    if args and _looks_like_id_suffix(service, principal, args[0]):
        return _find_mission(service, principal, args[0]), args[1:]
    candidates = service.list_mine(principal, status=fallback_status)
    if candidates:
        return candidates[0].id, args
    raise ServiceError("not_found", fallback_hint)


def _looks_like_id_suffix(service: MissionService, principal: Principal, token: str) -> bool:
    if token.startswith("/"):
        return False
    return any(ini.id.lower().endswith(token.lower()) for ini in service.list_mine(principal))


class ImCommands:
    """执行 IM 命令。无状态，可安全复用。"""

    def __init__(
        self,
        *,
        service_factory: Callable[[], MissionService] | None = None,
    ) -> None:
        self._service_factory = service_factory or MissionService

    @property
    def service(self) -> MissionService:
        return self._service_factory()

    async def handle(self, name: str, args: list[str], *, principal: Principal) -> CommandResult:
        service = self.service
        try:
            if name == "help":
                return CommandResult(ok=True, text=HELP_TEXT)
            if name == "new":
                return await self._new(service, principal, args)
            if name == "confirm":
                return await self._confirm(service, principal, args)
            if name == "list":
                return self._list(service, principal)
            if name == "running":
                return self._running(service, principal)
            if name == "status":
                return self._status(service, principal, args)
            if name == "approve":
                return await self._approve(service, principal, args)
            if name == "reject":
                return await self._reject(service, principal, args)
            if name == "stop":
                return await self._stop(service, principal, args)
            if name == "artifacts":
                return self._artifacts(service, principal, args)
        except ServiceError as exc:
            # 服务层错误文案本就是给用户看的（权限 / 状态 / 不存在）
            return CommandResult(ok=False, text=exc.message)
        except Exception as exc:  # noqa: BLE001 — 一条命令失败不能打挂 IM 通道
            logger.exception("IM 命令执行失败 name=%s", name)
            return CommandResult(ok=False, text=f"命令执行失败：{type(exc).__name__}: {exc}")
        return CommandResult(ok=False, text=f"未知命令 /{name}，发送 /help 查看可用命令")

    # --- 各命令 -----------------------------------------------------------

    async def _new(
        self, service: MissionService, principal: Principal, args: list[str]
    ) -> CommandResult:
        brief = " ".join(args).strip()
        if not brief:
            return CommandResult(ok=False, text="用法：/new <一句话需求>")
        ini = await service.create_mission(principal, brief=brief)
        return CommandResult(
            ok=True,
            mission_id=ini.id,
            text=(
                f"已创建任务：\n{_line(ini)}\n\n"
                f"航线已确认，发送 /confirm {ini.id} 启动；\n"
                f"/status {ini.id} 查看详情。"
            ),
        )

    async def _confirm(
        self, service: MissionService, principal: Principal, args: list[str]
    ) -> CommandResult:
        if args:
            mission_id = _find_mission(service, principal, args[0])
        else:
            drafts = [
                i for i in service.list_mine(principal) if i.status == InitiativeStatus.DRAFT
            ]
            if not drafts:
                return CommandResult(ok=False, text="没有可启动的草稿任务，先发送 /new <需求>")
            mission_id = drafts[0].id
        with contextlib.suppress(ServiceError):
            # 已确认过的任务再 confirm 会报冲突；用户意图是「启动」，继续走 start
            await service.confirm(principal, mission_id)
        ini = await service.start(principal, mission_id)
        return CommandResult(
            ok=True,
            mission_id=ini.id,
            text=(
                f"已启动 {ini.id}，当前阶段 {ini.current_stage}。\n"
                f"发送 /status {ini.id} 跟踪进度。"
            ),
        )

    def _list(self, service: MissionService, principal: Principal) -> CommandResult:
        items = service.list_mine(principal, limit=10)
        if not items:
            return CommandResult(ok=True, text="你还没有任务，发送 /new <需求> 创建第一个。")
        return CommandResult(
            ok=True,
            text=f"你最近 {len(items)} 个任务：\n" + "\n".join(_line(i) for i in items),
            mission_ids=[i.id for i in items],
        )

    def _running(self, service: MissionService, principal: Principal) -> CommandResult:
        items = service.list_mine(principal, status="running") + service.list_mine(
            principal, status="waiting_hitl"
        )
        items.sort(key=lambda i: i.created_at, reverse=True)
        if not items:
            return CommandResult(ok=True, text="当前没有运行中或待审批的任务。")
        return CommandResult(
            ok=True,
            text=f"运行中/待审批 {len(items)} 个：\n" + "\n".join(_line(i) for i in items),
            mission_ids=[i.id for i in items],
        )

    def _status(
        self, service: MissionService, principal: Principal, args: list[str]
    ) -> CommandResult:
        if args:
            mission_id = _find_mission(service, principal, args[0])
        else:
            latest = service.latest(principal)
            if latest is None:
                return CommandResult(ok=True, text="你还没有任务。")
            mission_id = latest.id
        ini = service.get(principal, mission_id)
        lines = [
            f"任务 {ini.id}",
            f"标题：{ini.title}",
            f"状态：{_status(ini)}    阶段：{ini.current_stage}",
        ]
        if ini.status == InitiativeStatus.WAITING_HITL and ini.pending_hitl:
            lines.append(f"待审批：{ini.pending_hitl.stage}")
            prompt = (ini.pending_hitl.prompt or "").strip()
            if prompt:
                lines.append(f"说明：{prompt[:120]}")
            lines.append(f"发送 /approve {ini.id} 放行，或 /reject {ini.id} <原因>。")
        elif ini.status == InitiativeStatus.RUNNING:
            lines.append(f"/stop {ini.id} 可中止。")
        return CommandResult(ok=True, mission_id=ini.id, text="\n".join(lines))

    async def _approve(
        self, service: MissionService, principal: Principal, args: list[str]
    ) -> CommandResult:
        mission_id, rest = _pick_mission(service, principal, args)
        note = " ".join(rest).strip() or None
        ini = await service.hitl(principal, mission_id, action="approve", note=note)
        return CommandResult(
            ok=True,
            mission_id=ini.id,
            text=f"已放行 {ini.id}，当前阶段 {ini.current_stage}。",
        )

    async def _reject(
        self, service: MissionService, principal: Principal, args: list[str]
    ) -> CommandResult:
        mission_id, rest = _pick_mission(service, principal, args)
        reason = " ".join(rest).strip() or "IM 打回"
        ini = await service.hitl(
            principal, mission_id, action="reject", instruction=reason, note=reason
        )
        return CommandResult(ok=True, mission_id=ini.id, text=f"已打回 {ini.id}：{reason}")

    async def _stop(
        self, service: MissionService, principal: Principal, args: list[str]
    ) -> CommandResult:
        mission_id, _rest = _pick_mission(
            service,
            principal,
            args,
            fallback_status="running",
            fallback_hint="没有指定任务，且你名下当前没有运行中的任务",
        )
        ini = await service.stop(principal, mission_id)
        return CommandResult(ok=True, mission_id=ini.id, text=f"已停止 {ini.id}。")

    def _artifacts(
        self, service: MissionService, principal: Principal, args: list[str]
    ) -> CommandResult:
        if args:
            mission_id = _find_mission(service, principal, args[0])
        else:
            latest = service.latest(principal)
            if latest is None:
                return CommandResult(ok=True, text="你还没有任务。")
            mission_id = latest.id
        items = service.artifacts(principal, mission_id)
        if not items:
            return CommandResult(ok=True, text=f"{mission_id} 暂无产物。")
        lines = [f"{mission_id} 产物 {len(items)} 个："]
        for art in items[:20]:
            stage = getattr(art, "stage", "")
            kind = getattr(art, "kind", "")
            lines.append(f"- {stage}/{kind}")
        return CommandResult(ok=True, mission_id=mission_id, text="\n".join(lines))
