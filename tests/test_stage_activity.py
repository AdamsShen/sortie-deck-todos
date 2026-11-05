"""阶段内工具级进度的回归测试。

前端转圈旁的「正在编辑 src/game.js」来自 provider 的工具回调：
1. 只有声明了 ``on_activity`` 的 provider 才会收到回调（旧签名不能被传参打挂）；
2. 工具入参要能压成一个短目标，避免把整段 prompt 写进事件表。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from sortie_deck.harness_providers.base import HarnessReply
from sortie_deck.harness_providers.claude_code import _activity_detail
from sortie_deck.harness_providers.executor import (
    _activity_callback,
    _send_turn,
    _supports_on_activity,
)


class _LegacyProvider:
    """旧签名 provider：不接受 on_activity。"""

    name = "legacy"

    async def send(self, session, prompt, *, cwd=None, context_pack=None, extra_dirs=None):
        return HarnessReply(status="ok", text="legacy")


class _ActivityProvider:
    """新签名 provider：记录收到的 on_activity。"""

    name = "with-activity"

    def __init__(self) -> None:
        self.received = None

    async def send(self, session, prompt, *, cwd=None, context_pack=None, on_activity=None):
        self.received = on_activity
        return HarnessReply(status="ok", text="ok")


def test_supports_on_activity_detects_signature():
    assert _supports_on_activity(_ActivityProvider()) is True
    assert _supports_on_activity(_LegacyProvider()) is False


def test_send_turn_skips_callback_for_legacy_provider(tmp_path):
    provider = _LegacyProvider()

    async def cb(tool, detail):  # pragma: no cover — 不应被调用
        raise AssertionError("legacy provider 不该收到回调")

    reply = asyncio.run(
        _send_turn(
            provider,
            session=object(),
            prompt="p",
            cwd=tmp_path,
            pack={},
            on_activity=cb,
        )
    )
    assert reply.status == "ok"


def test_send_turn_forwards_callback_when_supported(tmp_path):
    provider = _ActivityProvider()

    async def cb(tool, detail):  # pragma: no cover — 只验证转发
        return None

    asyncio.run(
        _send_turn(
            provider,
            session=object(),
            prompt="p",
            cwd=tmp_path,
            pack={},
            on_activity=cb,
        )
    )
    assert provider.received is cb


def test_activity_detail_prefers_file_and_command():
    assert _activity_detail({"file_path": "/tmp/mission/src/game.js"}) == "game.js"
    assert _activity_detail({"command": "npm   test\n--watch"}) == "npm test --watch"
    assert _activity_detail({"pattern": "TODO"}) == "TODO"
    assert _activity_detail({}) == ""
    assert len(_activity_detail({"description": "x" * 200})) <= 60


def test_activity_detail_handles_non_str_values():
    assert _activity_detail({"file_path": Path("/tmp/a/b.md")}) == ""


def test_activity_callback_emits_and_throttles():
    received = []
    ctx = SimpleNamespace(
        stage="eng_implement",
        role="eng",
        progress_cb=lambda payload: received.append(payload),
    )
    cb = _activity_callback(ctx)

    asyncio.run(cb("Edit", "src/game.js"))
    # 0.8s 内的第二条必须被节流：否则一轮连读多个文件会刷屏事件表
    asyncio.run(cb("Read", "src/map.js"))

    assert len(received) == 1
    assert received[0]["phase"] == "activity"
    assert received[0]["stage"] == "eng_implement"
    assert received[0]["tool"] == "Edit"
    assert received[0]["detail"] == "src/game.js"
