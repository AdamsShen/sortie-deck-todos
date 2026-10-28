from __future__ import annotations

import asyncio

import pytest

from sortie_deck.models import (
    ConfirmPipelineRequest,
    CreateInitiativeRequest,
    HitlAction,
    HitlDecision,
    InitiativeStatus,
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

        for _ in range(400):
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


class _SystemExitGraph:
    """替身图：第一次迭代即抛 SystemExit，模拟环境注入的强制退出。"""

    async def astream(self, payload, config, stream_mode=None):
        raise SystemExit(1)
        yield  # pragma: no cover — 让函数保持异步生成器签名

    async def aget_state(self, config):  # pragma: no cover — 异常路径不会走到
        raise AssertionError("aget_state 不应被调用")


@pytest.mark.asyncio
async def test_system_exit_marks_failed_instead_of_killing_process(tmp_path, monkeypatch):
    """回归 2026-10-27 事故：SystemExit 只能把任务置 failed，不得上抛杀死进程。

    SystemExit 继承 BaseException，原来的 `except Exception` 拦不住，任务会永远
    停在 running（UI 点启动也无反应）。
    """
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
        ini = await orch.create_initiative(
            CreateInitiativeRequest(
                title="SystemExit 兜底",
                brief="验证清理异常不会打死进程",
                coding_executor="mock",
                harness="mock",
                creator_name="Ada",
            )
        )
        ini.status = InitiativeStatus.RUNNING
        orch.store.upsert(ini)
        monkeypatch.setattr(orch, "_graph_for", lambda _ini: _SystemExitGraph())

        returned = await orch._run_until_interrupt(ini.id)

        assert returned.status == InitiativeStatus.FAILED
        stored = orch.store.get(ini.id)
        assert stored is not None
        assert stored.status == InitiativeStatus.FAILED
        assert any(e.kind == "error" for e in stored.timeline)
    finally:
        await orch.shutdown()
