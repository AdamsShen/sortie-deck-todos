"""IM 接入的统一内部模型。

各平台（飞书 / 钉钉 / 企微 / 微信 / Slack）的原始回调差异很大，但这四件事
在业务层必须一致，否则每加一个平台就要改一遍编排逻辑：

* :class:`InboundEvent` —— 入站的「一句话」或「一次按钮点击」；
* :class:`ApprovalCard` —— 出站的审批卡片（唯一渲染源，各平台各自翻译）；
* :class:`CardAction`  —— 卡片按钮携带的动作，幂等键是 ``decision_id``；
* :class:`ImProvider`   —— 适配器协议，新增平台只实现它。

设计约束来自平台侧（详见 docs/plans/2026-11-09-im-channel-integration.md）：
飞书/钉钉回调要求 3 秒内响应，企微要求 5 秒内更新卡片。因此适配器只负责
「解析 + ACK」，真正的业务处理由 :mod:`sortie_deck.im.router` 丢到后台任务。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免循环导入
    from sortie_deck.im.connection import ImConnection


# 卡片按钮的合法动作，与 HitlAction 对齐（rewrite = edit_instruction）
CARD_ACTIONS: tuple[str, ...] = ("approve", "reject", "rewrite", "stop")


class CardAction(BaseModel):
    """一次卡片点击。

    ``decision_id`` 是幂等键：平台不保证回调只来一次（钉钉按 eventId 重推、
    飞书按钮可能被连点），只有它能保证同一次审批不会 resume 两遍。
    """

    action: str
    decision_id: str = ""
    instruction: str = ""
    operator_id: str = ""
    operator_name: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    def valid(self) -> bool:
        return self.action in CARD_ACTIONS


class InboundEvent(BaseModel):
    """归一化后的入站事件：要么是一句文本，要么是一次卡片动作。"""

    provider: str
    channel_id: str
    chat_type: str = "group"  # single | group
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    action: CardAction | None = None
    # 平台消息 id（钉钉 eventId / 企微 msgid / 微信 msgid），用于入站去重
    raw_id: str = ""
    received_at: float = Field(default_factory=time.time)

    @property
    def is_action(self) -> bool:
        return self.action is not None


class CardButton(BaseModel):
    key: str  # 与 CardAction.action 同名
    text: str
    style: str = "default"  # default | primary | danger


class ApprovalCard(BaseModel):
    """审批卡片的平台无关描述。

    三平台渲染时只读这个对象：飞书 → interactive 卡片 JSON；
    钉钉 → ActionCard；企微 → template_card（button_interaction）。
    """

    title: str
    stage: str
    mission_id: str
    decision_id: str
    summary: str = ""
    buttons: list[CardButton] = Field(default_factory=list)
    # 卡片过期（飞书 14 天不可更新 / 30 天不可交互）后的降级入口
    fallback_url: str = ""
    # pending（待处理）| approved | rejected | expired —— 已处理态用于就地更新
    status: str = "pending"
    footer: str = ""

    def to_text(self) -> str:
        """纯文本降级形态：卡片渲染失败或渠道不支持卡片时使用。"""
        lines = [f"【{self.title}】", f"阶段：{self.stage}", self.summary]
        if self.buttons:
            lines.append("可选操作：" + " / ".join(f"/{b.key}" for b in self.buttons))
        if self.fallback_url:
            lines.append(f"或到工作台处理：{self.fallback_url}")
        return "\n".join(x for x in lines if x)


class SendResult(BaseModel):
    ok: bool = False
    message_id: str = ""
    error: str = ""
    provider: str = ""


@runtime_checkable
class ImProvider(Protocol):
    """适配器协议。实现方只管协议细节，不碰任务状态。"""

    name: str

    async def probe(self, conn: ImConnection) -> tuple[bool, str]:
        """用当前凭据探活。返回 ``(ok, error)``；ok=False 时 error 要能直接给用户看。"""
        ...

    async def send_text(self, conn: ImConnection, chat_id: str, text: str) -> SendResult: ...

    async def send_card(
        self, conn: ImConnection, chat_id: str, card: ApprovalCard
    ) -> SendResult: ...

    async def update_card(
        self, conn: ImConnection, chat_id: str, message_id: str, card: ApprovalCard
    ) -> bool:
        """就地更新卡片为已处理态。不支持就地更新的平台返回 False（调用方降级发文本）。"""
        ...

    def parse_inbound(self, payload: dict[str, Any]) -> InboundEvent | None:
        """把平台原始 payload 归一化；非消息类事件（如 URL 校验）返回 None。"""
        ...
