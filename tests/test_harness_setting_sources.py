"""claude_code provider 的 setting_sources 解析。"""

from __future__ import annotations

from sortie_deck.harness_providers.claude_code import _setting_sources


def test_default_keeps_sdk_defaults():
    assert _setting_sources("") is None
    assert _setting_sources(None) is None


def test_none_disables_filesystem_settings():
    """隔离模式：不加载本机 settings / 插件 hooks。"""
    assert _setting_sources("none") == []
    assert _setting_sources(" Off ") == []


def test_explicit_sources():
    assert _setting_sources("project") == ["project"]
    assert _setting_sources("user,local") == ["user", "local"]


def test_unknown_values_fall_back_to_default():
    assert _setting_sources("bogus") is None
