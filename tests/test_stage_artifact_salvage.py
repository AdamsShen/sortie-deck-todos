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
