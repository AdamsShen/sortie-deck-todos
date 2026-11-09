"""IM 接入层：渠道适配、连接管理、审批卡片与入站路由。

对外入口（其它模块只应通过这里拿能力）：

* :func:`get_im_manager` / :func:`set_im_manager` —— 全局连接管理器单例；
* :class:`InboundEvent` / :class:`ApprovalCard` —— 统一模型；
* :class:`ImConnection` / :class:`ImConnectionStore` —— 接入记录。

设计说明见 ``docs/plans/2026-11-09-im-channel-integration.md``。
"""

from __future__ import annotations

import threading
from typing import Any

from sortie_deck.im.base import (
    CARD_ACTIONS,
    ApprovalCard,
    CardAction,
    CardButton,
    ImProvider,
    InboundEvent,
    SendResult,
)
from sortie_deck.im.connection import (
    MODES,
    PROVIDERS,
    STATUSES,
    ImConnection,
    ImConnectionConflict,
    ImConnectionError,
    ImConnectionManager,
    ImConnectionNotFound,
    ImConnectionStore,
    InvalidTransition,
)

_MANAGER: ImConnectionManager | None = None
_LOCK = threading.Lock()


def get_im_manager() -> ImConnectionManager | None:
    return _MANAGER


def set_im_manager(manager: ImConnectionManager | None) -> None:
    global _MANAGER
    with _LOCK:
        _MANAGER = manager


def provider_catalog() -> list[dict[str, Any]]:
    """渠道能力表，供管理页渲染卡片与表单。

    与 ``_ALLOWED_MODES`` 保持一致：微信只能扫码；其余可选长连接或回调。
    这里显式写出字段，是为了让前端不必硬编码"飞书要 App ID"这类知识。
    """
    return [
        {
            "provider": "wechat",
            "name": "微信 ClawBot",
            "beta": True,
            "recommended": True,
            "modes": ["qr"],
            "fields": [],
            "hint": "微信扫码接入，仅支持单聊；协议非稳定契约，建议只用于任务通知。",
            "lede": "扫描二维码授权后，机器人即可接收任务通知（仅单聊）。",
            "guide_url": "https://github.com/Tencent/openclaw-weixin",
        },
        {
            "provider": "wecom",
            "name": "企业微信 AIBot",
            "beta": False,
            "recommended": False,
            "modes": ["stream", "webhook"],
            "fields": ["bot_id", "secret"],
            "hint": "企微管理后台创建智能机器人并开启「API 模式 → 长连接」，取 BotID 与 Secret。",
            "lede": "选择连接方式并输入对应凭据，以将此工作区绑定到企微 AIBot。",
            "guide_url": "https://developer.work.weixin.qq.com/document/path/101463",
        },
        {
            "provider": "feishu",
            "name": "飞书",
            "beta": False,
            "recommended": False,
            "modes": ["stream", "webhook"],
            "fields": ["app_id", "app_secret"],
            "hint": "开发者后台需把「事件与回调 → 订阅方式」同步改为「使用长连接」。",
            "lede": "填写自建应用的 App ID 与 App Secret，把工作区绑定到飞书机器人。",
            "guide_url": "https://open.feishu.cn/document/event-subscription-guide/callback-subscription/step-1-choose-a-subscription-mode/configure-callback-request-address?lang=zh-CN",
        },
        {
            "provider": "dingtalk",
            "name": "钉钉",
            "beta": False,
            "recommended": False,
            "modes": ["stream", "webhook"],
            "fields": ["app_key", "app_secret"],
            "hint": "开发管理 → 推送方式选 Stream，无需公网 IP 与验签解密。",
            "lede": "填写企业内部应用的 App Key 与 App Secret，把工作区绑定到钉钉机器人。",
            "guide_url": "https://open.dingtalk.com/document/orgapp/stream",
        },
        {
            "provider": "slack",
            "name": "Slack",
            "beta": False,
            "recommended": False,
            "modes": ["webhook"],
            "fields": ["bot_token", "signing_secret"],
            "hint": (
                "在 Slack 应用里开启 Event Subscriptions 与 Interactivity，"
                "两者都指向 /api/im/webhook/slack。"
                "Socket Mode 需要 app-level token（xapp-），当前表单放不下，暂不支持。"
            ),
            "lede": "填写 Bot Token 与 Signing Secret，把工作区绑定到 Slack 应用。",
            "guide_url": "https://api.slack.com/apis/events-api",
        },
    ]


__all__ = [
    "CARD_ACTIONS",
    "MODES",
    "PROVIDERS",
    "STATUSES",
    "ApprovalCard",
    "CardAction",
    "CardButton",
    "ImConnection",
    "ImConnectionConflict",
    "ImConnectionError",
    "ImConnectionManager",
    "ImConnectionNotFound",
    "ImConnectionStore",
    "ImProvider",
    "InboundEvent",
    "InvalidTransition",
    "SendResult",
    "get_im_manager",
    "provider_catalog",
    "set_im_manager",
]
