"""P-1 回归：没有可用 LLM 时，任务必须能正常推进而不是卡在 intake。

背景：stage_consult / manager / agent_forge 曾硬编码 ``LlmClient(cli_fallback=True)``，
把 ``TDT_LLM_CLI_FALLBACK=0`` 绕了过去；于是每个阶段都会 shell 调 ``claude -p``，
CLI 挂住后按 60s 超时返回，阶段被拖死，测试与端到端验收全部超时。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from sortie_deck.llm import LlmClient
from sortie_deck.models import CreateInitiativeRequest
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.settings import Settings


def test_cli_fallback_switch_is_respected(monkeypatch):
    monkeypatch.setenv("TDT_LLM_CLI_FALLBACK", "0")
    monkeypatch.setenv("TDT_LLM_API_KEY", "")
    client = LlmClient()
    assert client.cli_fallback is False
    # 没有 key 且关闭 CLI 回退 → 完全不可用，调用方必须走降级
    assert client.enabled is False


def test_cli_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv("TDT_LLM_CLI_TIMEOUT", "2")
    assert LlmClient().cli_timeout == 2.0


@pytest.mark.asyncio
async def test_mission_progresses_without_llm(tmp_path, monkeypatch):
    """无 key、无 CLI 回退时，mock 管线必须快速到达第一个门禁。"""
    monkeypatch.setenv("TDT_LLM_CLI_FALLBACK", "0")
    monkeypatch.setenv("TDT_LLM_API_KEY", "")

    from sortie_deck import llm as llm_mod

    async def _forbidden(*args, **kwargs):  # pragma: no cover — 被调用即失败
        raise AssertionError("无 LLM 配置时不应拉起 claude CLI")

    monkeypatch.setattr(llm_mod.LlmClient, "_chat_claude_cli", _forbidden)

    cfg = Settings(
        data_dir=tmp_path / "data",
        deploy_executor="mock",
        kb_backend="local",
        pipeline_llm=False,
        room_llm=False,
    )
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_and_start(
            CreateInitiativeRequest(
                title="offline run",
                brief="Build notification center",
                coding_executor="mock",
                harness="mock",
                auto_start=True,
                auto_confirm_pipeline=True,
            )
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            current = orch.get(ini.id)
            assert current is not None
            ini = current
            if ini.status.value in {"waiting_hitl", "done", "failed", "stopped"}:
                break

        assert ini.status.value in {"waiting_hitl", "done"}, [
            event.message for event in ini.timeline[-3:]
        ]
    finally:
        await orch.shutdown()
