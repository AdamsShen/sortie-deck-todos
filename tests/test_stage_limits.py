"""按阶段限 agent 回合上限 + 阶段级禁拉群：解析与判断逻辑。"""

from __future__ import annotations

from sortie_deck.harness_providers.base import parse_stage_turns, resolve_stage_max_turns
from sortie_deck.subroom import stage_allows_subroom

# --- 阶段回合上限 ---------------------------------------------------------------


def test_parse_stage_turns_ignores_invalid_entries():
    mapping = parse_stage_turns("intake=2, eng_backend=0 ,bad, x=abc, y=-1, =5")
    assert mapping == {"intake": 2, "eng_backend": 0}  # 非法/负数项丢弃，0=不限制保留


def test_resolve_stage_max_turns_prefers_stage_mapping(monkeypatch):
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "harness_max_turns", 7)
    monkeypatch.setattr(settings, "stage_max_turns", "intake=2")
    assert resolve_stage_max_turns("intake") == 2
    assert resolve_stage_max_turns("INTAKE") == 2  # 大小写不敏感

    # 未列出的阶段回落全局值
    assert resolve_stage_max_turns("eng_backend") == 7


def test_resolve_stage_max_turns_defaults(monkeypatch):
    from sortie_deck.settings import settings

    # 两个配置都为空 → 0（不限制），provider 会传 None
    monkeypatch.setattr(settings, "harness_max_turns", 0)
    monkeypatch.setattr(settings, "stage_max_turns", "")
    assert resolve_stage_max_turns("intake") == 0
    assert resolve_stage_max_turns("") == 0


# --- 阶段级禁拉群 ---------------------------------------------------------------


def test_stage_allows_subroom_disables_intake_by_default():
    assert stage_allows_subroom("intake") is False
    assert stage_allows_subroom("INTAKE") is False
    assert stage_allows_subroom("product_prd") is True
    assert stage_allows_subroom("") is True


def test_stage_allows_subroom_configurable(monkeypatch):
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "subroom_disabled_stages", "intake,product_prd")
    assert stage_allows_subroom("intake") is False
    assert stage_allows_subroom("product_prd") is False
    assert stage_allows_subroom("eng_web") is True

    monkeypatch.setattr(settings, "subroom_disabled_stages", "")
    assert stage_allows_subroom("intake") is True
