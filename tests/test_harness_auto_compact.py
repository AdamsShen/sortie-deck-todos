"""会话内自动压缩：窗口注入、compact 事件解析，以及摘要+路径的 prompt 形态。"""

from __future__ import annotations

from pathlib import Path

from sortie_deck.artifact_summary import ArtifactBrief
from sortie_deck.harness_providers.claude_code import _auto_compact_env, _compact_meta
from sortie_deck.harness_providers.executor import HarnessStageExecutor
from sortie_deck.models import RoleId, StageContext


def test_auto_compact_env_injects_window():
    assert _auto_compact_env(200000) == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}
    assert _auto_compact_env("150000") == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"}


def test_auto_compact_env_disabled_for_zero_or_invalid():
    assert _auto_compact_env(0) is None
    assert _auto_compact_env(None) is None
    assert _auto_compact_env("abc") is None
    assert _auto_compact_env(-1) is None


def test_compact_meta_accepts_camel_and_snake_case():
    camel = _compact_meta({"preTokens": 210000, "postTokens": 40000, "trigger": "auto"})
    assert camel == {
        "compacted": True,
        "at_token": 210000,
        "post_token": 40000,
        "trigger": "auto",
    }
    snake = _compact_meta({"pre_tokens": 1, "post_tokens": 2})
    assert snake["at_token"] == 1 and snake["post_token"] == 2


def _ctx(**kwargs) -> StageContext:
    payload = {
        "initiative_id": "ini_test",
        "thread_id": "thr_test",
        "stage": "eng_backend_selftest",
        "role": RoleId.ENG_BACKEND,
        "brief": "新增查询用户信息接口",
        "artifact_dir": "/tmp/artifacts",
    }
    payload.update(kwargs)
    return StageContext(**payload)


def _briefs(text: str) -> dict[str, ArtifactBrief]:
    return {
        "tech_design.md": ArtifactBrief(
            name="tech_design.md",
            path="/abs/tech_design.md",
            chars=len(text),
            mode="llm",
            summary="后端技术方案，覆盖 D-1…D-11 与 R1…R16",
        )
    }


def test_prompt_uses_briefs_instead_of_full_text(tmp_path: Path):
    long_text = "# 技术方案\n\n" + "接口细节" * 3000
    ctx = _ctx(upstream_artifacts={"tech_design.md": long_text})
    pack = {"briefs": _briefs(long_text)}
    prompt = HarnessStageExecutor()._build_prompt(ctx, tmp_path, pack, "")
    assert "Upstream artifacts" in prompt
    assert "/abs/tech_design.md" in prompt
    assert "D-1…D-11" in prompt
    assert "接口细节接口细节" not in prompt


def test_prompt_falls_back_to_full_prd_when_briefs_missing(tmp_path: Path):
    ctx = _ctx(upstream_artifacts={"prd.md": "PRD 全文内容"})
    prompt = HarnessStageExecutor()._build_prompt(ctx, tmp_path, {"briefs": {}}, "")
    assert "Upstream PRD" in prompt
    assert "PRD 全文内容" in prompt


def test_mcp_servers_skipped_when_strict_disabled(monkeypatch):
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "harness_mcp_strict", False)
    servers, notes = HarnessStageExecutor()._mcp_servers(_ctx())
    assert servers == {}
    assert notes and "strict 关闭" in notes[0]
