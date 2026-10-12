from __future__ import annotations

import asyncio

import pytest

from sortie_deck.harness_providers import (
    HARNESS_NAMES,
    HarnessStageExecutor,
    build_harness_registry,
    is_valid_harness,
    reset_harness_registry,
)
from sortie_deck.mission_memory import (
    MissionMemoryStore,
    estimate_tokens,
    reset_mission_memory,
)
from sortie_deck.models import (
    CODING_ROLE_VALUES,
    ConfirmPipelineRequest,
    CreateInitiativeRequest,
    HarnessLockedError,
    HitlAction,
    HitlDecision,
    Initiative,
    RoleId,
    StageContext,
    UnknownHarnessError,
)
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.settings import Settings


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """Isolate the global settings data dir so mission files land under tmp."""
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    reset_mission_memory()
    yield settings
    reset_mission_memory()


def _ctx(stage: str = "product_prd", role: str = "product", harness: str = "mock") -> StageContext:
    return StageContext(
        initiative_id="ini_test",
        thread_id="thr_test",
        stage=stage,
        role=RoleId(role),
        brief="Build a notification center",
        artifact_dir="/tmp/artifacts",
        harness=harness,
    )


# --- registry ------------------------------------------------------------------


def test_registry_has_all_builtin_providers():
    reset_harness_registry()
    registry = build_harness_registry()
    assert set(registry.names()) >= set(HARNESS_NAMES)
    assert registry.probe("mock").ok
    assert not is_valid_harness("not_a_harness")
    assert is_valid_harness("claude_code")


def test_probe_ttl_cache():
    reset_harness_registry()
    registry = build_harness_registry()
    first = registry.probe("mock")
    again = registry.probe("mock")
    assert first.status == again.status
    registry.invalidate("mock")


# --- mission memory ---------------------------------------------------------------


def test_mission_memory_files_and_bootstrap(tmp_path):
    mm = MissionMemoryStore(tmp_path / "missions")
    mm.bind_context_file("ini_1", "brief.md", "# Brief\nBuild chat")
    mm.bind_context_file("ini_1", "plan.md", "# Plan\n1. intake")
    mm.append_memory("ini_1", "Stage intake done via mock harness.")
    mm.record_turn(
        "ini_1",
        "hs_abc",
        stage="intake",
        role="ops",
        prompt="do intake",
        reply="intake finished",
        provider="mock",
    )
    assert "Stage intake done" in mm.load_memory_md("ini_1")
    assert mm.list_context_files("ini_1").keys() >= {"brief.md", "plan.md"}
    assert len(mm.load_session_log("ini_1")) == 1
    ini = Initiative(id="ini_1", title="t", brief="b")
    pack = asyncio.run(mm.bootstrap_context(ini))
    assert "Stage intake done" in pack.memory_md
    assert "brief.md" in pack.context_files
    assert pack.token_estimate == estimate_tokens(pack.render())


def test_mission_memory_summarize_degrades_without_llm(tmp_path):
    mm = MissionMemoryStore(tmp_path / "missions")
    mm.write_memory_md("ini_1", "# Rolling memory\n\n" + ("line of decisions.\n" * 500))
    summary = asyncio.run(mm.summarize_from_text("ini_1", mm.load_memory_md("ini_1")))
    assert summary  # degraded compaction still yields content
    reloaded = mm.load_memory_md("ini_1")
    assert "auto-summarized" in reloaded


# --- executor: routing + contract + fallback ---------------------------------------


def test_executor_mock_harness_satisfies_contract(tmp_settings):
    executor = HarnessStageExecutor()
    result = asyncio.run(executor.run(_ctx(stage="product_prd", role="product", harness="mock")))
    assert result.status == "ok"
    assert "prd.md" in result.artifacts
    assert "acceptance.json" in result.artifacts
    assert result.meta.get("harness") == "mock"
    assert result.meta.get("session_id")


def test_executor_eng_stage_emits_coding_artifacts(tmp_settings):
    executor = HarnessStageExecutor()
    result = asyncio.run(executor.run(_ctx(stage="eng_implement", role="eng", harness="mock")))
    assert result.status == "ok"
    for name in ("implementation.md", "tasks.json", "diff.patch"):
        assert name in result.artifacts


def test_executor_falls_back_when_provider_missing(tmp_settings, monkeypatch):
    reset_harness_registry()
    from sortie_deck.harness_providers.base import ProbeStatus, build_harness_registry

    registry = build_harness_registry()
    monkeypatch.setattr(
        "sortie_deck.harness_providers.base.get_harness_registry", lambda: registry
    )
    monkeypatch.setattr(registry, "probe", lambda name, refresh=False: ProbeStatus(
        status="missing", detail="forced missing"
    ))
    executor = HarnessStageExecutor()
    result = asyncio.run(
        executor.run(_ctx(stage="product_prd", role="product", harness="claude_code"))
    )
    assert result.status == "ok"  # legacy mock_product picked up the slack
    assert result.meta.get("harness_fallback") == "mock_product"
    assert result.meta.get("harness") == "claude_code"


def test_executor_fails_hard_when_fallback_disabled(tmp_settings, monkeypatch):
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "harness_fallback", False)
    executor = HarnessStageExecutor()
    result = asyncio.run(
        executor.run(_ctx(stage="intake", role="ops", harness="opencode"))
    )
    assert result.status == "fail"
    assert result.meta.get("fallback") is False


