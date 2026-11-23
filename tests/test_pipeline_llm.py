from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sortie_deck.llm import LlmClient
from sortie_deck.parallel import collapse_to_waves
from sortie_deck.pipeline_llm import plan_pipeline_llm, plan_pipeline_smart


@pytest.mark.asyncio
async def test_plan_pipeline_smart_heuristic_default():
    proposal = await plan_pipeline_smart(title="typo", brief="一行代码改文案", use_llm=False)
    assert proposal["planner"] == "heuristic"
    assert proposal["status"] == "pending"
    assert proposal["stages"][0]["id"] == "intake"


@pytest.mark.asyncio
async def test_plan_pipeline_llm_uses_client(monkeypatch):
    client = LlmClient(api_key="test-key")
    stages = [
        {"id": "intake", "label": "Intake"},
        {"id": "eng_implement", "label": "Eng"},
        {"id": "qa_verify", "label": "QA"},
        {"id": "deploy_preview", "label": "Deploy"},
        {"id": "done", "label": "Done"},
    ]

    async def fake_json(**kwargs):
        return {
            "track_hint": "express",
            "rationale": "LLM says express",
            "stages": stages,
            "confidence": 0.7,
        }

    monkeypatch.setattr(client, "chat_json", fake_json)
    proposal = await plan_pipeline_llm(title="hot", brief="hotfix", client=client)
    assert proposal is not None
    assert proposal["planner"] == "llm"
    assert proposal["track_hint"] == "express"
    assert proposal["stages"][0]["id"] == "intake"


@pytest.mark.asyncio
async def test_plan_pipeline_smart_falls_back_when_llm_fails(monkeypatch):
    client = LlmClient(api_key="test-key", cli_fallback=False)
    monkeypatch.setattr(client, "chat_json", AsyncMock(side_effect=RuntimeError("boom")))
    proposal = await plan_pipeline_smart(
        title="x", brief="一行改文案", use_llm=True, client=client
    )
    assert proposal["planner"] == "heuristic"


@pytest.mark.asyncio
@pytest.mark.parametrize(("requested", "expected"), [("auto", "full"), ("standard", "standard")])
async def test_planner_can_infer_parallel_work_from_user_goals(monkeypatch, requested, expected):
    client = LlmClient(api_key="test-key")
    analyze = AsyncMock(return_value={
        "task_kind": "delivery",
        "rationale": "申报、审核和记录保存可分别开发，再联调。",
        "parallel_builds": ["eng_implement", "eng_web", "eng_backend"],
        "stages": [{"id": "intake"}, {"id": "done"}],
    })
    monkeypatch.setattr(client, "chat_json", analyze)

    plan = await plan_pipeline_smart(
        brief="让旅客在手机上申报，主管在电脑审批，结果通过邮件通知并保存记录",
        tier=requested, use_llm=True, client=client,
    )

    analyze.assert_awaited_once()
    assert plan["planner"] == "llm" and plan["tier"] == expected
    wave = next(w for w in collapse_to_waves(plan["stages"]) if w.get("group") == "eng_delivery")
    assert {
        b["id"]: [s["id"] for s in b["members"]] for b in wave["branches"]
    } == {
        "client": ["eng_implement", "eng_selftest"],
        "web": ["eng_web", "eng_web_selftest"],
        "backend": ["eng_backend", "eng_backend_selftest"],
    }
    join = next(s for s in plan["stages"] if s["id"] == "eng_integrate")
    assert set(join["depends_on"]) == {"eng_selftest", "eng_web_selftest", "eng_backend_selftest"}


@pytest.mark.asyncio
async def test_planners_serial_decision_takes_precedence_over_platform_mentions(monkeypatch):
    client = LlmClient(api_key="test-key")
    analyze = AsyncMock(return_value={
        "parallel_builds": [],
        "stages": [
            {"id": "intake"},
            {"id": "eng_implement", "parallel_group": "eng_delivery", "branch": "web"},
            {"id": "eng_selftest", "parallel_group": "eng_delivery", "branch": "web"},
            {"id": "done"},
        ],
    })
    monkeypatch.setattr(client, "chat_json", analyze)

    plan = await plan_pipeline_smart(
        brief="前端、客户端和服务端的使用情况都要考虑，这次交付一处修复",
        tier="standard", use_llm=True, client=client,
    )

    analyze.assert_awaited_once()
    assert plan["planner"] == "llm" and not plan["parallel_delivery"]
    assert all(w["kind"] == "single" for w in collapse_to_waves(plan["stages"]))


@pytest.mark.asyncio
async def test_full_tier_keeps_required_specialist_builds_in_planned_serial_order(monkeypatch):
    client = LlmClient(api_key="test-key")
    stage_ids = ["intake", "eng_backend", "eng_backend_selftest", "eng_web", "eng_web_selftest", "done"]
    monkeypatch.setattr(client, "chat_json", AsyncMock(return_value={
        "task_kind": "delivery",
        "parallel_builds": [],
        "stages": [{"id": sid} for sid in stage_ids],
    }))

    plan = await plan_pipeline_smart(
        brief="先交付新接口，验收后再切换页面", tier="full", use_llm=True, client=client,
    )

    assert plan["planner"] == "llm" and not plan["parallel_delivery"]
    assert [stage["id"] for stage in plan["stages"]] == stage_ids


@pytest.mark.asyncio
async def test_propose_loadout_llm(monkeypatch):
    from sortie_deck.pipeline_llm import propose_loadout_llm

    client = LlmClient(api_key="test-key", cli_fallback=False)

    async def fake_json(**kwargs):
        return {
            "needed_slots": ["product", "eng_web", "qa", "deploy"],
            "rationale": "web portal",
        }

    monkeypatch.setattr(client, "chat_json", fake_json)
    out = await propose_loadout_llm(
        brief="门户预发",
        stages=[{"id": "eng_web", "role": "eng_web"}],
        client=client,
    )
    assert out is not None
    assert out["needed_slots"] == ["product", "eng_web", "qa", "deploy"]
    assert out["planner"] == "llm"


def test_resolve_coding_executor_explicit():
    from sortie_deck.llm import resolve_coding_executor

    assert resolve_coding_executor("mock") == "mock"
    assert resolve_coding_executor("claude_code") == "claude_code"
    assert resolve_coding_executor("cursor_cli") == "cursor_cli"
