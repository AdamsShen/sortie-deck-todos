"""审批卡片渲染：三平台结构、签名链接、已处理态。"""

from __future__ import annotations

import time

import pytest

from sortie_deck.im.base import ApprovalCard
from sortie_deck.im.cards import (
    action_url,
    default_buttons,
    dingtalk_card,
    feishu_card,
    mark_done,
    render,
    signature_for,
    slack_card,
    verify_action_signature,
    wecom_card,
)


def make_card(**kwargs) -> ApprovalCard:
    base = {
        "title": "【门禁】示例任务 · eng_build",
        "stage": "eng_build",
        "mission_id": "ini_abc",
        "decision_id": "dc_123",
        "summary": "需要确认是否放行写 worktree",
        "buttons": default_buttons(),
        "fallback_url": "https://sortie.example.com/missions/ini_abc",
    }
    base.update(kwargs)
    return ApprovalCard(**base)


def test_default_buttons_order_and_actions():
    keys = [b.key for b in default_buttons()]
    # 放行在最前、停止在最末：误触成本最低的顺序
    assert keys == ["approve", "rewrite", "reject", "stop"]


def test_feishu_card_has_buttons_with_decision_id():
    card = feishu_card(make_card())
    assert card["config"]["update_multi"] is True
    assert card["header"]["title"]["content"].startswith("【门禁】")

    action_block = next(e for e in card["elements"] if e["tag"] == "action")
    values = [a["value"] for a in action_block["actions"]]
    assert all(v["decision_id"] == "dc_123" for v in values)
    assert {v["action"] for v in values} == {"approve", "rewrite", "reject", "stop"}


def test_feishu_done_card_drops_buttons_keeps_fallback():
    """已处理态必须去掉按钮，否则用户还能再点一次。"""
    card = feishu_card(mark_done(make_card(), "approved"))
    assert not any(e["tag"] == "action" for e in card["elements"])
    note = next(e for e in card["elements"] if e["tag"] == "note")
    assert "已通过" in note["elements"][0]["content"]
    assert "https://sortie.example.com/missions/ini_abc" in note["elements"][0]["content"]


def test_wecom_card_is_button_interaction():
    card = wecom_card(make_card())
    assert card["card_type"] == "button_interaction"
    assert card["task_id"] == "dc_123"
    assert [b["key"] for b in card["button_list"]] == ["approve", "rewrite", "reject", "stop"]
    # 卡片过期后的兜底入口
    assert card["card_action"]["url"].endswith("/missions/ini_abc")


def test_wecom_done_card_has_no_buttons():
    card = wecom_card(mark_done(make_card(), "rejected"))
    assert "button_list" not in card
    assert "已拒绝" in card["sub_title_text"]


def test_dingtalk_card_uses_signed_action_url():
    """钉钉按钮只能跳 URL，所以链接必须自带签名与动作。"""
    card = make_card()
    secret = "hook-secret"
    rendered = dingtalk_card(
        card,
        url_builder=lambda b: action_url("https://sortie.example.com", secret, card, b),
    )
    btns = rendered["actionCard"]["btns"]
    assert len(btns) == 4
    url = btns[0]["actionURL"]
    assert "a=approve" in url and "d=dc_123" in url and "sig=" in url


def test_dingtalk_card_without_builder_has_no_dead_buttons():
    """没有签名 builder 时宁可不给按钮，也不给点了没反应的死按钮。"""
    rendered = dingtalk_card(make_card())
    assert "btns" not in rendered["actionCard"]


def test_slack_card_action_value_carries_decision():
    card = slack_card(make_card())
    actions = next(b for b in card["blocks"] if b["type"] == "actions")
    values = [e["value"] for e in actions["elements"]]
    assert values == [f"dc_123:{k}" for k in ("approve", "rewrite", "reject", "stop")]


def test_signature_roundtrip_and_tamper():
    secret = "s3"
    sig = signature_for(secret, "dc_1", "approve")
    assert verify_action_signature(secret, "dc_1", "approve", sig)
    # 换动作 / 换决策 / 换密钥都必须失效
    assert not verify_action_signature(secret, "dc_1", "reject", sig)
    assert not verify_action_signature(secret, "dc_2", "approve", sig)
    assert not verify_action_signature("other", "dc_1", "approve", sig)


def test_signature_expires():
    secret = "s3"
    old_ts = int(time.time()) - 90000  # 超过 24h
    sig = signature_for(secret, "dc_1", "approve", ts=old_ts)
    assert not verify_action_signature(secret, "dc_1", "approve", sig)


def test_render_dispatch_and_unknown_provider():
    card = make_card()
    assert "blocks" in render("slack", card)
    assert "actionCard" in render("dingtalk", card)
    with pytest.raises(ValueError):
        render("wechat", card)  # 微信无卡片能力，上层应退回纯文本


def test_to_text_fallback_mentions_slash_commands():
    """纯文本降级要能靠斜杠命令完成审批（微信/群机器人 webhook 走这条路）。"""
    text = make_card().to_text()
    assert "/approve" in text
    assert "/stop" in text
    assert "https://sortie.example.com/missions/ini_abc" in text
