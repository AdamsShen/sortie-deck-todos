from __future__ import annotations

import asyncio

import pytest

from sortie_deck.models import (
    ConfirmPipelineRequest,
    CreateInitiativeRequest,
    HitlAction,
    HitlDecision,
    PipelineNotConfirmedError,
    PipelineStageSpec,
    UpdatePipelineRequest,
)
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.settings import Settings


@pytest.mark.asyncio
async def test_create_edit_confirm_start_hitl_until_done(tmp_path):
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
        ini = await orch.create_initiative(
            CreateInitiativeRequest(
                title="Flow demo",
                brief="Build notification center",
                coding_executor="mock",
                # 显式 mock harness：默认 harness 会拉起真实 CLI，测试只需验证图与门禁
                harness="mock",
                creator_name="Ada",
            )
        )
        assert ini.meta.get("pipeline_confirmed") is False

        specs = [
            PipelineStageSpec(**{k: s[k] for k in ("id", "label", "role", "hitl_after") if k in s})
            for s in ini.meta["pipeline_specs"]
        ]
        edited = [s for s in specs if s.id != "qa_cases"]
        ini = await orch.update_pipeline(
            ini.id,
            UpdatePipelineRequest(stages=edited, confirm=False),
            author_name="Ada",
        )
        assert ini.meta["pipeline_confirmed"] is False
        assert f"custom:{ini.id}" not in orch.graphs

        ini = await orch.confirm_pipeline(
            ini.id, ConfirmPipelineRequest(), author_name="Ada"
        )
        assert ini.meta["pipeline_confirmed"] is True
        assert f"custom:{ini.id}" in orch.graphs

        ini = await orch.start_pipeline(ini.id, author_name="Ada")
        assert ini.status.value in {"running", "waiting_hitl"}

        for _ in range(140):
            await asyncio.sleep(0.1)
            ini = orch.get(ini.id)
            assert ini
            if ini.status.value == "waiting_hitl":
                break
        assert ini.pending_hitl is not None

        while ini and ini.status.value != "done":
            if ini.status.value == "waiting_hitl":
                ini = await orch.submit_hitl(ini.id, HitlDecision(action=HitlAction.APPROVE))
            else:
                await asyncio.sleep(0.1)
                ini = orch.get(ini.id)

        assert ini
        assert ini.status.value == "done"
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
async def test_start_before_confirm_raises_structured_error(tmp_path):
    cfg = Settings(data_dir=tmp_path / "data", deploy_executor="mock")
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_initiative(
            CreateInitiativeRequest(title="x", brief="y", creator_name="Ada")
        )
        with pytest.raises(PipelineNotConfirmedError) as err:
            await orch.start_pipeline(ini.id)
        assert err.value.code == "pipeline_not_confirmed"
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
async def test_startup_recompiles_confirmed_custom_graph(tmp_path):
    cfg = Settings(data_dir=tmp_path / "data", deploy_executor="mock")
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_initiative(
            CreateInitiativeRequest(
                title="Persist graph",
                brief="一行改文案",
                creator_name="Ada",
            )
        )
        ini = await orch.confirm_pipeline(ini.id, author_name="Ada")
        initiative_id = ini.id
        assert f"custom:{initiative_id}" in orch.graphs
    finally:
        await orch.shutdown()

    orch2 = Orchestrator(cfg)
    await orch2.startup()
    try:
        assert f"custom:{initiative_id}" in orch2.graphs
        started = await orch2.start_pipeline(initiative_id, author_name="Ada")
        assert started.status.value in {"running", "waiting_hitl", "done"}
    finally:
        await orch2.shutdown()