def test_legacy_executor_name_mapping():
    assert HarnessStageExecutor and True
    from sortie_deck.harness_providers.executor import _legacy_executor_name

    assert _legacy_executor_name(_ctx(stage="intake", role="ops")) == "mock_intake"
    assert _legacy_executor_name(_ctx(stage="eng_implement", role="eng")) == "mock_eng"
    assert _legacy_executor_name(_ctx(stage="qa_verify", role="qa")) == "mock_qa"
    assert _legacy_executor_name(_ctx(stage="qa_cases", role="qa")) == "mock_qa_cases"
    assert _legacy_executor_name(_ctx(stage="unknown_stage", role="product")) == "mock_intake"
    assert "eng" in CODING_ROLE_VALUES  # sanity: coding roles present


# --- graph routing -------------------------------------------------------------------


def test_graph_routes_all_stages_through_harness():
    from sortie_deck.graph import _executor_name

    state = {"harness": "claude_code", "harness_locked": True}
    cfg: dict = {}
    for stage, role in (
        ("intake", "ops"),
        ("product_prd", "product"),
        ("eng_implement", "eng"),
        ("qa_verify", "qa"),
        ("deploy_preview", "deploy"),
    ):
        assert _executor_name(stage, role, state, cfg) == "harness_stage"
    # no harness → legacy routing intact
    assert _executor_name("eng_implement", "eng", {"coding_executor": "mock"}, cfg) == "mock_eng"


# --- orchestrator lock & HITL switch --------------------------------------------------


@pytest.mark.asyncio
async def test_harness_locked_on_start_and_draft_update(tmp_path):
    cfg = Settings(data_dir=tmp_path / "data", deploy_executor="mock")
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_initiative(
            CreateInitiativeRequest(
                title="Harness lock",
                brief="Build chat",
                coding_executor="mock",
                creator_name="Ada",
                harness="mock",
            )
        )
        assert ini.harness == "mock"
        assert ini.harness_locked is False

        # draft: harness may change
        ini = await orch.update_harness(ini.id, "mock")
        assert ini.harness == "mock"

        ini = await orch.confirm_pipeline(ini.id, ConfirmPipelineRequest(), author_name="Ada")
        ini = await orch.start_pipeline(ini.id, author_name="Ada")
        assert ini.harness_locked is True
        assert ini.meta.get("harness_locked") is True
        assert ini.meta.get("harness") == "mock"

        with pytest.raises(HarnessLockedError):
            await orch.update_harness(ini.id, "mock")
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
async def test_unknown_harness_rejected_at_creation(tmp_path):
    cfg = Settings(data_dir=tmp_path / "data")
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        with pytest.raises(UnknownHarnessError):
            await orch.create_initiative(
                CreateInitiativeRequest(
                    title="Bad harness",
                    brief="x",
                    coding_executor="mock",
                    harness="not_a_harness",
                )
            )
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
async def test_switch_harness_via_hitl_updates_mission(tmp_path):
    cfg = Settings(data_dir=tmp_path / "data", deploy_executor="mock")
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_initiative(
            CreateInitiativeRequest(
                title="HITL switch",
                brief="Build chat",
                coding_executor="mock",
                creator_name="Ada",
                harness="mock",
                auto_start=True,
            )
        )
        ini = await orch.switch_harness_via_hitl(
            ini, HitlDecision(action=HitlAction.SWITCH_HARNESS, harness="mock")
        )
        assert ini.harness == "mock"
        assert ini.meta.get("harness") == "mock"
        assert ini.harness_locked is True
        with pytest.raises(UnknownHarnessError):
            await orch.switch_harness_via_hitl(
                ini, HitlDecision(action=HitlAction.SWITCH_HARNESS, harness="bogus")
            )
    finally:
        await orch.shutdown()


# --- memory backends --------------------------------------------------------------


def test_composite_memory_merges_mem0(tmp_path):
    from sortie_deck.memory import (
        CompositeMemory,
        CreateMemoryRequest,
        Mem0Memory,
        MemoryStore,
    )

    class FakeMem0(Mem0Memory):
        def __init__(self) -> None:
            super().__init__(base_url="http://mem0.invalid", api_key=None, user_prefix="t")
            self.seeded = ["Customer wants SSO first."]

        def add(self, content, *, initiative_id, tags=None):
            self.seeded.append(content)
            return True

        def search(self, query, *, initiative_id, limit=8):
            return [m for m in self.seeded if "SSO" in m][:limit]

    local = MemoryStore(tmp_path / "memory.json")
    composite = CompositeMemory(local=local, mem0=FakeMem0())
    entry = composite.create(
        CreateMemoryRequest(
            scope="mission", scope_id="ini_9", kind="fact", content="Mission memory entry."
        ),
        author_id="usr_a",
        author_name="Ada",
    )
    assert entry.id
    block = composite.context_block(mission_id="ini_9")
    assert "Mission memory entry." in block
    assert "Shared memory (mem0):" in block
    assert "Customer wants SSO first." in block


def test_build_memory_store_local_by_default(tmp_path):
    from sortie_deck.memory import MemoryStore, build_memory_store

    store = build_memory_store(tmp_path / "memory.json")
    assert isinstance(store, MemoryStore)
    reset_mission_memory()
    reset_harness_registry()
