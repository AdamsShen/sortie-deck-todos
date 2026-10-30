"""执行期输入注入：prompt 只带路径索引；实现文件计数忽略知识包产物。"""

from __future__ import annotations

from pathlib import Path

from sortie_deck.harness_providers.executor import HarnessStageExecutor
from sortie_deck.models import StageContext
from sortie_deck.plugins.coding import _count_impl_files


def _ctx(tmp_path: Path, inputs: list[dict]) -> StageContext:
    return StageContext(
        initiative_id="ini_x",
        thread_id="thr_x",
        stage="build",
        role="eng",
        brief="实现预发健康检查",
        artifact_dir=str(tmp_path / "artifacts"),
        inputs=inputs,
    )


def _row(**kw) -> dict:
    base = {
        "id": "mat_1",
        "name": "prd.md",
        "kind": "prd",
        "size": 2048,
        "pinned": True,
        "stored_path": "files/mat_1__prd.md",
        "text_path": "text/mat_1.md",
        "extract_status": "ok",
    }
    base.update(kw)
    return base


def test_prompt_carries_input_index_not_content(tmp_path: Path):
    executor = HarnessStageExecutor.__new__(HarnessStageExecutor)
    ctx = _ctx(tmp_path, [_row()])

    prompt = HarnessStageExecutor._build_prompt(
        executor, ctx, tmp_path / "ws", {}, "", with_subroom=False
    )

    assert "## Input index" in prompt
    assert "input/files/mat_1__prd.md" in prompt
    assert "input/text/mat_1.md" in prompt
    assert "任务输入已快照在" in prompt


def test_prompt_without_inputs_has_no_index(tmp_path: Path):
    executor = HarnessStageExecutor.__new__(HarnessStageExecutor)
    ctx = _ctx(tmp_path, [])

    prompt = HarnessStageExecutor._build_prompt(
        executor, ctx, tmp_path / "ws", {}, "", with_subroom=False
    )

    assert "## Input index" not in prompt


def test_count_impl_files_ignores_knowledge_pack(tmp_path: Path):
    """知识包注入产物不算实现文件，否则空跑也会被判成功。"""
    workspace = tmp_path / "ws"
    (workspace / ".cursor" / "rules").mkdir(parents=True)
    (workspace / ".cursor" / "rules" / "sortie-knowledge.mdc").write_text("x", encoding="utf-8")
    (workspace / ".sortie-deck").mkdir(parents=True)
    (workspace / ".sortie-deck" / "knowledge-pack.md").write_text("x", encoding="utf-8")

    assert _count_impl_files(workspace) == 0

    (workspace / "impl.py").write_text("print(1)\n", encoding="utf-8")
    assert _count_impl_files(workspace) == 1
