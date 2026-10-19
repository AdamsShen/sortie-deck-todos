from __future__ import annotations

import asyncio

import pytest

from sortie_deck.contracts import REQUIRED
from sortie_deck.harness_providers import HarnessStageExecutor
from sortie_deck.knowledge import CreateKnowledgeRequest, KnowledgeStore
from sortie_deck.knowledge_gateway import KnowledgeGateway, gateway_from_settings
from sortie_deck.models import (
    CreateInitiativeRequest,
    HitlAction,
    HitlDecision,
    RoleId,
    StageContext,
)
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.pipeline import catalog_by_id, stage_catalog
from sortie_deck.settings import Settings, settings


@pytest.fixture
def iso_settings(tmp_path, monkeypatch):
    """Isolate the global settings data dir (knowledge/missions under tmp)."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    # 强制本地后端：单测不应依赖真实 WeKnora 实例（.env 可能配置了 weknora）
    monkeypatch.setattr(settings, "kb_backend", "local")
    return settings


def _ctx(initiative_id: str = "ini_kb1", stage: str = "eng_implement") -> StageContext:
    return StageContext(
        initiative_id=initiative_id,
        thread_id="thr_kb",
        stage=stage,
        role=RoleId.ENG,
        brief="Build notification center",
        artifact_dir="/tmp/artifacts",
        harness="mock",
    )


def _gateway(tmp_path) -> KnowledgeGateway:
    return gateway_from_settings(settings, KnowledgeStore(tmp_path / "knowledge.json"))


# --- tech design stage ---------------------------------------------------------


def test_tech_design_in_catalog_and_templates():
    ids = [s["id"] for s in stage_catalog()]
    # overall eng tech design is gone
    assert "eng_tech_design" not in ids
    # per-role tech design stages exist, each owned by its dev role
    catalog = catalog_by_id()
    for role in ("eng_backend", "eng_web", "eng_ios", "eng_android", "eng_agent"):
        td = f"{role}_tech_design"
        assert td in ids
        assert catalog[td]["role"] == role
    # track templates no longer carry the overall stage
    from sortie_deck.templates import load_pipeline_template

    for track in ("default", "standard"):
        names = [s["name"] for s in load_pipeline_template(track)["stages"]]
        assert "eng_tech_design" not in names


def test_tech_design_contract_requires_tech_design_md():
    for role in ("eng_backend", "eng_web", "eng_ios", "eng_android", "eng_agent"):
        assert REQUIRED.get(f"{role}_tech_design") == ["tech_design.md"]


def test_mock_tech_design_executor_produces_contract_artifact():
    from sortie_deck.plugins.mock import MockTechDesignExecutor

    ctx = _ctx(stage="eng_backend_tech_design")
    ctx.upstream_artifacts["prd.md"] = "# PRD\nBuild a notification center."
    result = asyncio.run(MockTechDesignExecutor().run(ctx))
    assert result.status == "ok"
    assert "tech_design.md" in result.artifacts
    assert "notification center" in result.artifacts["tech_design.md"]


def test_harness_executor_tech_design_contract(iso_settings):
    result = asyncio.run(
        HarnessStageExecutor().run(_ctx(stage="eng_backend_tech_design", initiative_id="ini_kb2"))
    )
    assert result.status == "ok"
    assert "tech_design.md" in result.artifacts


# --- mission namespace: retrieve -------------------------------------------------


def test_mission_namespace_priority_and_isolation(tmp_path, iso_settings):
    gw = _gateway(tmp_path)
    ini_a, ini_b = "ini_aaaa", "ini_bbbb"

    # Seed mission-scoped knowledge for ini_a
    gw.index_mission_artifact(
        initiative_id=ini_a,
        stage="product_prd",
        kind="prd.md",
        title="Mission A · product_prd · prd.md",
        content="# PRD\nZebra quota unique to mission A.",
    )
    # Global library entry
    gw.store.create(
        CreateKnowledgeRequest(
            title="Global Runbook",
            summary="team-wide deploy runbook",
            body="Deploy steps: zebra quota general guidance.",
            tags=["runbook"],
            category="runbook",
        ),
        author_id="usr_x",
        author_name="X",
    )

    hits_a = gw.retrieve("zebra quota", limit=5, initiative_id=ini_a)
    assert hits_a, "mission hit expected"
    assert (hits_a[0].meta or {}).get("mission") == ini_a
    assert "unique to mission A" in hits_a[0].content

    hits_b = gw.retrieve("zebra quota", limit=5, initiative_id=ini_b)
    assert all((h.meta or {}).get("mission") != ini_a for h in hits_b)

    # No initiative filter → global search unaffected
    hits_global = gw.retrieve("deploy runbook", limit=5)
    assert any(h.title == "Global Runbook" for h in hits_global)


def test_mission_falls_back_to_global_when_namespace_empty(tmp_path, iso_settings):
    gw = _gateway(tmp_path)
    gw.store.create(
        CreateKnowledgeRequest(
            title="Shared Lore",
            summary="legacy payments flow",
            body="Payments flow details.",
            tags=["lore"],
            category="lore",
        ),
        author_id="usr_x",
        author_name="X",
    )
    hits = gw.retrieve("payments flow", limit=5, initiative_id="ini_empty")
    assert hits and "Payments flow details." in hits[0].content


# --- mission namespace: writeback -------------------------------------------------


def test_index_mission_artifact_idempotent_and_disabled(tmp_path, iso_settings, monkeypatch):
    gw = _gateway(tmp_path)
    first = gw.index_mission_artifact(
        initiative_id="ini_idem",
        stage="eng_backend_tech_design",
        kind="tech_design.md",
        title="Ini Idem · eng_tech_design · tech_design.md",
        content="# Tech design\nApproach A.",
    )
    assert first is not None
    assert "ini:ini_idem" in first.tags
    again = gw.index_mission_artifact(
        initiative_id="ini_idem",
        stage="eng_backend_tech_design",
        kind="tech_design.md",
        title="Ini Idem · eng_tech_design · tech_design.md",
        content="# Tech design\nApproach A.",
    )
    assert again is not None and again.id == first.id
    assert len([d for d in gw.store.list() if d.title == first.title]) == 1

    monkeypatch.setattr(settings, "kb_mission_index", False)
    assert (
        gw.index_mission_artifact(
            initiative_id="ini_off",
            stage="qa_verify",
            kind="handoff.md",
            title="Off",
            content="x",
        )
        is None
    )


def test_mission_folder_prefix(iso_settings):
    gw = _gateway(iso_settings.data_dir / "kb")
    assert gw.mission_folder("ini_x") == "missions/ini_x"


@pytest.mark.asyncio
async def test_orchestrator_stage_artifacts_auto_indexed(tmp_path):
    cfg = Settings(data_dir=tmp_path / "data", deploy_executor="mock")
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        gw = gateway_from_settings(cfg, KnowledgeStore(cfg.knowledge_db))
        ini = await orch.create_initiative(
            CreateInitiativeRequest(
                title="KB writeback",
                brief="Build chat widget",
                coding_executor="mock",
                creator_name="Ada",
                harness="mock",
                auto_start=True,
            )
        )
        for _ in range(300):
            await asyncio.sleep(0.1)
            ini = orch.get(ini.id)
            assert ini
            if ini.status.value == "done":
                break
            if ini.status.value == "waiting_hitl":
                ini = await orch.submit_hitl(ini.id, HitlDecision(action=HitlAction.APPROVE))
        assert ini.status.value == "done"
        indexed = set((ini.meta or {}).get("kb_indexed") or [])
        assert any(s.startswith("product_prd:") for s in indexed)
        mission_docs = [d for d in gw.store.list() if f"ini:{ini.id}" in d.tags]
        assert mission_docs, "mission namespace should contain auto-indexed artifacts"
        assert any("prd.md" in d.title for d in mission_docs)
    finally:
        await orch.shutdown()
