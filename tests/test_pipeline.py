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
    )
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_and_start(
            CreateInitiativeRequest(
                title="Loop demo",
                brief="Build notification center",
                coding_executor="mock",
                pipeline_track="default",
                auto_start=True,
                auto_confirm_pipeline=True,
            )
        )
        for _ in range(30):
            await asyncio.sleep(0.1)
            ini = orch.get(ini.id)
            assert ini
            if ini.status.value == "waiting_hitl":
                break
        assert ini.pending_hitl is not None
        assert ini.pending_hitl.stage == "product_prd"

        ini = await orch.submit_hitl(ini.id, HitlDecision(action=HitlAction.APPROVE))
        for _ in range(40):
            await asyncio.sleep(0.1)
            ini = orch.get(ini.id)
            assert ini
            if ini.status.value == "waiting_hitl":
                break
        assert ini.pending_hitl is not None
        # PRD gate is followed by the new eng_tech_design review gate
        assert ini.pending_hitl.stage == "eng_tech_design"

        ini = await orch.submit_hitl(ini.id, HitlDecision(action=HitlAction.APPROVE))
        for _ in range(40):
            await asyncio.sleep(0.1)
            ini = orch.get(ini.id)
            assert ini
            if ini.status.value == "waiting_hitl":
                break
        assert ini.pending_hitl is not None
        assert ini.pending_hitl.stage in {"design_ui", "qa_verify", "deploy_preview"}

        for _ in range(10):
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
