"""微信个人号（iLink Bot / ClawBot）适配器。

协议来源：``Tencent/openclaw-weixin`` 的 ``docs/protocol.md``。要点：

* 登录：``POST /ilink/bot/get_bot_qrcode?bot_type=3`` 取二维码 →
  ``GET /ilink/bot/get_qrcode_status?qrcode=...`` 轮询，``confirmed`` 时拿
  ``bot_token``（有效期到下一次登录）；
* 收消息：``POST /ilink/bot/getupdates`` 长轮询，用 ``get_updates_buf`` 做游标；
* 发消息：``POST /ilink/bot/sendmessage``，必须回传入站消息的 ``context_token``；
* 能力边界：**仅单聊、无卡片**，因此审批按钮只能落到飞书/钉钉/企微，
  微信这条通道用于「任务需要你处理 / 已完成 / 失败了」的通知。

协议文档明确声明「client types do not establish the complete server contract」，
所以本适配器是**尽力而为**：接口路径与字段按当前实现，失败时通过 ``probe``
把平台原始错误暴露到管理页，便于快速定位协议变化。
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any

import httpx

from sortie_deck.im.base import ApprovalCard, InboundEvent, SendResult

logger = logging.getLogger(__name__)

API_BASE = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
# 协议版本，仅用于 base_info 归因，不影响路由
CLIENT_VERSION = "0x00010000"
# 长轮询默认超时（服务端建议值优先）
DEFAULT_LONGPOLL_MS = 35000

# chat_id(=对方 user_id) → 最近一次 context_token。回复必须带它；
# 进程重启丢失可接受：下一条入站消息会补回来。
_CONTEXT_TOKENS: dict[str, str] = {}


def _uin() -> str:
    """``X-WECHAT-UIN``：随机 uint32 的 base64（协议要求，不具备鉴权语义）。"""
    value = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(value).encode()).decode()


def _headers(token: str = "") -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": CLIENT_VERSION,
        "X-WECHAT-UIN": _uin(),
    }
    if token:
        headers["AuthorizationType"] = "ilink_bot_token"
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _base_info() -> dict[str, str]:
    return {"channel_version": "1.0.0", "bot_agent": "SortieDeck"}


async def get_bot_qrcode(*, base_url: str = API_BASE, timeout: float = 15.0) -> dict[str, Any]:
    """申请登录二维码。返回 ``{qrcode, qrcode_img_content}``。

    ``qrcode_img_content`` 可能是 URL 也可能是可直接渲染的二维码内容，
    前端统一当作「二维码载荷」处理。
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{base_url}/ilink/bot/get_bot_qrcode",
            params={"bot_type": 3},
            headers=_headers(),
            json={"local_token_list": []},
        )
    data = resp.json()
    if not data.get("qrcode"):
        raise RuntimeError(f"微信二维码申请失败: {data}")
    return data


