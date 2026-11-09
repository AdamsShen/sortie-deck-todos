"""入站事件路由：去重 → 身份解析 → 命令分发。

**范式**：IM 是操作终端，不是某个任务房间的镜像。

```text
IM 消息 → (provider, sender_id) → ImIdentityStore → 用户记录 → Principal
        → im/commands.py → MissionService（与 CLI / MCP 同一套权限）
```

四个必须守住的点：

1. **平台窗口内 ACK**：飞书 3 秒、钉钉 1.5 秒、企微 5 秒；执行命令可能几十秒，
   所以回调里只 ``schedule()``，处理放后台。
2. **重推去重**：按 ``(provider, raw_id)`` 落库；卡片动作另有 ``decision_id``
   幂等（见 :mod:`sortie_deck.decisions`），双保险。
3. **未绑定身份不能操作**：只允许 ``/bind``，避免"随便谁都能建任务"。
4. **群里按发言人各自权限**：每次都从用户存储重新加载角色/状态，不用映射表
   里缓存的旧身份，权限变更立即生效。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sortie_deck.im.base import InboundEvent
from sortie_deck.im.commands import CommandResult, ImCommands, parse_command
from sortie_deck.im.connection import ImConnectionStore
from sortie_deck.im.identity import ImIdentityStore, LinkCodeService, parse_bind_command
from sortie_deck.im.intent import (
    CONFIRM_REQUIRED,
    Intent,
    IntentParser,
    describe_blocked,
    parse_reference,
)

logger = logging.getLogger(__name__)

DEDUPE_RETENTION_SECONDS = 86400.0

_ACTION_TO_HITL = {
    "approve": "approve",
    "reject": "reject",
    "rewrite": "edit_instruction",
    "stop": "stop",
}

@dataclass
class _SessionContext:
    """一个 (渠道, 会话, 用户) 上一轮列表的展示顺序。"""

    ids: list[str] = field(default_factory=list)
    updated_at: float = 0.0


NO_INTENT_HINT = (
    "没太听懂。可以直接说「有哪些任务」「运行中的任务」这类说法，\n"
    "也可以发送 /help 查看全部命令。"
)

UNBOUND_HINT = (
    "这个 IM 账号还没绑定工作台账号。\n"
    "请在工作台「设置 → IM 集成 → 身份绑定」生成 6 位绑定码，"
    "然后发送：/bind <绑定码>"
)


class ImDedupe:
    """入站事件去重表（sqlite，多进程共享）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS im_events (
                provider TEXT NOT NULL,
                raw_id TEXT NOT NULL,
                seen_at REAL NOT NULL,
                PRIMARY KEY (provider, raw_id)
            )
            """
        )
        self._conn.commit()

    def claim(self, provider: str, raw_id: str) -> bool:
        """首次出现返回 True；已处理过返回 False（调用方直接 ACK）。"""
        if not raw_id:
            # 没有平台消息 id 的 payload 无法去重，宁可放行也不要吞消息
            return True
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO im_events (provider, raw_id, seen_at) VALUES (?, ?, ?)",
                (provider, raw_id, now),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def prune(self, retention_seconds: float = DEDUPE_RETENTION_SECONDS) -> int:
        cutoff = time.time() - retention_seconds
        with self._lock:
            cur = self._conn.execute("DELETE FROM im_events WHERE seen_at < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount


class ImDispatchResult(BaseModel):
    accepted: bool = False
    duplicate: bool = False
    reason: str = ""
    initiative_id: str = ""
    message_id: str = ""


class ImRouter:
    """把归一化事件落到编排层。不碰平台协议（那是 adapter 的事）。"""

    def __init__(
        self,
        *,
        store: ImConnectionStore,
        dedupe: ImDedupe,
        identity: ImIdentityStore | None = None,
        link_codes: LinkCodeService | None = None,
        commands: ImCommands | None = None,
        intent: IntentParser | None = None,
        orchestrator_factory: Callable[[], Any] | None = None,
        background: Callable[[Awaitable[Any]], Any] | None = None,
        reply: Callable[[str, str, str], Awaitable[Any]] | None = None,
    ) -> None:
        self.store = store
        self.dedupe = dedupe
        self.identity = identity
        self.link_codes = link_codes
        self.commands = commands or ImCommands()
        # 自然语言层：用户不会先记命令再说话
        self.intent = intent if intent is not None else IntentParser()
        # 「第一个」必须落到具体任务：按 (provider, channel, sender) 隔离，
        # 群里别人的列表不能串到你身上。
        self._contexts: dict[str, _SessionContext] = {}
        self.context_ttl = 1800.0
        self._orchestrator_factory = orchestrator_factory
        self._background = background or self._default_background
        self._reply = reply
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def _default_background(awaitable: Awaitable[Any]) -> Any:
        return asyncio.create_task(awaitable)  # type: ignore[arg-type]

    def _orchestrator(self) -> Any:
        if self._orchestrator_factory is not None:
            return self._orchestrator_factory()
        from sortie_deck.orchestrator import get_orchestrator

        return get_orchestrator()

    # --- 对外 -------------------------------------------------------------

    def schedule(self, event: InboundEvent) -> None:
        """立即返回（用于回调 ACK），处理放后台。"""
        task = self._background(self._guarded_dispatch(event))
        if isinstance(task, asyncio.Task):
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _guarded_dispatch(self, event: InboundEvent) -> None:
        try:
            result = await self.dispatch(event)
            if not result.accepted and not result.duplicate:
                logger.info(
                    "IM 入站未处理 provider=%s channel=%s reason=%s",
                    event.provider,
                    event.channel_id,
                    result.reason,
                )
        except Exception:  # noqa: BLE001 — 后台异常必须记录，不能静默消失
            logger.exception(
                "IM 入站处理失败 provider=%s channel=%s raw_id=%s",
                event.provider,
                event.channel_id,
                event.raw_id,
            )

    async def dispatch(self, event: InboundEvent) -> ImDispatchResult:
        """同步处理一条事件（测试与后台任务共用）。"""
        if not self.dedupe.claim(event.provider, event.raw_id):
            return ImDispatchResult(duplicate=True, reason="duplicate event")

        conn = self.store.get_by_provider(event.provider)
        if conn is not None:
            self.store.touch_event(conn.id)

        # /bind 是唯一不要求已绑定身份的命令——它正是用来建立身份的
        if not event.is_action:
            code = parse_bind_command(event.text)
            if code is not None:
                return await self._dispatch_bind(event, code)

        principal = self._principal_for(event)
        if principal is None:
            await self._send_reply(event, UNBOUND_HINT)
            return ImDispatchResult(reason="identity not bound")

        # 门禁卡片按钮（带 decision_id）走决策中心：幂等 + assignee 鉴权
        if event.is_action:
            return await self._dispatch_action(event, principal)

        parsed = parse_command(event.text)
        if parsed is None:
            return await self._dispatch_natural(event, principal)

        name, args = parsed
        result = await self.commands.handle(name, args, principal=principal)
        self._remember(event, result)
        if result.text:
            await self._send_reply(event, result.text)
        return ImDispatchResult(
            accepted=result.ok,
            reason=(result.text or "")[:160],
            initiative_id=result.mission_id,
        )

    async def _dispatch_natural(
        self, event: InboundEvent, principal: Any
    ) -> ImDispatchResult:
        """非命令文本：先规则、再模型。

        只读命令与 ``new`` 直接执行；``approve`` / ``reject`` / ``stop`` /
        ``confirm`` 这些状态变更必须显式命令，否则群里一句「可以」就能放行门禁。
        """
        intent = await self.intent.understand(event.text)
        if intent is None:
            await self._send_reply(event, NO_INTENT_HINT)
            return ImDispatchResult(reason="intent not understood")

        if not intent.command:
            # 模型判定为闲聊：把它的解释回过去，不要让用户撞墙
            await self._send_reply(event, intent.reply or NO_INTENT_HINT)
            return ImDispatchResult(accepted=True, reason="answered")

        # 「看一下第一个任务详情」：位置指代 → 具体 ID
        if intent.command in {"status", "artifacts"} and not intent.args:
            resolved = await self._apply_reference(event, principal, intent)
            if resolved is not None:
                self._remember(event, resolved)
                if resolved.text:
                    await self._send_reply(event, resolved.text)
                return ImDispatchResult(
                    accepted=resolved.ok,
                    reason=(resolved.text or "")[:160],
                    initiative_id=resolved.mission_id,
                )

        if intent.command in CONFIRM_REQUIRED:
            # 补上默认目标，让提示里的命令可直接复制执行（否则用户还要自己找 ID）
            self._fill_default_target(intent, principal)
            await self._send_reply(event, describe_blocked(intent))
            return ImDispatchResult(reason=f"blocked {intent.command}")

        result = await self.commands.handle(
            intent.command, intent.args, principal=principal
        )
        self._remember(event, result)
        if result.text:
            await self._send_reply(event, result.text)
        return ImDispatchResult(
            accepted=result.ok,
            reason=(result.text or "")[:160],
            initiative_id=result.mission_id,
        )

    # --- 内部 -------------------------------------------------------------

    def _principal_for(self, event: InboundEvent) -> Any:
        """IM 身份 → Principal。

        每次从用户存储重新加载：群里发言人的角色/状态可能已变化，用映射表
        里缓存的旧身份做权限判断会留下提权窗口。
        """
        if self.identity is None:
            return None
        sender = event.sender_id
        if not sender and event.action is not None:
            sender = event.action.operator_id
        ident = self.identity.resolve(event.provider, sender)
        if ident is None:
            return None
        from sortie_deck.service import get_auth_store, principal_for_user

        user = get_auth_store().get_by_id(ident.user_id)
        if user is None or getattr(user, "status", "active") != "active":
            return None
        return principal_for_user(user)

    async def _dispatch_bind(self, event: InboundEvent, code: str) -> ImDispatchResult:
        """兑换绑定码。短码由工作台生成（身份可信），IM 侧只负责兑换。"""
        if self.identity is None or self.link_codes is None:
            return ImDispatchResult(reason="身份绑定未启用")
        if not code:
            await self._send_reply(
                event, "用法：/bind <6 位绑定码>（在 设置 → IM 集成 生成）"
            )
            return ImDispatchResult(reason="missing link code")
        redeemed = self.link_codes.redeem(code)
        if redeemed is None:
            await self._send_reply(event, "绑定码无效或已过期，请到工作台重新生成")
            return ImDispatchResult(reason="invalid link code")
        user_id, username = redeemed
        self.identity.bind(
            provider=event.provider,
            sender_id=event.sender_id,
            user_id=user_id,
            username=username,
        )
        await self._send_reply(event, f"已绑定为 {username}。发送 /help 查看可用命令。")
        return ImDispatchResult(accepted=True, reason=f"bound to {username}")

    async def _dispatch_action(self, event: InboundEvent, principal: Any) -> ImDispatchResult:
        action = event.action
        assert action is not None  # 由 is_action 保证
        if not action.valid():
            return ImDispatchResult(reason=f"未知卡片动作: {action.action}")
        if not action.decision_id:
            return ImDispatchResult(reason="卡片动作缺少 decision_id，无法幂等处理")

        try:
            outcome = await self._orchestrator().resolve_decision(
                action.decision_id,
                answer=action.instruction or action.action,
                by=principal.display_name,
                channel=f"im:{event.provider}",
                action=_ACTION_TO_HITL[action.action],
            )
        except KeyError:
            return ImDispatchResult(reason=f"决策不存在: {action.decision_id}")
        except Exception as exc:  # noqa: BLE001
            return ImDispatchResult(reason=f"处理卡片动作失败: {exc}")

        accepted = bool(getattr(outcome, "accepted", False))
        duplicate = bool(getattr(outcome, "duplicate", False))
        if not accepted and not duplicate:
            return ImDispatchResult(
                reason=getattr(outcome, "reason", "") or "动作未被接受（权限或状态不符）"
            )
        return ImDispatchResult(
            accepted=accepted, duplicate=duplicate, message_id=action.decision_id
        )

    @staticmethod
    def _scope(event: InboundEvent) -> str:
        author = getattr(event, "author_id", "") or getattr(event, "author_name", "")
        return f"{event.provider}:{event.channel_id}:{author}"

    def _remember(self, event: InboundEvent, result: CommandResult) -> None:
        """列表类命令执行后记住展示顺序。"""
        if not result.mission_ids:
            return
        self._contexts[self._scope(event)] = _SessionContext(
            ids=list(result.mission_ids), updated_at=time.time()
        )
        if len(self._contexts) > 200:
            cutoff = time.time() - self.context_ttl
            for key in [k for k, v in self._contexts.items() if v.updated_at < cutoff]:
                self._contexts.pop(key, None)

    async def _apply_reference(
        self, event: InboundEvent, principal: Any, intent: Intent
    ) -> CommandResult | None:
        """把「第一个」「它」换成具体任务 ID 再执行。

        没有上一轮列表时按"最近任务"兜底——IM 里说"第一个"几乎总是指列表第一
        条，比直接回"我不知道你在说哪个"更接近用户预期。
        """
        ref = parse_reference(event.text)
        if ref is None:
            return None

        ctx = self._contexts.get(self._scope(event))
        ids: list[str] = []
        if ctx and time.time() - ctx.updated_at < self.context_ttl:
            ids = list(ctx.ids)
        if not ids:
            try:
                ids = [i.id for i in self.commands.service.list_mine(principal, limit=10)]
            except Exception:  # noqa: BLE001 — 取不到列表就退回通用提示
                return None
        if not ids:
            return CommandResult(ok=True, text="你还没有任务。")

        index = ref.index if ref.index >= 0 else len(ids) + ref.index
        if index < 0 or index >= len(ids):
            return CommandResult(
                ok=False, text=f"只有 {len(ids)} 个任务，没有第 {index + 1} 个。"
            )
        return await self.commands.handle(
            intent.command, [ids[index]], principal=principal
        )

    def _fill_default_target(self, intent: Any, principal: Any) -> None:
        """写操作的提示里带上具体任务 ID。

        只补"最近一个"，不猜——猜错任务比不给更糟。
        """
        if intent.args:
            return
        status = "running" if intent.command == "stop" else "waiting_hitl"
        try:
            rows = self.commands.service.list_mine(principal, status=status)
        except Exception:  # noqa: BLE001 — 补不上就退回不带 ID 的通用提示
            return
        if rows:
            intent.args = [rows[0].id]

    async def _send_reply(self, event: InboundEvent, text: str) -> None:
        if self._reply is None or not text:
            return
        try:
            await self._reply(event.provider, event.channel_id, text)
        except Exception as exc:  # noqa: BLE001 — 回执失败不影响命令结果
            logger.warning("IM 回执发送失败 provider=%s: %s", event.provider, exc)


_ROUTER: ImRouter | None = None
_LOCK = threading.Lock()


def get_im_router() -> ImRouter | None:
    return _ROUTER


def set_im_router(router: ImRouter | None) -> None:
    global _ROUTER
    with _LOCK:
        _ROUTER = router
