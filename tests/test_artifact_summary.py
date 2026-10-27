"""上游产物摘要器：规则摘要 / 短文件内联 / 缓存命中 / LLM 失败回落。"""

from __future__ import annotations

from pathlib import Path

from sortie_deck import artifact_summary as mod
from sortie_deck.artifact_summary import (
    ArtifactBrief,
    build_artifact_briefs,
    render_artifact_briefs,
    rule_summary,
)


def test_markdown_rule_summary_has_outline_and_first_para():
    text = "# 技术方案\n\n## 接口设计\n\n提供 GET /api/v1/users/{uid}。\n\n## 风险\n\n- 枚举风险\n"
    summary = rule_summary("tech_design.md", text)
    assert "接口设计" in summary
    assert "GET /api/v1/users/{uid}" in summary


def test_json_rule_summary_lists_top_keys():
    summary = rule_summary("acceptance.json", '{"criteria": [1, 2], "owner": "qa"}')
    assert "criteria" in summary and "owner" in summary


def test_diff_rule_summary_counts_lines_and_files():
    patch = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1,2 @@\n"
        "-old\n"
        "+new\n"
        "+extra\n"
    )
    summary = rule_summary("diff.patch", patch)
    assert "a.py" in summary
    assert "+2" in summary and "-1" in summary


def test_empty_file_is_labelled():
    assert rule_summary("x.md", "") == "（空文件）"


async def test_short_artifact_is_inlined(tmp_path: Path):
    briefs = await build_artifact_briefs(
        {"prd.md": "短内容"},
        {"prd.md": "/abs/prd.md"},
        mission_id="ini_x",
        inline_chars=100,
        use_llm=False,
        memory_dir=tmp_path,
    )
    brief = briefs["prd.md"]
    assert brief.mode == "inline"
    assert brief.summary == "短内容"
    assert brief.path == "/abs/prd.md"


async def test_long_artifact_falls_back_to_rule_when_llm_disabled(tmp_path: Path):
    text = "# 标题\n\n" + "正文" * 2000
    briefs = await build_artifact_briefs(
        {"tech_design.md": text},
        {"tech_design.md": "/abs/tech_design.md"},
        mission_id="ini_x",
        inline_chars=100,
        use_llm=False,
        memory_dir=tmp_path,
    )
    assert briefs["tech_design.md"].mode == "rule"
    assert len(briefs["tech_design.md"].summary) < len(text)


async def test_diff_artifact_never_calls_llm(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    async def _boom(name, text, **_kwargs):
        calls.append(name)
        raise AssertionError("diff 不应送 LLM")

    monkeypatch.setattr(mod, "_llm_summary", _boom)
    briefs = await build_artifact_briefs(
        {"diff.patch": "+++ b/a.py\n+1\n" * 100},
        {"diff.patch": "/abs/diff.patch"},
        mission_id="ini_x",
        inline_chars=10,
        use_llm=True,
        memory_dir=tmp_path,
    )
    assert calls == []
    assert briefs["diff.patch"].mode == "rule"


async def test_llm_failure_falls_back_to_rule(tmp_path: Path, monkeypatch):
    async def _fail(*_args, **_kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(mod, "_llm_summary", _fail)
    briefs = await build_artifact_briefs(
        {"prd.md": "# 需求\n\n" + "细节" * 2000},
        {"prd.md": "/abs/prd.md"},
        mission_id="ini_x",
        inline_chars=10,
        use_llm=True,
        memory_dir=tmp_path,
    )
    assert briefs["prd.md"].mode == "rule"


async def test_claude_cli_backend_is_not_used_for_summary(tmp_path: Path, monkeypatch):
    """只有 CLI fallback 时不拉子进程：阶段内已在跑 harness，规则摘要够用。"""
    calls: list[int] = []

    class _FakeClient:
        enabled = True
        backend = "claude_cli"

        def __init__(self, **_kwargs) -> None:
            pass

        async def chat_text(self, **_kwargs) -> str:
            calls.append(1)
            return "不该被调用"

    monkeypatch.setattr("sortie_deck.llm.LlmClient", _FakeClient)
    briefs = await build_artifact_briefs(
        {"prd.md": "# 需求\n\n" + "细节" * 2000},
        {"prd.md": "/abs/prd.md"},
        mission_id="ini_x",
        inline_chars=10,
        use_llm=True,
        memory_dir=tmp_path,
    )
    assert calls == []
    assert briefs["prd.md"].mode == "rule"


async def test_summary_is_cached_by_path_and_content(tmp_path: Path, monkeypatch):
    contents = {"prd.md": "# 需求\n\n" + "细节" * 2000}
    paths = {"prd.md": "/abs/prd.md"}
    first = await build_artifact_briefs(
        contents, paths, mission_id="ini_x", inline_chars=10, use_llm=True, memory_dir=tmp_path
    )

    async def _boom(*_args, **_kwargs):
        raise AssertionError("命中缓存后不应再调 LLM")

    monkeypatch.setattr(mod, "_llm_summary", _boom)
    second = await build_artifact_briefs(
        contents, paths, mission_id="ini_x", inline_chars=10, use_llm=True, memory_dir=tmp_path
    )
    assert second["prd.md"].summary == first["prd.md"].summary


def test_render_includes_path_and_summary():
    brief = ArtifactBrief(
        name="prd.md", path="/abs/prd.md", chars=1604, mode="llm", summary="查询用户信息接口 PRD"
    )
    rendered = render_artifact_briefs({"prd.md": brief})
    assert "/abs/prd.md" in rendered
    assert "查询用户信息接口 PRD" in rendered
    assert render_artifact_briefs({}) == ""
