from __future__ import annotations

import pytest

from sortie_deck.materials import (
    MATERIALS_HEADER,
    compose_brief,
    pinned_token_estimate,
)
from sortie_deck.models import CreateInitiativeRequest, ExternalMaterial
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.settings import Settings


def test_compose_brief_only_pins_pinned():
    mats = [
        ExternalMaterial(name="prd.md", kind="prd", pinned=True, text_excerpt="需求A"),
        ExternalMaterial(name="plan.pdf", kind="plan", pinned=False, text_excerpt="计划B"),
    ]
    out = compose_brief("目标", mats)
    assert MATERIALS_HEADER in out
    assert "需求A" in out
    # unpinned materials are archived, never injected
    assert "计划B" not in out


def test_compose_brief_is_idempotent():
    mats = [ExternalMaterial(name="prd.md", kind="prd", pinned=True, text_excerpt="需求A")]
    once = compose_brief("目标", mats)
    assert compose_brief(once, mats) == once


def test_pinned_token_estimate_ignores_unpinned():
    mats = [
        ExternalMaterial(name="a.md", pinned=True, text_excerpt="abcd"),
        ExternalMaterial(name="b.md", pinned=False, text_excerpt="x" * 400),
    ]
    assert pinned_token_estimate(mats) == 1


@pytest.mark.asyncio
async def test_create_initiative_folds_pinned_materials(tmp_path):
    cfg = Settings(data_dir=tmp_path / "data", deploy_executor="mock", pipeline_llm=False)
    orch = Orchestrator(cfg)
    try:
        ini = await orch.create_initiative(
            CreateInitiativeRequest(
                title="材料测试",
                brief="原始目标",
                coding_executor="mock",
                materials=[
                    ExternalMaterial(
                        name="prd.md", kind="prd", pinned=True, text_excerpt="钉住的需求"
                    ),
                    ExternalMaterial(
                        name="plan.md", kind="plan", pinned=False, text_excerpt="未钉住的计划"
                    ),
                ],
            )
        )
        assert MATERIALS_HEADER in ini.brief
        assert "钉住的需求" in ini.brief
        assert "未钉住的计划" not in ini.brief
        assert ini.meta["brief_base"] == "原始目标"
        assert len(ini.materials) == 2

        # unpinning everything reverts the brief to the raw user brief
        ini = orch.set_materials(ini.id, [])
        assert ini.brief == "原始目标"
        assert ini.materials == []
    finally:
        await orch.shutdown()
