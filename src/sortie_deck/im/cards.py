"""审批卡片的平台渲染。

:class:`~sortie_deck.im.base.ApprovalCard` 是唯一输入，四个渠道各自翻译：

* **飞书** —— 交互卡片，按钮 ``value`` 回传 ``decision_id + action``，走
  ``card.action.trigger`` 回调（真交互）；
* **企业微信** —— ``template_card``（``button_interaction``），点按钮推
  ``template_card_event``（真交互，5 秒内可 update）；
* **Slack** —— Block Kit，``block_actions`` 回传（真交互）；
* **钉钉** —— ActionCard 的按钮**只能跳 URL，不能回传回调**。群机器人 webhook
  没有交互卡片能力，要真回调必须走企业应用互动卡片（需在卡片平台建模板 +
  ``outTrackId``）。因此这里降级为「按钮跳签名链接」：点击由我们的
  ``/api/im/action`` 处理，效果等价，少一层卡片模板依赖。

所有渲染都必须带 :attr:`ApprovalCard.fallback_url`，因为飞书卡片 14 天后
不能再更新、30 天后不能再交互，过期后只能引导回工作台。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from urllib.parse import urlencode

from sortie_deck.im.base import ApprovalCard, CardButton

# 各平台按钮语义色
_FEISHU_STYLE = {"primary": "primary", "danger": "danger", "default": "default"}
_SLACK_STYLE = {"primary": "primary", "danger": "danger", "default": "default"}

# 审批完成后的状态文案
_STATUS_TEXT = {
    "pending": "待处理",
    "approved": "已通过",
    "rejected": "已拒绝",
    "expired": "已超时",
    "stopped": "已停止",
}

_STATUS_TEMPLATE = {
    "pending": "orange",
    "approved": "green",
    "rejected": "red",
    "expired": "grey",
    "stopped": "grey",
}


def default_buttons() -> list[CardButton]:
    """门禁的四个标准动作。顺序即展示顺序：放行在最前，停止在最末。"""
    return [
        CardButton(key="approve", text="通过", style="primary"),
        CardButton(key="rewrite", text="改指令重跑", style="default"),
        CardButton(key="reject", text="打回", style="danger"),
        CardButton(key="stop", text="停止", style="danger"),
    ]


def signature_for(secret: str, decision_id: str, action: str, ts: int | None = None) -> str:
    """钉钉按钮链接的签名：把 ``decision_id + action + 时间戳`` 绑在一起。

    链接是会被人转发/复制的，所以必须带时间戳并限制有效期（见
    :func:`verify_action_signature`），否则等于把审批权限变成了公开 URL。
    """
    moment = int(ts if ts is not None else time.time())
    base = f"{decision_id}:{action}:{moment}".encode()
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f"{moment}.{digest}"


def verify_action_signature(
    secret: str, decision_id: str, action: str, sig: str, *, max_age: int = 86400
) -> bool:
    if not secret or not sig:
        return False
    try:
        ts_text, _ = sig.split(".", 1)
        ts = int(ts_text)
    except (ValueError, AttributeError):
        return False
    if abs(time.time() - ts) > max_age:
        return False
    return hmac.compare_digest(signature_for(secret, decision_id, action, ts), sig)


def action_url(
    base_url: str,
    secret: str,
    card: ApprovalCard,
    button: CardButton,
) -> str:
    """钉钉按钮的跳转地址（后端会先验签再执行动作）。"""
    base = (base_url or "").rstrip("/")
    query = urlencode(
        {
            "d": card.decision_id,
            "a": button.key,
            "sig": signature_for(secret, card.decision_id, button.key),
        }
    )
    return f"{base}/api/im/action?{query}"


# --- 飞书 -------------------------------------------------------------------


def feishu_card(card: ApprovalCard, *, message_id: str = "") -> dict:
    """飞书交互卡片（card JSON 1.0，兼容性最好）。"""
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": card.summary or "—"}},
    ]
    if card.status == "pending" and card.buttons:
        actions = []
        for btn in card.buttons:
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn.text},
                    "type": _FEISHU_STYLE.get(btn.style, "default"),
                    "value": {
                        "decision_id": card.decision_id,
                        "action": btn.key,
                        "mission_id": card.mission_id,
                    },
                }
            )
        elements.append({"tag": "action", "actions": actions})
    notes = []
    if card.status != "pending":
        notes.append(f"状态：{_STATUS_TEXT.get(card.status, card.status)}")
    if card.fallback_url:
        notes.append(f"或到工作台处理：{card.fallback_url}")
    if card.footer:
        notes.append(card.footer)
    if notes:
        elements.append(
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "\n".join(notes)}]}
        )
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": _STATUS_TEMPLATE.get(card.status, "orange"),
            "title": {"tag": "plain_text", "content": card.title},
        },
        "elements": elements,
    }


def feishu_payload(card: ApprovalCard, *, receive_id: str) -> dict:
    """飞书发送消息的请求体（``im/v1/messages``）。"""
    return {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(feishu_card(card), ensure_ascii=False),
    }


# --- 企业微信 ---------------------------------------------------------------


def wecom_card(card: ApprovalCard) -> dict:
    """企微模板卡片（``card_type=button_interaction``）。

    企微模板卡片的按钮只能带 ``key``，点击后推 ``template_card_event``，
    服务端在 5 秒内调 ``updateTemplateCard`` 换掉卡片。
    """
    body: dict = {
        "card_type": "button_interaction",
        "source": {"desc": "Sortie Deck", "desc_color": 1},
        "main_title": {"title": card.title, "desc": card.stage},
        "quote_area": {
            "type": 1,
            "title": "任务",
            "quote_text": card.mission_id,
        },
        "sub_title_text": card.summary or "",
    }
    if card.status == "pending" and card.buttons:
        body["button_list"] = [{"text": b.text, "key": b.key} for b in card.buttons]
    else:
        body["sub_title_text"] = (
            f"{card.summary}\n状态：{_STATUS_TEXT.get(card.status, card.status)}"
        ).strip()
    if card.fallback_url:
        body["card_action"] = {"type": 1, "url": card.fallback_url}
    body["task_id"] = card.decision_id
    return body


# --- 钉钉 -------------------------------------------------------------------


def dingtalk_card(
    card: ApprovalCard,
    *,
    url_builder: Callable[[CardButton], str] | None = None,
) -> dict:
    """钉钉 ActionCard。

    按钮只支持 ``actionURL``；``url_builder`` 负责生成带签名的确认链接。
    没有 builder 时退化为纯文本卡片（不加按钮），避免出现点了没反应的死按钮。
    """
    status_line = (
        ""
        if card.status == "pending"
        else f"\n\n**状态**：{_STATUS_TEXT.get(card.status, card.status)}"
    )
    text = f"**阶段**：{card.stage}\n\n{card.summary or '—'}{status_line}"
    if card.fallback_url:
        text += f"\n\n[到工作台处理]({card.fallback_url})"
    payload: dict = {
        "msgtype": "actionCard",
        "actionCard": {
            "title": card.title,
            "text": text,
            "btnOrientation": "0",
        },
    }
    if card.status == "pending" and card.buttons and url_builder is not None:
        payload["actionCard"]["btns"] = [
            {"title": b.text, "actionURL": url_builder(b)} for b in card.buttons
        ]
    return payload


# --- Slack ------------------------------------------------------------------


def slack_card(card: ApprovalCard) -> dict:
    """Slack Block Kit。"""
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": card.title[:150]},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": card.summary or "—"},
        },
    ]
    if card.status == "pending" and card.buttons:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": b.text},
                        "style": _SLACK_STYLE.get(b.style, "default"),
                        "action_id": b.key,
                        "value": f"{card.decision_id}:{b.key}",
                    }
                    for b in card.buttons
                ],
            }
        )
    notes = []
    if card.status != "pending":
        notes.append(f"状态：{_STATUS_TEXT.get(card.status, card.status)}")
    if card.fallback_url:
        notes.append(f"<{card.fallback_url}|到工作台处理>")
    if notes:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(notes)}]}
        )
    return {"blocks": blocks, "text": card.title}  # text 是通知栏降级文案


# --- 统一入口 ---------------------------------------------------------------


def render(
    provider: str,
    card: ApprovalCard,
    *,
    action_url_builder: Callable[[CardButton], str] | None = None,
) -> dict:
    """按渠道渲染。未知渠道抛 ValueError（上层应捕获并退回纯文本）。"""
    if provider == "feishu":
        return feishu_card(card)
    if provider == "wecom":
        return wecom_card(card)
    if provider == "dingtalk":
        return dingtalk_card(card, url_builder=action_url_builder)
    if provider == "slack":
        return slack_card(card)
    raise ValueError(f"不支持卡片的渠道: {provider}")


def mark_done(card: ApprovalCard, status: str) -> ApprovalCard:
    """返回已处理态的副本：去掉按钮，保留结论，用于就地更新卡片。"""
    return card.model_copy(update={"status": status, "buttons": []})
