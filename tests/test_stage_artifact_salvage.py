"""超时/失败阶段的产物兜底回收与真实失败原因暴露。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sortie_deck.contracts import ContractError, StageContractError, validate_stage_artifacts
from sortie_deck.graph import _salvage_stage_artifacts, _validate_or_explain
from sortie_deck.models import CreateInitiativeRequest, StageResult
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.settings import Settings


def test_contract_error_carries_stage():
    with pytest.raises(StageContractError) as excinfo:
        validate_stage_artifacts("eng_backend_tech_design", {})
    assert excinfo.value.stage == "eng_backend_tech_design"
    # 仍是 ContractError 子类，既有调用方的捕获语义不变
    assert isinstance(excinfo.value, ContractError)


def test_validate_merges_executor_failure_reason():
    """超时的真实原因不能被 "missing artifacts" 掩盖。"""
    result = StageResult(
        status="fail",
        message="stage `eng_backend_tech_design` timed out after 1800s",
        artifacts={},
    )
    with pytest.raises(StageContractError) as excinfo:
        _validate_or_explain("eng_backend_tech_design", result)
    text = str(excinfo.value)
    assert "missing artifacts: tech_design.md" in text
    assert "timed out after 1800s" in text


def test_validate_keeps_plain_error_when_executor_ok():
    result = StageResult(status="ok", message="", artifacts={})
    with pytest.raises(StageContractError) as excinfo:
        _validate_or_explain("eng_backend_tech_design", result)
    assert "executor failure" not in str(excinfo.value)


def test_salvage_reads_top_level_artifact(tmp_path: Path):
    (tmp_path / "tech_design.md").write_text("# 后端技术方案", encoding="utf-8")
    found = _salvage_stage_artifacts("eng_backend_tech_design", tmp_path)
    assert found == {"tech_design.md": "# 后端技术方案"}


def test_salvage_reads_nested_artifact(tmp_path: Path):
    nested = tmp_path / "docs"
    nested.mkdir()
    (nested / "tech_design.md").write_text("# 方案", encoding="utf-8")
    assert "tech_design.md" in _salvage_stage_artifacts("eng_backend_tech_design", tmp_path)


def test_salvage_skips_empty_file_and_repo_dirs(tmp_path: Path):
    (tmp_path / "tech_design.md").write_text("  \n", encoding="utf-8")
    repo_file = tmp_path / "repos" / "svc" / "tech_design.md"
    repo_file.parent.mkdir(parents=True)
    repo_file.write_text("# 业务仓库里的同名文件", encoding="utf-8")
    assert _salvage_stage_artifacts("eng_backend_tech_design", tmp_path) == {}


def test_salvage_ignores_stage_without_contract(tmp_path: Path):
    (tmp_path / "tech_design.md").write_text("# 方案", encoding="utf-8")
    assert _salvage_stage_artifacts("done", tmp_path) == {}


def test_salvage_requires_existing_workspace(tmp_path: Path):
    assert _salvage_stage_artifacts("eng_backend_tech_design", tmp_path / "missing") == {}
    assert _salvage_stage_artifacts("eng_backend_tech_design", None) == {}


@pytest.mark.asyncio
async def test_stage_timeout_recovers_artifacts_from_workspace(tmp_path, monkeypatch):
    """阶段超时但产物已落 workspace 时，应回收产物并继续推进，而不是判失败。"""
    from sortie_deck import graph as graph_mod

    real_run_executor = graph_mod.run_executor

    async def fake_run_executor(executor, ctx, host=None):
        if ctx.stage != "product_prd":
            return await real_run_executor(executor, ctx, host)
        workspace = Path(ctx.workspace_dir or "")
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "prd.md").write_text("# PRD（超时前已写出）", encoding="utf-8")
        (workspace / "acceptance.json").write_text('{"ok": true}', encoding="utf-8")
        await asyncio.sleep(30)  # 必被 stage_timeout 取消
        raise AssertionError("unreachable")

    monkeypatch.setattr(graph_mod, "run_executor", fake_run_executor)

    cfg = Settings(
        data_dir=tmp_path / "data",
        deploy_executor="mock",
        kb_backend="local",
        pipeline_llm=False,
        room_llm=False,
        stage_timeout_seconds=0.3,
    )
    orch = Orchestrator(cfg)
    await orch.startup()
    try:
        ini = await orch.create_and_start(
            CreateInitiativeRequest(
                title="超时回收",
                brief="新增一个查询用户信息的接口",
                coding_executor="mock",
                harness="mock",
                pipeline_track="default",
                auto_start=True,
                auto_confirm_pipeline=True,
            )
        )
        for _ in range(300):
            await asyncio.sleep(0.1)
            ini = orch.get(ini.id)
            assert ini
            if ini.status.value in {"waiting_hitl", "failed", "done"}:
                break

        assert ini.status.value == "waiting_hitl", [e.message for e in ini.timeline[-3:]]
        assert ini.pending_hitl is not None
        assert ini.pending_hitl.stage == "product_prd"
        assert ("product_prd", "prd.md") in {(a.stage, a.kind) for a in ini.artifacts}
        assert any("回收产物" in e.message for e in ini.timeline)
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_checkpointed_build_retries_before_handing_off(tmp_path, monkeypatch, exhausted):
    from sortie_deck import graph as graph_mod
    from sortie_deck.settings import settings

    real_run_executor = graph_mod.run_executor
    build_attempts = []
    qa_calls = []

    async def fake_run_executor(executor, ctx, host=None):
        if ctx.stage == "eng_implement":
            build_attempts.append(ctx)
            if len(build_attempts) == 1 or exhausted:
                workspace = Path(ctx.workspace_dir)
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "implementation.md").write_text("# Partial build", encoding="utf-8")
                return StageResult(
                    status="fail",
                    message="DSH timeout",
                    meta={"build_checkpoint": True, "timeout": True},
                )
        if ctx.stage == "qa_verify":
            qa_calls.append(ctx.stage)
        return await real_run_executor(executor, ctx, host)

    monkeypatch.setattr(graph_mod, "run_executor", fake_run_executor)
    monkeypatch.setattr(settings, "stage_self_check_attempts", 1)
    monkeypatch.setattr(settings, "max_consecutive_same_failure", 3)
    orch = Orchestrator(Settings(
        data_dir=tmp_path / "data", kb_backend="local", pipeline_llm=False, room_llm=False,
    ))
    await orch.startup()
    try:
        ini = await orch.create_and_start(CreateInitiativeRequest(
            title="Checkpoint retry",
            brief="Build a static page",
            harness="mock",
            pipeline_track="express",
            auto_start=True,
            auto_confirm_pipeline=True,
        ))
        for _ in range(100):
            ini = orch.get(ini.id)
            if ini.status.value in {"waiting_hitl", "failed", "done", "stopped"}:
                break
            await asyncio.sleep(0.05)

        assert len(build_attempts) == 2
        if exhausted:
            assert not qa_calls
            assert ini.status.value == "failed"
            assert ini.current_stage == "eng_implement"
        else:
            assert ini.status.value in {"waiting_hitl", "done"}
            assert qa_calls == ["qa_verify"]
            assert build_attempts[1].permissions.get("build_recovery") is True
    finally:
        await orch.shutdown()


@pytest.mark.asyncio
async def test_parallel_branch_timeout_blocks_downstream(tmp_path, monkeypatch):
    """并行分支里任一实现重试耗尽，都不能继续到联调与 QA。

    串行航线在节点里就 raise 了；并行航线是把每个分支的 run_stage 结果
    gather 回来再合并，所以失败分支必须单独拦截，否则合并后照常往下走。
    """
    from sortie_deck import graph as graph_mod
    from sortie_deck.contracts import REQUIRED
    from sortie_deck.settings import settings

    web_attempts: list[object] = []
    downstream: list[str] = []

    async def fake_run_executor(executor, ctx, host=None):
        if ctx.stage == "eng_web":
            web_attempts.append(ctx)
            return StageResult(
                status="fail",
                message="DSH timeout",
                meta={"build_checkpoint": True, "timeout": True},
            )
        if ctx.stage in {"eng_integrate", "qa_verify", "deploy_preview"}:
            downstream.append(ctx.stage)
        return StageResult(
            status="ok",
            message="fixture",
            artifacts={name: "{}" for name in REQUIRED.get(ctx.stage, [])},
        )

    # 固定一条最短并行航线，避开 standard 模板里的 PRD 人工确认与 LLM 规划
    async def fake_plan(*, title="", brief="", **kwargs):
        return {
            "stages": [
                {"id": "intake", "name": "intake"},
                {"id": "eng_web", "name": "eng_web", "parallel_group": "eng_delivery",
                 "branch": "web", "depends_on": ["intake"]},
                {"id": "eng_backend", "name": "eng_backend", "parallel_group": "eng_delivery",
                 "branch": "backend", "depends_on": ["intake"]},
                {"id": "eng_integrate", "name": "eng_integrate",
                 "depends_on": ["eng_web", "eng_backend"]},
                {"id": "done", "name": "done", "depends_on": ["eng_integrate"]},
            ],
            "rationale": "前后端并行开发",
            "notes": [],
        }

    monkeypatch.setattr("sortie_deck.orchestrator.plan_pipeline_smart", fake_plan)
    monkeypatch.setattr(graph_mod, "run_executor", fake_run_executor)
    monkeypatch.setattr(settings, "stage_self_check_attempts", 1)
    monkeypatch.setattr(settings, "max_consecutive_same_failure", 3)
    orch = Orchestrator(Settings(
        data_dir=tmp_path / "data", kb_backend="local", pipeline_llm=False, room_llm=False,
    ))
    await orch.startup()
    try:
        ini = await orch.create_and_start(CreateInitiativeRequest(
            title="Parallel checkpoint retry",
            brief="需要前后端 API 与页面，可并行开发后联调",
            harness="mock",
            pipeline_track="standard",
            auto_start=True,
            auto_confirm_pipeline=True,
        ))
        for _ in range(200):
            ini = orch.get(ini.id)
            if ini.status.value in {"waiting_hitl", "failed", "done", "stopped"}:
                break
            await asyncio.sleep(0.05)
        # 运行是 create_task 起的：等失败收尾（timeline / room / emit）落完，
        # 否则残留协程会跑进后面的用例，造成随机顺序下的偶发失败
        await asyncio.sleep(0.2)

        assert web_attempts, "失败分支应被真正执行"
        assert downstream == [], f"并行失败后仍进入下游：{downstream}"
        assert ini.status.value == "failed"
    finally:
        await orch.shutdown()
