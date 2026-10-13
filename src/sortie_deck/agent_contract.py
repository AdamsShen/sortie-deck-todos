"""Unified agent contract: I/O, handoff whitelist, timeout and failure policy.

This is a **pure data layer** — it must not import orchestrator / graph / plugins
so it can be shared by ``contracts``, ``graph``, ``manager``, ``agent_forge`` and
``loadout`` without creating import cycles.

Contracts are authored as YAML front-matter on the persona markdown files under
``packages/templates/personas/<role>.md``::

    ---
    role_key: qa
    title: QA Engineer
    inputs: [prd.md, implementation.md]
    outputs: [test_plan.md, test_cases.md]
    handoff_targets: [eng_web, eng_backend, eng]
    timeout_seconds: 900
    on_fail: escalate
    tools_allowed: []
    ---
    You are a QA engineer ...

A missing front-matter block yields ``None`` (no contract) — callers must treat
that as "no autonomous handoff", falling back to the static pipeline edges.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

FRONT_MATTER_DELIMITER = "---"
VALID_ON_FAIL = ("retry", "escalate", "halt")


class ContractError(ValueError):
    """Raised when a stage artifact or an agent handoff violates its contract."""


class AgentContract(BaseModel):
    """The capabilities and boundaries of one role agent."""

    role_key: str
    title: str = ""
    # Artifact kinds this agent consumes from upstream
    inputs: list[str] = Field(default_factory=list)
    # Artifact kinds this agent is expected to produce
    outputs: list[str] = Field(default_factory=list)
    # Whitelist of role keys this agent may hand off to.
    # An empty list means "no autonomous handoff" (static edges decide).
    handoff_targets: list[str] = Field(default_factory=list)
    timeout_seconds: float = 900.0
    on_fail: Literal["retry", "escalate", "halt"] = "escalate"
    # Toolkit item ids this role is allowed to load
    tools_allowed: list[str] = Field(default_factory=list)

    def allows_handoff(self, dst_role_key: str) -> bool:
        """True when ``dst_role_key`` is inside the handoff whitelist."""
        target = (dst_role_key or "").strip()
        if not target:
            return False
        return target in {t.strip() for t in self.handoff_targets if t and t.strip()}

    def problems(self) -> list[str]:
        """Self-check used by the forge / draft review flow. Empty = healthy."""
        issues: list[str] = []
        if not (self.role_key or "").strip():
            issues.append("role_key is required")
        if self.on_fail not in VALID_ON_FAIL:
            issues.append(f"on_fail must be one of {VALID_ON_FAIL}")
        if self.timeout_seconds <= 0:
            issues.append("timeout_seconds must be > 0")
        if not self.outputs:
            issues.append("outputs is empty — the agent cannot hand anything downstream")
        dupes = {t for t in self.handoff_targets if self.handoff_targets.count(t) > 1}
        if dupes:
            issues.append(f"duplicate handoff_targets: {sorted(dupes)}")
        if self.role_key in self.handoff_targets:
            issues.append("handoff_targets must not contain the role itself")
        return issues

    def brief(self) -> str:
        """Compact contract summary injected into the stage persona prompt."""
        lines = [f"## Contract · `{self.role_key}`"]
        if self.title:
            lines.append(f"- Title: {self.title}")
        if self.inputs:
            lines.append(f"- Reads: {', '.join(self.inputs)}")
        if self.outputs:
            lines.append(f"- Produces: {', '.join(self.outputs)}")
        lines.append(
            "- May hand off to: "
            + (", ".join(self.handoff_targets) if self.handoff_targets else "(none — follow the plan)")
        )
        lines.append(f"- On failure: {self.on_fail}")
        return "\n".join(lines)


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown document into (front_matter_dict, body).

    Documents without a leading ``---`` block return ``({}, text)`` unchanged.
    Malformed YAML is tolerated: the block is consumed and ``{}`` is returned so
    a bad persona can never crash the pipeline.
    """
    if not text:
        return {}, ""
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith(FRONT_MATTER_DELIMITER):
        return {}, text

    lines = stripped.splitlines()
    # First line must be exactly the delimiter (allow trailing whitespace)
    if lines[0].strip() != FRONT_MATTER_DELIMITER:
        return {}, text

    close_index = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == FRONT_MATTER_DELIMITER:
            close_index = idx
            break
    if close_index is None:
        # Unterminated front-matter → treat the whole doc as body
        return {}, text

    raw = "\n".join(lines[1:close_index])
    body = "\n".join(lines[close_index + 1 :]).lstrip("\n")
    try:
        import yaml

        data = yaml.safe_load(raw) or {}
    except Exception:  # noqa: BLE001 — a broken persona header must not break the run
        return {}, body
    if not isinstance(data, dict):
        return {}, body
    return data, body


def contract_from_mapping(data: dict[str, Any] | None, *, role_key: str = "") -> AgentContract | None:
    """Build a contract from parsed front-matter. Returns ``None`` when absent."""
    if not data:
        return None
    payload = dict(data)
    contract_block = payload.get("contract")
    if isinstance(contract_block, dict):
        merged: dict[str, Any] = {k: v for k, v in payload.items() if k != "contract"}
        merged.update(contract_block)
        payload = merged
    payload.setdefault("role_key", role_key)
    # Drop unknown keys so personas can carry unrelated front-matter freely
    allowed = set(AgentContract.model_fields)
    payload = {k: v for k, v in payload.items() if k in allowed}
    if not payload.get("role_key"):
        return None
    if not any(k in payload for k in ("inputs", "outputs", "handoff_targets", "on_fail", "title")):
        # Front-matter exists but carries nothing contract-shaped
        return None
    try:
        return AgentContract.model_validate(payload)
    except ValidationError:
        return None


def validate_handoff(src: AgentContract | None, dst_role_key: str) -> None:
    """Raise :class:`ContractError` unless ``src`` may hand off to ``dst_role_key``."""
    if src is None:
        raise ContractError(
            f"No agent contract for source role — autonomous handoff to `{dst_role_key}` is not allowed"
        )
    if not src.allows_handoff(dst_role_key):
        allowed = ", ".join(src.handoff_targets) or "(none)"
        raise ContractError(
            f"`{src.role_key}` may not hand off to `{dst_role_key}`; allowed: {allowed}"
        )


def handoff_allowed(src: AgentContract | None, dst_role_key: str) -> bool:
    """Non-raising variant of :func:`validate_handoff`."""
    if src is None:
        return False
    return src.allows_handoff(dst_role_key)
