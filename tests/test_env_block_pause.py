"""环境阻塞自动暂停与任务级权限透传（SDK 路径）。

回归目标：QA 判定 not_a_code_defect 的环境阻塞不得再打回 eng_implement，
且 HITL 提权必须真正进入 ClaudeAgentOptions.permission_mode。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

from sortie_deck import graph
from sortie_deck.harness_providers.base import HarnessSession
from sortie_deck.harness_providers.claude_code import ClaudeCodeProvider
from sortie_deck.harness_providers.executor import HarnessStageExecutor
from sortie_deck.models import RoleId, StageContext


def _result(kind: str | None = None, *, status: str = "fail") -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if kind:
        meta = {"failure_kind": kind, "failure_reason": "命令审批被阻断，无运行时证据"}
    return {"status": status, "message": "harness stage failed", "meta": meta}


def _state() -> dict[str, Any]:
    return {"initiative_id": "ini_test", "events": [], "failure_signatures": {}}


def test_env_block_pauses_on_first_occurrence(monkeypatch: Any) -> None:
    monkeypatch.setattr("sortie_deck.settings.settings.env_block_pause_enabled", True)
    st = _state()
    out = graph._apply_environment_block_guard(st, "qa_verify", _result("environment_blocked"))
    assert out is not None
    assert out["stopped"] is True
    assert out["status"] == "stopped"
    assert out["handoff_target"] is None
    assert "自动暂停" in out["events"][-1]["message"]
    assert "环境阻塞" in out["last_result"]["message"]


def test_auth_block_also_pauses(monkeypatch: Any) -> None:
    monkeypatch.setattr("sortie_deck.settings.settings.env_block_pause_enabled", True)
    out = graph._apply_environment_block_guard(_state(), "eng_implement", _result("auth_blocked"))
    assert out is not None and out["stopped"] is True


def test_unknown_failure_pauses_after_consecutive_limit(monkeypatch: Any) -> None:
    monkeypatch.setattr("sortie_deck.settings.settings.env_block_pause_enabled", True)
    monkeypatch.setattr("sortie_deck.settings.settings.max_consecutive_same_failure", 3)
    st = _state()
    result = _result()
    assert graph._apply_environment_block_guard(st, "eng_implement", result) is None
    assert graph._apply_environment_block_guard(st, "eng_implement", result) is None
    out = graph._apply_environment_block_guard(st, "eng_implement", result)
    assert out is not None
    assert out["stopped"] is True
    assert "连续 3 次" in out["events"][-1]["message"]


def test_successful_stage_resets_failure_signature(monkeypatch: Any) -> None:
    monkeypatch.setattr("sortie_deck.settings.settings.max_consecutive_same_failure", 3)
    st = _state()
    graph._apply_environment_block_guard(st, "eng_implement", _result())
    assert st["failure_signatures"]
    assert graph._apply_environment_block_guard(st, "eng_implement", _result(status="ok")) is None
    assert st["failure_signatures"] == {}


def test_env_block_pause_can_be_disabled(monkeypatch: Any) -> None:
    monkeypatch.setattr("sortie_deck.settings.settings.env_block_pause_enabled", False)
    monkeypatch.setattr("sortie_deck.settings.settings.max_consecutive_same_failure", 0)
    assert (
        graph._apply_environment_block_guard(_state(), "qa_verify", _result("environment_blocked"))
        is None
    )


def test_self_check_skips_environment_block() -> None:
    assert graph._needs_self_check("eng_implement", _result("environment_blocked")) is False
    assert graph._needs_self_check("eng_implement", _result()) is True


def test_gate_recommendation_exposes_blocked_reason() -> None:
    gate = graph._gate_recommendation(
        "qa_verify",
        {**_result("environment_blocked"), "qa_passed": False},
        "eng_implement",
    )
    assert "环境" in gate["blocked_reason"]
    assert gate["recommended_action"]["action"] == "reroute"


def test_context_pack_carries_resolved_permission_mode(monkeypatch: Any) -> None:
    monkeypatch.setattr("sortie_deck.settings.settings.coding_permission_mode", "bypassPermissions")

    async def _no_briefs(self: Any, ctx: Any, mm: Any) -> dict[str, str]:
        return {}

    async def _no_rag(self: Any, ctx: Any) -> str:
        return ""

    def _no_mcp(self: Any, ctx: Any) -> tuple[dict, list]:
        return {}, []

    class _NoOrchestrator:
        def get(self, _id: str) -> None:
            raise RuntimeError("no orchestrator in unit test")

    monkeypatch.setattr("sortie_deck.orchestrator.get_orchestrator", lambda: _NoOrchestrator())
    monkeypatch.setattr("sortie_deck.mission_memory.get_mission_memory", lambda: object())
    monkeypatch.setattr(HarnessStageExecutor, "_artifact_briefs", _no_briefs)
    monkeypatch.setattr(HarnessStageExecutor, "_rag_brief", _no_rag)
    monkeypatch.setattr(HarnessStageExecutor, "_mcp_servers", _no_mcp)

    ctx = StageContext(
        initiative_id="ini",
        thread_id="t",
        stage="eng_implement",
        role=RoleId.ENG,
        brief="x",
        artifact_dir="/tmp/arts",
        permissions={"coding_permission_mode": "bypassPermissions"},
        human_instruction="[sortie:coding_permission_mode=bypassPermissions]",
    )
    pack = asyncio.run(HarnessStageExecutor()._build_context_pack(ctx))
    assert pack["permission_mode"] == "bypassPermissions"
    assert pack["disallowed_tools"] == []


def _fake_sdk(monkeypatch: Any, captured: dict[str, Any]) -> None:
    class _FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    class _FakeMessage:
        pass

    async def _empty_query(**_kwargs: Any):
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


def test_sdk_uses_task_level_permission_mode(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr("sortie_deck.settings.settings.coding_permission_mode", "acceptEdits")
    captured: dict[str, Any] = {}
    _fake_sdk(monkeypatch, captured)
    provider = ClaudeCodeProvider()
    session = HarnessSession(id="hs_test", provider="claude_code")
    asyncio.run(
        provider._send_via_sdk(
            session,
            "写代码",
            cwd=str(tmp_path),
            context_pack={"permission_mode": "bypassPermissions"},
        )
    )
    assert captured["permission_mode"] == "bypassPermissions"


def test_sdk_falls_back_to_global_mode(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr("sortie_deck.settings.settings.coding_permission_mode", "acceptEdits")
    captured: dict[str, Any] = {}
    _fake_sdk(monkeypatch, captured)
    provider = ClaudeCodeProvider()
    session = HarnessSession(id="hs_test", provider="claude_code")
    asyncio.run(provider._send_via_sdk(session, "写代码", cwd=str(tmp_path), context_pack={}))
    assert captured["permission_mode"] == "acceptEdits"
