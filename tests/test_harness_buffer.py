"""harness 单条消息缓冲：配置解析、溢出提示与失败降级。

背景：claude-agent-sdk 默认只允许 1 MiB 的单条 CLI 消息，编码 agent 用 Read
打开截图时图片会 base64 进消息，超限直接抛错并让整个阶段失败。
"""

from __future__ import annotations

import logging

from sortie_deck.harness_providers.base import HarnessSession
from sortie_deck.harness_providers.claude_code import (
    ClaudeCodeProvider,
    _buffer_overflow_hint,
    _max_buffer_size,
)


def test_max_buffer_size_accepts_positive_values():
    assert _max_buffer_size(10 * 1024 * 1024) == 10 * 1024 * 1024
    assert _max_buffer_size("2097152") == 2097152


def test_max_buffer_size_falls_back_to_sdk_default():
    # 0/None/非法值都应返回 None，交回 SDK 内置的 1 MiB 默认值
    assert _max_buffer_size(0) is None
    assert _max_buffer_size(None) is None
    assert _max_buffer_size("abc") is None
    assert _max_buffer_size(-1) is None


def test_buffer_overflow_hint_only_matches_buffer_errors():
    overflow = RuntimeError(
        "Failed to decode JSON: JSON message exceeded maximum buffer size of 1048576 bytes"
    )
    hint = _buffer_overflow_hint(overflow, 10 * 1024 * 1024)
    assert "TDT_HARNESS_MAX_BUFFER_BYTES" in hint
    assert "10485760" in hint

    assert _buffer_overflow_hint(RuntimeError("connection reset"), 10) == ""


async def test_send_degrades_buffer_overflow_with_context(monkeypatch, caplog):
    provider = ClaudeCodeProvider()
    monkeypatch.setattr(
        "sortie_deck.harness_providers.claude_code._sdk_available", lambda: True
    )

    async def explode(*args, **kwargs):
        raise RuntimeError("JSON message exceeded maximum buffer size of 1048576 bytes")

    monkeypatch.setattr(ClaudeCodeProvider, "_send_via_sdk", explode)
    session = HarnessSession(
        id="hs_test",
        provider="claude_code",
        meta={"initiative_id": "ini_test", "stage": "eng_implement"},
    )

    with caplog.at_level(logging.ERROR, logger="sortie_deck.harness_providers.claude_code"):
        reply = await provider.send(session, "写出贪吃蛇")

    assert reply.status == "fail"
    assert reply.meta["buffer_overflow"] is True
    assert reply.meta["error_type"] == "RuntimeError"
    assert "TDT_HARNESS_MAX_BUFFER_BYTES" in reply.text
    # 异常不能被静默吞掉：日志里要能定位到任务与阶段
    assert "ini_test" in caplog.text
    assert "eng_implement" in caplog.text
