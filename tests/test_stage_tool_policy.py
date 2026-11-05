"""阶段级工具禁用：tech_design 只写文档，不给 Bash。

链路：Settings.stage_disallowed_tools → graph 解析 → StageContext.disallowed_tools
→ 执行器放进 context pack → claude_code provider 映射到 SDK 的 disallowed_tools；
同时 prompt 里显式声明，避免 agent 先撞一次拒绝再放弃。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

from sortie_deck.graph import _match_stage_rule, _resolve_stage_disallowed_tools
from sortie_deck.harness_providers.base import HarnessSession
from sortie_deck.harness_providers.claude_code import ClaudeCodeProvider
from sortie_deck.harness_providers.executor import HarnessStageExecutor
from sortie_deck.models import RoleId, StageContext
from sortie_deck.settings import Settings


def _ctx(**kwargs) -> StageContext:
    payload = {
        "initiative_id": "ini_test",
        "thread_id": "thr_test",
        "stage": "eng_web_tech_design",
        "role": RoleId.ENG_WEB,
        "brief": "贪吃蛇小游戏",
        "artifact_dir": "/tmp/artifacts",
    }
    payload.update(kwargs)
    return StageContext(**payload)


def test_tech_design_disallows_bash_by_default() -> None:
    """技术方案阶段默认禁 Bash；编码/自测阶段不能被误伤。"""
    cfg = Settings()
    assert _resolve_stage_disallowed_tools("eng_web_tech_design", cfg) == ["Bash"]
    assert _resolve_stage_disallowed_tools("eng_backend_tech_design", cfg) == ["Bash"]
    assert _resolve_stage_disallowed_tools("eng_web", cfg) == []
    assert _resolve_stage_disallowed_tools("eng_backend_selftest", cfg) == []


def test_disallowed_tools_exact_rule_beats_wildcard() -> None:
    """精确阶段规则优先于通配；多工具用 | 分隔。"""
    cfg = Settings(
        stage_disallowed_tools="*_tech_design=Bash,eng_web_tech_design=Bash|Write"
    )
    assert _resolve_stage_disallowed_tools("eng_web_tech_design", cfg) == ["Bash", "Write"]
    assert _resolve_stage_disallowed_tools("eng_backend_tech_design", cfg) == ["Bash"]


def test_match_stage_rule_supports_wildcard() -> None:
    assert _match_stage_rule("eng_web_tech_design", "*_tech_design=600") == "600"
    assert _match_stage_rule("eng_web", "*_tech_design=600") is None


def test_prompt_declares_disallowed_tools(tmp_path: Path) -> None:
    """prompt 里先说清楚，agent 不会先试 Bash 撞一次拒绝。"""
    ctx = _ctx(disallowed_tools=["Bash"])
    prompt = HarnessStageExecutor()._build_prompt(ctx, tmp_path, {"briefs": {}}, "")
    assert "Bash is NOT available" in prompt


def test_prompt_has_no_tool_policy_when_unrestricted(tmp_path: Path) -> None:
    ctx = _ctx(stage="eng_web")
    prompt = HarnessStageExecutor()._build_prompt(ctx, tmp_path, {"briefs": {}}, "")
    assert "Tool policy for this stage" not in prompt


def test_claude_options_receive_disallowed_tools(monkeypatch, tmp_path: Path) -> None:
    """pack 里的禁用工具必须落到 SDK options；空列表退化为 None（不限制）。"""
    captured: dict[str, object] = {}

    class _FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class _FakeMessage:
        pass

    async def _empty_query(**kwargs: object):
        if False:  # pragma: no cover — 只为让它成为异步生成器
            yield _FakeMessage()

    fake_mod = types.ModuleType("claude_agent_sdk")
    fake_types = types.ModuleType("claude_agent_sdk.types")
    fake_mod.ClaudeAgentOptions = _FakeOptions  # type: ignore[attr-defined]
    fake_mod.query = _empty_query  # type: ignore[attr-defined]
    fake_types.AssistantMessage = _FakeMessage  # type: ignore[attr-defined]
    fake_types.ResultMessage = _FakeMessage  # type: ignore[attr-defined]
    fake_types.SystemMessage = _FakeMessage  # type: ignore[attr-defined]
    fake_mod.types = fake_types  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_mod)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types)

    provider = ClaudeCodeProvider()
    session = HarnessSession(id="hs_test", provider="claude_code")

    asyncio.run(
        provider._send_via_sdk(
            session,
            "写技术方案",
            cwd=str(tmp_path),
            context_pack={"disallowed_tools": ["Bash"]},
        )
    )
    assert captured["disallowed_tools"] == ["Bash"]

    captured.clear()
    asyncio.run(
        provider._send_via_sdk(
            session,
            "写代码",
            cwd=str(tmp_path),
            context_pack={"disallowed_tools": []},
        )
    )
    assert captured["disallowed_tools"] is None
