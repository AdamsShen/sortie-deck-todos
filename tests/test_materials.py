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


def test_long_pinned_material_becomes_path_pointer():
    """超过内联阈值的长文本只留路径指针，避免把每个阶段的 prompt 撑爆。"""
    mats = [
        ExternalMaterial(
            name="big.pdf",
            kind="prd",
            pinned=True,
            text_excerpt="x" * 5000,
            stored_path="files/mat_1__big.pdf",
            text_path="text/mat_1.md",
        )
    ]
    out = compose_brief("目标", mats)
    assert "按需读取 `input/text/mat_1.md`" in out
    assert "x" * 5000 not in out


def test_binary_pinned_material_only_leaves_pointer():
    mats = [
        ExternalMaterial(
            name="wireframe.png",
            kind="design",
            pinned=True,
            stored_path="files/mat_2__wireframe.png",
        )
    ]
    out = compose_brief("目标", mats)
    assert "input/files/mat_2__wireframe.png" in out


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


@pytest.mark.asyncio
async def test_brief_snapshot_lives_in_input_dir(tmp_path):
    """brief 只有一份：input/brief.md；memory/context 不再复制，避免两份漂移。"""
    cfg = Settings(data_dir=tmp_path / "data", deploy_executor="mock", pipeline_llm=False)
    orch = Orchestrator(cfg)
    try:
        ini = await orch.create_initiative(
            CreateInitiativeRequest(
                title="快照测试",
                brief="原始目标",
                coding_executor="mock",
                materials=[
                    ExternalMaterial(
                        name="prd.md", kind="prd", pinned=True, text_excerpt="钉住的需求"
                    )
                ],
            )
        )
        orch._bind_mission_context_files(ini)

        input_dir = cfg.data_dir / "missions" / ini.id / "input"
        brief_md = (input_dir / "brief.md").read_text(encoding="utf-8")
        assert "原始目标" in brief_md
        assert "钉住的需求" in brief_md
        # 不再往 memory/context 复制 brief
        assert not (cfg.data_dir / "missions" / ini.id / "memory" / "context" / "brief.md").exists()

        orch.set_materials(ini.id, [])
        assert (input_dir / "brief.md").read_text(encoding="utf-8").strip() == "原始目标"
    finally:
        await orch.shutdown()