async def get_qrcode_status(
    qrcode: str, *, base_url: str = API_BASE, timeout: float = 15.0
) -> dict[str, Any]:
    """轮询扫码状态：wait / scaned / confirmed / expired / need_verifycode。"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"{base_url}/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode},
            # 轮询不带 Authorization（协议如此）
            headers=_headers(),
        )
    return resp.json()


def qr_data_uri(content: str) -> str:
    """把二维码载荷渲染成 SVG data URI。

    微信返回的 ``qrcode_img_content`` 是**网页链接**（``liteapp.weixin.qq.com``），
    不是图片地址——直接塞进 ``<img>`` 必然破图。这里用 segno 离线生成，
    矢量清晰，且不把用户数据发给任何第三方二维码服务。
    """
    if not content:
        return ""
    try:
        import segno
    except ImportError:  # pragma: no cover - segno 在主依赖里，缺失只降级为链接
        logger.warning("未安装 segno，微信二维码只能以链接形式提供")
        return ""
    try:
        return segno.make(content, error="m").svg_data_uri(
            scale=5, border=2, dark="#1a1f36", light="#ffffff"
        )
    except Exception as exc:  # noqa: BLE001 — 生成失败降级为链接
        logger.warning("二维码生成失败: %s", exc)
        return ""


def parse_inbound(payload: dict[str, Any], *, bot_id: str = "") -> InboundEvent | None:
    """归一化 ``getUpdates`` 返回的单条 ``WeixinMessage``。

    过滤机器人自己的消息（否则会自己回自己）。判断方式有两种：

    * 有 ``bot_id`` 时比较 ``from_user_id`` —— 比 ``message_type`` 可靠，
      因为 1=user / 2=bot 的语义在不同实现里并不一致（实测会漏真实消息）；
    * 没有 ``bot_id`` 时退回 ``message_type == 1``。
    """
    if not isinstance(payload, dict):
        return None
    from_user = str(payload.get("from_user_id") or "")
    if not from_user:
        return None
    if bot_id:
        if from_user == bot_id or from_user.startswith(bot_id.split("@")[0]):
            return None
    elif int(payload.get("message_type") or 0) != 1:
        return None
    text = ""
    for item in payload.get("item_list") or []:
        if int(item.get("type") or 0) == 1:
            text = str((item.get("text_item") or {}).get("text") or "")
            break
    context_token = str(payload.get("context_token") or "")
    if context_token:
        # 出站回复必须回传，先存下来（key 用对方 user_id）
        _CONTEXT_TOKENS[from_user] = context_token
    return InboundEvent(
        provider="wechat",
        channel_id=from_user,
        chat_type="single",  # 微信个人号仅单聊
        sender_id=from_user,
        sender_name=from_user,
        text=text.strip(),
        raw_id=str(payload.get("message_id") or payload.get("seq") or ""),
    )


class WeChatProvider:
    """微信适配器。

    ``conn.app_id`` 存 ``ilink_bot_id``（扫码后回填，便于人工核对是哪个号），
    ``secret`` 存 ``bot_token``。
    """

    name = "wechat"

    def __init__(self, secret_resolver) -> None:
        self._secret = secret_resolver
        self._base_url = API_BASE
        # 正在跑长轮询的连接。探活若发现循环已在运行，直接判定连通：
        # getupdates 是长轮询，重复发起会与后台循环抢同一个游标。
        self._active: set[str] = set()

    def mark_active(self, conn_id: str, active: bool = True) -> None:
        if active:
            self._active.add(conn_id)
        else:
            self._active.discard(conn_id)

    async def probe(self, conn) -> tuple[bool, str]:
        token = self._secret(conn)
        if not token:
            return False, "尚未扫码登录（没有 bot_token）"
        if conn.id in self._active:
            # 长轮询循环正在跑，说明登录态有效；再发一次 getupdates 会与它抢游标
            return True, ""
        try:
            # getupdates 是**长轮询**：服务端会 hold 到有消息或 35s 超时。
            # 这里用较短超时，并把「读超时」视为连通——请求已被接受并挂起，
            # 恰恰说明 token 有效、网络可达。
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    f"{self._base_url}/ilink/bot/getupdates",
                    headers=_headers(token),
                    json={"get_updates_buf": "", "base_info": _base_info()},
                )
            data = resp.json()
            ret = int(data.get("ret") or 0)
            if ret != 0:
                return False, (
                    f"登录态已失效 ret={ret} errmsg={data.get('errmsg', '')}（请重新扫码）"
                )
            return True, ""
        except httpx.ReadTimeout:
            # 长轮询被服务端 hold 住 = 请求已送达且 token 被接受
            return True, ""
        except httpx.HTTPStatusError as exc:
            return False, f"HTTP {exc.response.status_code}（微信后端拒绝了请求）"
        except Exception as exc:  # noqa: BLE001 — 探活失败原因要原样展示
            # 不用 str(exc)：Timeout/Connect 类异常的 message 常为空，
            # 界面上就只剩一个冒号，无法排查。
            detail = str(exc) or repr(exc)
            return False, f"连接微信后端失败（{type(exc).__name__}）: {detail}"

    async def send_text(self, conn, chat_id: str, text: str) -> SendResult:
        return await self._send_items(conn, chat_id, [{"type": 1, "text_item": {"text": text}}])

    async def send_card(self, conn, chat_id: str, card: ApprovalCard) -> SendResult:
        """微信无卡片能力：降级为带斜杠命令提示的纯文本。"""
        return await self.send_text(conn, chat_id, card.to_text())

    async def _send_items(self, conn, chat_id: str, item_list: list[dict[str, Any]]) -> SendResult:
        token = self._secret(conn)
        if not token:
            return SendResult(ok=False, provider="wechat", error="尚未扫码登录")
        context_token = _CONTEXT_TOKENS.get(chat_id, "")
        if not context_token:
            return SendResult(
                ok=False,
                provider="wechat",
                error="缺少 context_token（需对方先发一条消息，微信不允许冷启动私聊）",
            )
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": chat_id,
                "client_id": f"sortie_{int(time.time() * 1000)}",
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": item_list,
            },
            "base_info": _base_info(),
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self._base_url}/ilink/bot/sendmessage",
                    headers=_headers(token),
                    json=body,
                )
            data = resp.json()
            ret = int(data.get("ret") or 0)
            if ret != 0:
                return SendResult(
                    ok=False,
                    provider="wechat",
                    error=f"ret={ret} errmsg={data.get('errmsg', '')}",
                )
            return SendResult(ok=True, provider="wechat")
        except Exception as exc:  # noqa: BLE001 — 出站失败不能中断任务
            logger.warning("微信发送失败 conn=%s to=%s: %s", conn.id, chat_id, exc)
            return SendResult(ok=False, provider="wechat", error=str(exc))

    async def update_card(self, conn, chat_id: str, message_id: str, card: ApprovalCard) -> bool:
        return False  # 微信无卡片，无可更新对象

    def parse_inbound(self, payload: dict[str, Any]) -> InboundEvent | None:
        return parse_inbound(payload)


async def poll_once(
    bot_token: str,
    cursor: str = "",
    *,
    base_url: str = API_BASE,
    timeout: float | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """执行一次长轮询：返回 ``(msgs, next_cursor)``。

    服务端在无消息时会 hold 住请求直到超时，所以 timeout 要比
    ``longpolling_timeout_ms`` 略大。
    """
    wait = timeout if timeout is not None else DEFAULT_LONGPOLL_MS / 1000 + 5
    async with httpx.AsyncClient(timeout=wait) as client:
        resp = await client.post(
            f"{base_url}/ilink/bot/getupdates",
            headers=_headers(bot_token),
            json={"get_updates_buf": cursor, "base_info": _base_info()},
        )
    data = resp.json()
    if int(data.get("ret") or 0) != 0:
        raise RuntimeError(f"getupdates 失败 ret={data.get('ret')} errmsg={data.get('errmsg')}")
    next_cursor = str(data.get("get_updates_buf") or cursor)
    return list(data.get("msgs") or []), next_cursor


def decode_events(msgs: list[dict[str, Any]], *, bot_id: str = "") -> list[InboundEvent]:
    """批量归一化（过滤掉机器人自己发的消息）。"""
    events: list[InboundEvent] = []
    for msg in msgs:
        event = parse_inbound(msg, bot_id=bot_id)
        if event is not None:
            events.append(event)
    return events
