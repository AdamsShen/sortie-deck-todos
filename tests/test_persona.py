"""结构化人设（persona）的解析 / 渲染 / 质量自检。"""

from __future__ import annotations

from sortie_deck.persona import (
    PersonaExample,
    PersonaSpec,
    parse_persona,
    persona_is_structured,
    persona_quality_issues,
    render_persona,
    spec_from_mapping,
)
from sortie_deck.templates import list_persona_roles, load_persona_spec


def _valid_spec() -> PersonaSpec:
    return PersonaSpec(
        role="你是拥有 10 年经验的资深产品专家，擅长把模糊诉求变成可验收的产品定义。",
        background="- 上游是业务方口述，下游是设计与工程。",
        skills=[
            "需求澄清：提炼目标、用户与边界。",
            "PRD 撰写：范围、非目标与用户故事。",
            "验收设计：把需求转成可测量标准。",
        ],
        workflow=["读 brief 与上游产物。", "补齐关键假设并标注待确认。", "产出 PRD 与验收标准。"],
        examples=[
            PersonaExample(
                situation="做一个能自动发周报的功能",
                response="补齐读者、数据源与频率假设，输出 PRD 与 5 条可测验收标准。",
            )
        ],
        constraints=[
            "只写产物，不实现代码。",
            "不臆造上游未提供的事实。",
            "验收标准必须可测量。",
        ],
    )


def test_render_persona_emits_six_sections() -> None:
    text = render_persona(_valid_spec())
    for heading in ("## 角色", "## 背景", "## 技能", "## 工作流", "## 示例", "## 约束"):
        assert heading in text
    # 工作流按 1. 2. 3. 编号
    assert "1. 读 brief 与上游产物。" in text
    assert "- 期望输出：" in text


def test_render_parse_roundtrip_is_stable() -> None:
    text = render_persona(_valid_spec())
    again = render_persona(parse_persona(text))
    assert again == text


def test_parse_free_text_falls_back_to_role() -> None:
    spec = parse_persona("You are a release engineer. Keep changes reversible.")
    assert spec.role.startswith("You are a release engineer")
    assert spec.skills == []
    assert spec.examples == []


def test_parse_accepts_english_headings_and_bullets() -> None:
    text = "\n".join(
        [
            "## Role",
            "You are a senior QA engineer with 10 years of experience.",
            "## Skills",
            "- Test design",
            "- Defect triage",
            "- Release gating",
            "## Constraints",
            "- Never mark untested as pass",
            "- Cite acceptance criteria",
            "- Report blockers explicitly",
        ]
    )
    spec = parse_persona(text)
    assert spec.role.startswith("You are a senior QA engineer")
    assert spec.skills == ["Test design", "Defect triage", "Release gating"]
    assert len(spec.constraints) == 3


def test_persona_is_structured_threshold() -> None:
    assert persona_is_structured(render_persona(_valid_spec())) is True
    assert persona_is_structured("You are a product manager.") is False


def test_quality_issues_flag_missing_seniority_and_examples() -> None:
    spec = PersonaSpec(role="你是产品专家。", skills=["a"], constraints=[])
    issues = persona_quality_issues(spec)
    assert any("10 年经验" in issue for issue in issues)
    assert any("示例" in issue for issue in issues)
    assert any("约束" in issue for issue in issues)
    assert any("技能" in issue for issue in issues)


def test_quality_issues_clean_for_valid_spec() -> None:
    assert persona_quality_issues(_valid_spec()) == []


def test_all_shipped_personas_are_structured_and_healthy() -> None:
    roles = list_persona_roles()
    assert roles
    for role in roles:
        spec = load_persona_spec(role)
        assert spec.role, f"`{role}` role 段为空"
        assert persona_is_structured(render_persona(spec)), f"`{role}` 未采用六段式"
        assert persona_quality_issues(spec) == [], f"`{role}`: {persona_quality_issues(spec)}"


def test_spec_from_mapping_tolerates_bad_payload() -> None:
    assert spec_from_mapping({"role": "x", "skills": ["s"]}) is not None
    assert spec_from_mapping("not a dict") is None
    assert spec_from_mapping({"skills": "not-a-list"}) is None
