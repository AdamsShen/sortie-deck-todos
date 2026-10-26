from __future__ import annotations

import asyncio

import pytest

from sortie_deck.contracts import ContractError, validate_stage_artifacts
from sortie_deck.models import CreateInitiativeRequest, HitlAction, HitlDecision
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.settings import Settings


def test_validate_contract_ok():
    validate_stage_artifacts(
        "product_prd",
        {"prd.md": "# x", "acceptance.json": "{}"},
    )


def test_validate_contract_missing():
    with pytest.raises(ContractError):
        validate_stage_artifacts("product_prd", {"prd.md": "# x"})


@pytest.mark.asyncio
async def test_pipeline_hitl_and_qa_loop(tmp_path):
    cfg = Settings(
        data_dir=tmp_path / "data",
        deploy_executor="mock",
        # 测试不依赖外部 WeKnora / LLM：本地知识库 + 关掉 LLM 规划与房间讨论，
        # 阶段摘要与反思走规则回退，保证结果确定且不被网络耗时影响
        kb_backend="local",
        pipeline_llm=False,
        room_llm=False,
    )
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_and_start(
            CreateInitiativeRequest(
                title="Loop demo",
                brief="Build notification center",
                coding_executor="mock",
                # 显式 mock harness：默认 harness 会拉起真实 CLI，测试只需验证图与门禁
                harness="mock",
                pipeline_track="default",
                auto_start=True,
                auto_confirm_pipeline=True,
            )
        )
        for _ in range(120):
            await asyncio.sleep(0.1)
            ini = orch.get(ini.id)
            assert ini
            if ini.status.value == "waiting_hitl":
                break
        assert ini.pending_hitl is not None
        assert ini.pending_hitl.stage == "product_prd"

        ini = await orch.submit_hitl(ini.id, HitlDecision(action=HitlAction.APPROVE))
        for _ in range(140):
            await asyncio.sleep(0.1)
            ini = orch.get(ini.id)
            assert ini
            if ini.status.value == "waiting_hitl":
                break
        assert ini.pending_hitl is not None
        # Overall eng tech design removed; PRD gate is followed by the design gate
        assert ini.pending_hitl.stage == "design_ui"

        ini = await orch.submit_hitl(ini.id, HitlDecision(action=HitlAction.APPROVE))
        for _ in range(140):
            await asyncio.sleep(0.1)
            ini = orch.get(ini.id)
            assert ini
            if ini.status.value == "waiting_hitl":
                break
        assert ini.pending_hitl is not None
        assert ini.pending_hitl.stage in {"design_ui", "qa_verify", "deploy_preview"}

        for _ in range(40):
            ini = orch.get(ini.id)
            assert ini
            if ini.status.value == "done":
                break
            if ini.status.value != "waiting_hitl":
                await asyncio.sleep(0.1)
                continue
            ini = await orch.submit_hitl(ini.id, HitlDecision(action=HitlAction.APPROVE))

        ini = orch.get(ini.id)
        assert ini
        assert ini.status.value == "done"
        kinds = {a.kind for a in ini.artifacts}
        assert "prd.md" in kinds
        assert "design.md" in kinds
        assert "test_report.json" in kinds
        assert "deploy_result.json" in kinds
    finally:
        await orch.shutdown()
