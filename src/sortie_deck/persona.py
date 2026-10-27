"""角色人设（persona）的结构化规范与编译器。

角色工坊里的提示词按提示词工程的通用范式分段编写：角色 / 背景 / 技能 /
工作流 / 示例 / 约束（workflow 参考 DeepSeek 提示词库的 Workflow 体例）。
结构化字段只存在于工坊数据层，运行时仍然只消费编译后的 ``persona`` 字符串，
因此 graph / executor / plugins 的注入链路不需要任何改动。

纯数据层：不导入 orchestrator / graph / plugins，避免循环依赖。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

# 段落顺序即渲染顺序；改顺序会改变所有角色提示词，谨慎调整。
SECTION_ORDER: tuple[str, ...] = (
    "role",
    "background",
    "skills",
    "workflow",
    "examples",
    "constraints",
)

SECTION_LABELS: dict[str, str] = {
    "role": "角色",
    "background": "背景",
    "skills": "技能",
    "workflow": "工作流",
    "examples": "示例",
    "constraints": "约束",
}

# 中英文标题别名：历史 persona 是自由文本，用户也可能手写英文标题。
_HEADING_ALIASES: dict[str, str] = {
    "角色": "role",
    "定位": "role",
    "身份": "role",
    "role": "role",
    "背景": "background",
    "上下文": "background",
    "background": "background",
    "backstory": "background",
    "context": "background",
    "技能": "skills",
    "能力": "skills",
    "知识储备": "skills",
    "skills": "skills",
    "capabilities": "skills",
    "工作流": "workflow",
    "流程": "workflow",
    "执行步骤": "workflow",
    "使用说明": "workflow",
    "workflow": "workflow",
    "示例": "examples",
    "例子": "examples",
    "examples": "examples",
    "example": "examples",
    "约束": "constraints",
    "限制": "constraints",
    "禁忌": "constraints",
    "边界": "constraints",
    "constraints": "constraints",
    "rules": "constraints",
}

_H2 = re.compile(r"^\s{0,3}##\s+(.+?)\s*$")
_H3 = re.compile(r"^\s{0,3}###\s+(.+?)\s*$")
_BULLET = re.compile(r"^\s*(?:[-*•·]|\d+[.)、])\s*")
_KEY_PREFIX = r"^\s*(?:[-*•·]\s*)?"
_INPUT_KEY = re.compile(
    _KEY_PREFIX + r"(?:输入|场景|情况|input)\s*[：:]\s*(.*)$", re.IGNORECASE
)
_OUTPUT_KEY = re.compile(
    _KEY_PREFIX + r"(?:期望输出|输出|应做|做法|output|expected)\s*[：:]\s*(.*)$",
    re.IGNORECASE,
)
# 「10 年经验」的常见写法，用于质量自检
_YEARS_MARKERS = ("10 年", "10年", "十年", "10+ 年", "10 + 年")


class PersonaExample(BaseModel):
    """一条示例：典型输入 → 期望输出。"""

    situation: str = ""
    response: str = ""


class PersonaSpec(BaseModel):
    """一个角色的人设结构，可渲染为完整的 system prompt 文本。"""

    role: str = ""
    background: str = ""
    skills: list[str] = Field(default_factory=list)
    workflow: list[str] = Field(default_factory=list)
    examples: list[PersonaExample] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


def _normalize_heading(text: str) -> str:
    return re.sub(r"[\s_]+", "", (text or "").strip().lower())


def _clean_bullet(line: str) -> str:
    return _BULLET.sub("", line).strip()


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _parse_examples(lines: list[str]) -> list[PersonaExample]:
    """把「### 示例」下的文本块解析成输入/期望输出对。"""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if _H3.match(line):
            if current:
                blocks.append(current)
            current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)

    examples: list[PersonaExample] = []
    for block in blocks:
        situation_parts: list[str] = []
        response_parts: list[str] = []
        target = ""
        for line in block:
            if not line.strip():
                continue
            m_in = _INPUT_KEY.match(line)
            m_out = _OUTPUT_KEY.match(line)
            if m_in:
                target = "situation"
                situation_parts.append(m_in.group(1).strip())
            elif m_out:
                target = "response"
                response_parts.append(m_out.group(1).strip())
            elif target == "situation":
                situation_parts.append(_clean_bullet(line))
            elif target == "response":
                response_parts.append(_clean_bullet(line))
        situation = " ".join(p for p in situation_parts if p).strip()
        response = " ".join(p for p in response_parts if p).strip()
        if situation or response:
            examples.append(PersonaExample(situation=situation, response=response))
    return examples


