"""端到端链路：上传 → 抽取 → manifest → brief → prompt 注入。

分块单测各自覆盖了细节，这里只验证「串起来仍然成立」：
1. 原件与抽取文本落到 `input/`；
2. `input/brief.md` 由 pinned 资料重建（短文本内联、长文本给指针）；
3. manifest 投影进 `StageContext.inputs` 后，prompt 里是路径索引而不是正文。
"""

from __future__ import annotations

from pathlib import Path

from sortie_deck import mission_input
from sortie_deck.harness_providers.executor import HarnessStageExecutor
from sortie_deck.models import StageContext


def _prompt(tmp_path: Path, inputs: list[dict]) -> str:
    ctx = StageContext(
        initiative_id="ini_e2e",
        thread_id="thr_e2e",
        stage="build",
        role="eng",
        brief="实现预发健康检查",
        artifact_dir=str(tmp_path / "artifacts"),
        inputs=inputs,
    )
    executor = HarnessStageExecutor.__new__(HarnessStageExecutor)
    return HarnessStageExecutor._build_prompt(
        executor, ctx, tmp_path / "ws", {}, "", with_subroom=False
    )


def test_upload_to_prompt_inlines_short_material(tmp_path: Path):
    data = tmp_path / "data"
    saved = mission_input.save_upload(
        data,
        "ini_e2e",
        "prd.md",
        "# PRD\n必须支持 iOS 与 Web 预发健康检查".encode(),
        kind="prd",
        pinned=True,
    )
    assert saved.extract_status == "ok"

    brief = mission_input.rebuild_brief(data, "ini_e2e", "上线预发健康检查").read_text(
        encoding="utf-8"
    )
    assert "上线预发健康检查" in brief
    assert "必须支持 iOS 与 Web" in brief

    rows = mission_input.list_input_rows(data, "ini_e2e")
    prompt = _prompt(tmp_path, rows)

    assert f"input/{saved.stored_path}" in prompt
    assert f"input/{saved.text_path}" in prompt
    assert "## Input index" in prompt


def test_upload_to_prompt_keeps_long_material_as_pointer(tmp_path: Path):
    data = tmp_path / "data"
    long_text = ("很长的需求段落。" * 800).encode()
    saved = mission_input.save_upload(
        data, "ini_e2e", "big-prd.md", long_text, kind="prd", pinned=True
    )
    assert saved.extract_status == "ok"

    brief = mission_input.rebuild_brief(data, "ini_e2e", "目标").read_text(encoding="utf-8")
    # 超过内联阈值：brief 里只有指针，正文不会进 prompt
    assert f"`input/{saved.text_path}`" in brief
    assert "很长的需求段落" * 50 not in brief

    rows = mission_input.list_input_rows(data, "ini_e2e")
    prompt = _prompt(tmp_path, rows)
    assert f"input/{saved.text_path}" in prompt
    assert "很长的需求段落。" * 100 not in prompt


def test_binary_material_still_indexed(tmp_path: Path):
    """图片没有抽取文本，但必须出现在索引里（否则 agent 不知道有这个资料）。"""
    data = tmp_path / "data"
    saved = mission_input.save_upload(
        data, "ini_e2e", "wireframe.png", b"\x89PNG\r\n\x1a\nfake", kind="design", pinned=True
    )
    assert saved.extract_status == "skipped"
    assert saved.text_path == ""

    rows = mission_input.list_input_rows(data, "ini_e2e")
    prompt = _prompt(tmp_path, rows)
    assert f"input/{saved.stored_path}" in prompt
    assert "text:skipped" in prompt
