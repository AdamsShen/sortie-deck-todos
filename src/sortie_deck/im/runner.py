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

from sortie_deck.im import streams, wechat, wecom
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

        微信（长轮询）、企微（WebSocket）、飞书/钉钉（官方 SDK 长连接）都走这里；
        webhook 模式不需要后台循环，由 API 的入站路由直接处理。
        """
        for conn in self.store.list():
            if not conn.enabled or not conn.has_secret:
                continue
            if conn.provider in {"wechat", "wecom", "feishu", "dingtalk"}:
                self.spawn(conn.id, conn.provider)

    def spawn(self, conn_id: str, provider: str) -> None:
        existing = self._tasks.get(conn_id)
        if existing is not None and not existing.done():
            return
        if provider == "wechat":
            task = self._wechat_loop(conn_id)
        elif provider == "wecom":
            task = self._wecom_loop(conn_id)
        elif provider in {"feishu", "dingtalk"}:
            task = self._stream_loop(conn_id, provider)
        else:
            # 早先这里是 else → 企微循环，飞书会被错误地当成企微去连 WS
            logger.warning("渠道 %s 没有后台循环实现，跳过 conn=%s", provider, conn_id)
            return
        self._tasks[conn_id] = asyncio.create_task(task)
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

    # --- 飞书 / 钉钉长连接 -------------------------------------------------

    async def _stream_loop(self, conn_id: str, provider_name: str) -> None:
        """SDK 的阻塞循环跑在 daemon 线程，事件桥回主循环。

        状态语义：线程起来只是 ``connecting``；**收到第一条事件才算 connected**。
        SDK 不提供握手回调，用「线程活着」冒充「已连接」就是之前那个骗人的 bug。
        """
        loop = asyncio.get_running_loop()
        attempt = 0
        failures = 0
        while not self._stopping:
            conn = self._safe_get(conn_id)
            if conn is None or not conn.enabled:
                return
            secret = ""
            with contextlib.suppress(Exception):
                secret = self.store.decrypt_secret(conn_id)
            if not conn.app_id or not secret:
                logger.warning("%s 连接缺凭据，退出循环 conn=%s", provider_name, conn_id)
                return

            def emit(payload: dict, _loop=loop, _cid=conn_id) -> None:
                # SDK 线程 → 主循环；不能在这里直接操作 store / router
                asyncio.run_coroutine_threadsafe(
                    self._handle_stream_event(_cid, provider_name, payload), _loop
                )

            try:
                handle = streams.build_stream(
                    provider_name, app_id=conn.app_id, app_secret=secret, emit=emit
                )
            except ImportError as exc:
                # SDK 是可选依赖：缺了就停在这里，管理页显示可操作的错误
                msg = f"未安装长连接 SDK：{exc}"
                logger.warning("%s conn=%s", msg, conn_id)
                with contextlib.suppress(Exception):
                    self.store.set_status(conn_id, "error", error=msg)
                return

            handle.start()
            with contextlib.suppress(Exception):
                self.store.set_status(conn_id, "connecting", allow_force=True)
            logger.info(
                "%s 长连接线程已启动 conn=%s（等待首条事件确认握手）",
                provider_name,
                conn_id,
            )

            # SDK 内部 auto_reconnect，正常不会退出；这里只等它真的挂掉
            while handle.alive and not self._stopping:
                await asyncio.sleep(1.0)
                current = self._safe_get(conn_id)
                if current is None or not current.enabled:
                    handle.stop()
                    return
            handle.stop()

            if self._stopping:
                return
            failures += 1
            attempt = min(attempt + 1, 5)
            err = handle.error or "长连接线程退出"
            logger.warning(
                "%s 长连接断开 conn=%s 第 %s 次: %s（%.0fs 后重连）",
                provider_name,
                conn_id,
                failures,
                err,
                _backoff(attempt),
            )
            if failures == MAX_SILENT_FAILURES:
                with contextlib.suppress(Exception):
                    self.store.set_status(conn_id, "error", error=err)
            await asyncio.sleep(_backoff(attempt))

    async def _handle_stream_event(
        self, conn_id: str, provider_name: str, payload: dict
    ) -> None:
        """长连接收到的一帧：解析 → 记活跃 → 交给路由。"""
        provider = self.providers.get(provider_name)
        if provider is None:
            return
        try:
            event = provider.parse_inbound(payload)
        except Exception as exc:  # noqa: BLE001 — 一帧解析失败不能拖垮连接
            logger.warning("%s 长连接事件解析失败: %s", provider_name, exc)
            return
        if event is None:
            return
        current = self._safe_get(conn_id)
        if current is None:
            return
        with contextlib.suppress(Exception):
            self.store.touch_event(conn_id)
        if current.status != "connected":
            with contextlib.suppress(Exception):
                self.store.set_status(conn_id, "connected", allow_force=True)
        self.router.schedule(event)

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