def parse_persona(text: str) -> PersonaSpec:
    """从任意人设文本容错解析出 :class:`PersonaSpec`。

    没有识别到任何段落标题时，整段文本视为「角色」，避免历史自由文本丢失。
    """
    raw = (text or "").strip()
    if not raw:
        return PersonaSpec()

    buckets: dict[str, list[str]] = {}
    current = "role"
    matched = False
    for line in raw.splitlines():
        m = _H2.match(line)
        if m:
            key = _HEADING_ALIASES.get(_normalize_heading(m.group(1)))
            if key:
                current = key
                matched = True
                buckets.setdefault(key, [])
                continue
        buckets.setdefault(current, []).append(line)

    if not matched:
        return PersonaSpec(role=raw)

    role = "\n".join(buckets.get("role", [])).strip()
    background_lines = [
        _clean_bullet(line)
        for line in buckets.get("background", [])
        if _clean_bullet(line)
    ]
    return PersonaSpec(
        role=role,
        background="\n".join(background_lines),
        skills=_dedupe_keep_order(
            [_clean_bullet(line) for line in buckets.get("skills", []) if _clean_bullet(line)]
        ),
        workflow=_dedupe_keep_order(
            [_clean_bullet(line) for line in buckets.get("workflow", []) if _clean_bullet(line)]
        ),
        examples=_parse_examples(buckets.get("examples", [])),
        constraints=_dedupe_keep_order(
            [_clean_bullet(line) for line in buckets.get("constraints", []) if _clean_bullet(line)]
        ),
    )


def render_persona(spec: PersonaSpec) -> str:
    """把结构化人设渲染为 Markdown 提示词（写入 ``persona`` 的唯一出口）。"""
    sections: list[str] = []

    role = (spec.role or "").strip()
    if role:
        sections.append(f"## 角色\n{role}")

    background = [line.strip() for line in (spec.background or "").splitlines() if line.strip()]
    if background:
        sections.append("## 背景\n" + "\n".join(f"- {_clean_bullet(line)}" for line in background))

    skills = _dedupe_keep_order([s.strip() for s in spec.skills])
    if skills:
        sections.append("## 技能\n" + "\n".join(f"- {s}" for s in skills))

    workflow = _dedupe_keep_order([s.strip() for s in spec.workflow])
    if workflow:
        sections.append(
            "## 工作流\n" + "\n".join(f"{i}. {step}" for i, step in enumerate(workflow, start=1))
        )

    examples = [
        e for e in spec.examples if (e.situation or "").strip() or (e.response or "").strip()
    ]
    if examples:
        blocks: list[str] = []
        for i, example in enumerate(examples, start=1):
            lines = [f"### 示例 {i}"]
            if (example.situation or "").strip():
                lines.append(f"- 输入：{example.situation.strip()}")
            if (example.response or "").strip():
                lines.append(f"- 期望输出：{example.response.strip()}")
            blocks.append("\n".join(lines))
        sections.append("## 示例\n" + "\n\n".join(blocks))

    constraints = _dedupe_keep_order([s.strip() for s in spec.constraints])
    if constraints:
        sections.append("## 约束\n" + "\n".join(f"- {c}" for c in constraints))

    return "\n\n".join(sections).strip()


def persona_is_structured(text: str) -> bool:
    """文本是否已包含至少三个规范段落标题（用于判断能否切结构化编辑）。"""
    found = 0
    for line in (text or "").splitlines():
        m = _H2.match(line)
        if m and _HEADING_ALIASES.get(_normalize_heading(m.group(1))):
            found += 1
    return found >= 3


def persona_quality_issues(spec: PersonaSpec) -> list[str]:
    """人设质量自检。只产出问题描述供人工评审，不阻断保存。"""
    issues: list[str] = []
    role = (spec.role or "").strip()
    if not role:
        issues.append("人设缺少「角色」段")
    elif not any(marker in role for marker in _YEARS_MARKERS):
        issues.append("角色段未体现资深年限（建议写明「10 年经验」）")
    if not (spec.background or "").strip():
        issues.append("人设缺少「背景」段")
    if len(spec.skills) < 3:
        issues.append(f"技能条目偏少（当前 {len(spec.skills)} 条，建议 ≥ 3）")
    if not spec.examples:
        issues.append("缺少「示例」，模型难以对齐输出形态")
    if len(spec.constraints) < 3:
        issues.append(f"约束条目偏少（当前 {len(spec.constraints)} 条，建议 ≥ 3）")
    return issues


def spec_from_mapping(value: Any) -> PersonaSpec | None:
    """把 LLM / API 传来的 dict 转成 :class:`PersonaSpec`，失败返回 ``None``。"""
    if isinstance(value, PersonaSpec):
        return value
    if isinstance(value, dict) and value:
        try:
            return PersonaSpec.model_validate(value)
        except Exception:  # noqa: BLE001 — 结构不合法时由调用方降级为文本解析
            return None
    return None
