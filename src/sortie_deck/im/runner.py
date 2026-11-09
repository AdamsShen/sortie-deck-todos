"""后台连接运行器：微信长轮询、企微 WebSocket。

为什么不放在适配器里：适配器只做「协议编解码」，生命周期（重连、退避、
停止）是横切关注点，抽出来才能让每个渠道的循环长得一样、也才能统一治理。

两条硬约束：

* **单消费者**：飞书/钉钉推送不广播、企微新连接会踢掉旧连接，所以只有
  ``TDT_IM_CONSUMER=true`` 的进程才允许跑这里的循环；
* **重连退避**：1s → 2s → 4s → … → 30s 上限，连续失败过多时把连接置
  ``error`` 并把最后一次错误写到管理页，而不是无限打转。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from sortie_deck.im import wechat, wecom
from sortie_deck.im.connection import ImConnectionStore

logger = logging.getLogger(__name__)

BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
# 连续失败多少次后把连接标记为 error（后台仍继续重试，只是不再刷屏）
MAX_SILENT_FAILURES = 5


def _backoff(attempt: int) -> float:
    return min(BACKOFF_MAX, BACKOFF_START * (2 ** min(attempt, 5)))


class ImRunner:
    """按接入记录起后台任务。"""

    def __init__(self, *, store: ImConnectionStore, router, providers: dict[str, object]) -> None:
        self.store = store
        self.router = router
        self.providers = providers
        self._tasks: dict[str, asyncio.Task] = {}
        self._stopping = False

    # --- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        """为所有「已启用的主动拉取型连接」起循环。

        飞书/钉钉的 stream 模式依赖官方 SDK，本版本未内置长连接实现，
        因此这里只处理 wechat（长轮询）与 wecom（WebSocket）。
        """
        for conn in self.store.list():
            if not conn.enabled or not conn.has_secret:
                continue
            if conn.provider in {"wechat", "wecom"}:
                self.spawn(conn.id, conn.provider)

    def spawn(self, conn_id: str, provider: str) -> None:
        existing = self._tasks.get(conn_id)
        if existing is not None and not existing.done():
            return
        loop = self._wechat_loop if provider == "wechat" else self._wecom_loop
        self._tasks[conn_id] = asyncio.create_task(loop(conn_id))
        logger.info("IM 连接循环已启动 conn=%s provider=%s", conn_id, provider)

    async def cancel(self, conn_id: str) -> None:
        """停掉单条连接的循环（删除接入记录时用）。"""
        task = self._tasks.pop(conn_id, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    # --- 微信长轮询 -------------------------------------------------------

    async def _wechat_loop(self, conn_id: str) -> None:
        cursor = ""
        attempt = 0
        failures = 0
        provider = self.providers.get("wechat")
        if provider is not None and hasattr(provider, "mark_active"):
            provider.mark_active(conn_id, True)
        # 无论正常退出、缺 token 还是被 cancel，都要注销 active，
        # 否则探活会一直以为"循环在跑"而给出假阳性。
        try:
            while not self._stopping:
                conn = self._safe_get(conn_id)
                if conn is None or not conn.enabled:
                    return
                try:
                    token = self.store.decrypt_secret(conn_id)
                    if not token:
                        logger.warning("微信连接缺 token，退出循环 conn=%s", conn_id)
                        return
                    msgs, cursor = await wechat.poll_once(token, cursor)
                    attempt = 0
                    failures = 0
                    if msgs:
                        # 原始消息结构必须可见：不同实现的字段/类型语义有差异，
                        # 出问题时要能一眼看出"是没收到"还是"被过滤了"。
                        logger.info(
                            "微信收到 %s 条原始消息: %s",
                            len(msgs),
                            [
                                {
                                    "type": m.get("message_type"),
                                    "from": str(m.get("from_user_id"))[:24],
                                    "items": len(m.get("item_list") or []),
                                }
                                for m in msgs
                            ],
                        )
                    events = wechat.decode_events(msgs, bot_id=getattr(conn, "app_id", ""))
                    if msgs and not events:
                        logger.warning("微信消息全部被过滤（可能是机器人自己发的）")
                    for event in events:
                        self.router.schedule(event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — 网络抖动必须自愈
                    failures += 1
                    attempt = min(attempt + 1, 5)
                    logger.warning(
                        "微信长轮询失败 conn=%s 第 %s 次: %s（%.0fs 后重试）",
                        conn_id,
                        failures,
                        exc,
                        _backoff(attempt),
                    )
                    if failures == MAX_SILENT_FAILURES:
                        with contextlib.suppress(Exception):
                            self.store.set_status(conn_id, "error", error=str(exc))
                    await asyncio.sleep(_backoff(attempt))
        finally:
            if provider is not None and hasattr(provider, "mark_active"):
                provider.mark_active(conn_id, False)

    # --- 企微 WebSocket ---------------------------------------------------

    async def _wecom_loop(self, conn_id: str) -> None:
        attempt = 0
        failures = 0
        while not self._stopping:
            conn = self._safe_get(conn_id)
            if conn is None or not conn.enabled:
                return
            provider = self.providers.get("wecom")
            secret = ""
            with contextlib.suppress(Exception):
                secret = self.store.decrypt_secret(conn_id)
            if provider is None or not secret:
                logger.warning("企微连接缺适配器或 secret，退出循环 conn=%s", conn_id)
                return
            try:
                await self._wecom_session(conn, provider, secret)
                attempt = 0
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failures += 1
                attempt = min(attempt + 1, 5)
                logger.warning(
                    "企微长连接断开 conn=%s 第 %s 次: %s（%.0fs 后重连）",
                    conn_id,
                    failures,
                    exc,
                    _backoff(attempt),
                )
                # 企微同一机器人只允许一条连接：被踢时不要立刻抢，退避后再试
                if failures == MAX_SILENT_FAILURES:
                    with contextlib.suppress(Exception):
                        self.store.set_status(conn_id, "error", error=str(exc))
                await asyncio.sleep(_backoff(attempt))

    async def _wecom_session(self, conn, provider, secret: str) -> None:
        import websockets

        async with websockets.connect(wecom.WS_URL, open_timeout=10, close_timeout=5) as ws:
            await ws.send(
                json.dumps(
                    {
                        "cmd": wecom.CMD_SUBSCRIBE,
                        "headers": {"req_id": wecom._req_id("subscribe")},
                        "body": {"bot_id": conn.app_id, "secret": secret},
                    }
                )
            )
            provider.attach(conn.id, ws)
            self.store.touch_event(conn.id)
            ping = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                    event = wecom.parse_inbound(payload)
                    if event is not None:
                        self.router.schedule(event)
            finally:
                ping.cancel()
                provider.detach(conn.id)

    async def _ping_loop(self, ws) -> None:
        """企微要求约 30s 一次心跳，否则连接会被服务端回收。"""
        while True:
            await asyncio.sleep(wecom.PING_INTERVAL)
            await ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": wecom._req_id("ping")}}))

    def _safe_get(self, conn_id: str):
        try:
            return self.store.get(conn_id)
        except Exception:  # noqa: BLE001 — 记录被删时循环应自行退出
            return None
