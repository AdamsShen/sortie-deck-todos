from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from sortie_deck.agent_contract import (
    AgentContract,
    contract_from_mapping,
    split_front_matter,
)
from sortie_deck.persona import PersonaSpec, parse_persona, render_persona

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "packages" / "templates"

PERSONAS_DIRNAME = "personas"

_ALIASES = {
    "default": "default_pipeline.yaml",
    "default-office-iteration": "default_pipeline.yaml",
    "express": "express_pipeline.yaml",
    "standard": "standard_pipeline.yaml",
}


def load_pipeline_template(name: str = "default") -> dict[str, Any]:
    filename = _ALIASES.get(name)
    if not filename:
        filename = f"{name}_pipeline.yaml" if not name.endswith(".yaml") else name
        if not name.endswith("_pipeline.yaml") and not name.endswith(".yaml"):
            filename = f"{name}_pipeline.yaml"
    path = TEMPLATES_DIR / filename
    if not path.exists():
        # bare name.yaml fallback
        alt = TEMPLATES_DIR / f"{name}.yaml"
        path = alt if alt.exists() else TEMPLATES_DIR / "default_pipeline.yaml"
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_persona(role: str) -> str:
    """Persona **body** for ``role`` (front-matter contract stripped).

    The YAML contract block is metadata, not prompt material — see
    :func:`load_agent_contract` for the parsed version.
    """
    _, body = load_persona_document(role)
    return body or f"You are the {role} role agent."


def persona_path(role: str) -> Path:
    return TEMPLATES_DIR / PERSONAS_DIRNAME / f"{role}.md"


def list_persona_roles() -> list[str]:
    """Role keys that ship a persona file (used by the role registry / forge)."""
    directory = TEMPLATES_DIR / PERSONAS_DIRNAME
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.md"))



def load_persona_document(role: str) -> tuple[AgentContract | None, str]:
    """Return ``(contract, body)`` for a role persona.

    ``contract`` is ``None`` when the persona has no front-matter block or the
    file does not exist. ``body`` falls back to an empty string in that case.
    """
    path = persona_path(role)
    if not path.exists():
        return None, ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, ""
    front, body = split_front_matter(raw)
    return contract_from_mapping(front, role_key=role), body.strip()


def load_agent_contract(role: str) -> AgentContract | None:
    """Parsed :class:`AgentContract` for ``role``, or ``None`` when undeclared."""
    return load_persona_document(role)[0]


def load_persona_spec(role: str) -> PersonaSpec:
    """结构化人设（角色 / 背景 / 技能 / 工作流 / 示例 / 约束）。

    文件缺失或正文为空时返回空 spec，由调用方决定兜底文案。
    """
    _, body = load_persona_document(role)
    return parse_persona(body)


def compose_persona(role: str, *, role_line: str = "") -> str:
    """基于底座人设生成角色文本，可用 ``role_line`` 覆盖「角色」首句。

    用于已发布 Agent 的专家变体（如产品侦察兵复用 product 底座，只换身份描述）。
    """
    spec = load_persona_spec(role)
    if role_line.strip():
        spec.role = role_line.strip()
    return render_persona(spec) or f"You are the {role} role agent."

