"""IM 栈装配：存储 + 适配器 + 管理器 + 入站路由。

只有这里知道"用哪个配置、注入哪些依赖"；适配器和路由本身不读全局配置，
这样测试可以整体替换而不动环境变量。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sortie_deck.im.cards import action_url
from sortie_deck.im.connection import ImConnectionManager, ImConnectionStore
from sortie_deck.im.dingtalk import DingTalkProvider
from sortie_deck.im.feishu import FeishuProvider
from sortie_deck.im.identity import ImIdentityStore, LinkCodeService
from sortie_deck.im.intent import IntentParser
from sortie_deck.im.qr_login import QrLoginService
from sortie_deck.im.router import ImDedupe, ImRouter
from sortie_deck.im.runner import ImRunner
from sortie_deck.im.wechat import WeChatProvider
from sortie_deck.im.wecom import WeComProvider

logger = logging.getLogger(__name__)


@dataclass
class ImStack:
    store: ImConnectionStore
    manager: ImConnectionManager
    router: ImRouter
    dedupe: ImDedupe
    qr: QrLoginService
    runner: ImRunner
    identity: ImIdentityStore
    link_codes: LinkCodeService


def build_stack(
    *,
    cfg: Any,
    orchestrator_factory: Callable[[], Any] | None = None,
) -> ImStack:
    """按配置装配。主密钥缺失不阻塞启动：只读（列表/状态）仍可用，写入会报错。"""
    secret_key = cfg.resolved_secret_key()
    if not secret_key:
        logger.warning(
            "未配置 TDT_SECRET_KEY / TDT_GIT_SECRET_KEY："
            "IM 接入可以浏览但无法保存凭据（不会降级为明文存储）"
        )

    store = ImConnectionStore(cfg.im_db(), secret_key=secret_key)

    def resolve_secret(conn) -> str:
        # 适配器不碰存储，由这里注入解密结果
        return store.decrypt_secret(conn.id)

    base_url = (getattr(cfg, "public_base_url", "") or "").rstrip("/")
    sign_secret = cfg.resolved_token_secret()

    def dingtalk_url(card, button) -> str:
        """钉钉按钮跳转链接（带签名，后端验签后执行动作）。"""
        return action_url(base_url, sign_secret, card, button)

    providers = {
        "wechat": WeChatProvider(resolve_secret),
        "feishu": FeishuProvider(resolve_secret),
        "dingtalk": DingTalkProvider(resolve_secret, action_url_builder=dingtalk_url),
        "wecom": WeComProvider(resolve_secret),
    }
    manager = ImConnectionManager(
        store=store,
        providers=providers,
        consumer=bool(getattr(cfg, "im_consumer", True)),
    )
    dedupe = ImDedupe(cfg.im_db().parent / "im_events.sqlite")
    identity = ImIdentityStore(cfg.im_db().parent / "im_identities.sqlite")

    async def reply(provider_name: str, chat_id: str, text: str) -> None:
        """交互回执（/bind 结果、未绑定提示）。失败由 router 记录，不影响主流程。"""
        provider = providers.get(provider_name)
        conn = store.get_by_provider(provider_name)
        if provider is not None and conn is not None and chat_id:
            await provider.send_text(conn, chat_id, text)

    router = ImRouter(
        store=store,
        dedupe=dedupe,
        orchestrator_factory=orchestrator_factory,
        identity=identity,
        link_codes=LinkCodeService(),
        intent=IntentParser(),
        reply=reply,
    )
    runner = ImRunner(store=store, router=router, providers=providers)
    return ImStack(
        store=store,
        manager=manager,
        router=router,
        dedupe=dedupe,
        qr=QrLoginService(store=store),
        runner=runner,
        identity=identity,
        link_codes=router.link_codes,
    )


_STACK: ImStack | None = None
_LOCK = threading.Lock()


def get_stack() -> ImStack | None:
    global _STACK
    if _STACK is not None:
        return _STACK
    with _LOCK:
        if _STACK is not None:
            return _STACK
        try:
            from sortie_deck.settings import Settings

            _STACK = build_stack(cfg=Settings())
        except Exception as exc:  # noqa: BLE001 — IM 是增强能力，不能拖垮 API 启动
            logger.warning("IM 栈初始化失败（IM 功能不可用，其余功能正常）: %s", exc)
            return None
    return _STACK


def set_stack(stack: ImStack | None) -> None:
    """测试注入用。"""
    global _STACK
    with _LOCK:
        _STACK = stack
