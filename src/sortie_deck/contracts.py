from __future__ import annotations

from sortie_deck.agent_contract import AgentContract, ContractError, validate_handoff

__all__ = [
    "REQUIRED",
    "ContractError",
    "validate_stage_artifacts",
    "validate_handoff",
    "validate_stage_handoff",
]

_ENG_BUILD_ARTIFACTS = ["implementation.md", "tasks.json", "diff.patch"]

REQUIRED: dict[str, list[str]] = {
    "product_prd": ["prd.md", "acceptance.json"],
    "eng_backend_tech_design": ["tech_design.md"],
    "eng_web_tech_design": ["tech_design.md"],
    "eng_ios_tech_design": ["tech_design.md"],
    "eng_android_tech_design": ["tech_design.md"],
    "eng_agent_tech_design": ["tech_design.md"],
    "design_ui": ["design.md", "screens.json"],
    "qa_cases": ["test_plan.md", "test_cases.md"],
    "eng_implement": list(_ENG_BUILD_ARTIFACTS),
    "eng_ios": list(_ENG_BUILD_ARTIFACTS),
    "eng_android": list(_ENG_BUILD_ARTIFACTS),
    "eng_web": list(_ENG_BUILD_ARTIFACTS),
    "eng_backend": list(_ENG_BUILD_ARTIFACTS),
    "eng_agent": list(_ENG_BUILD_ARTIFACTS),
    "eng_selftest": ["selftest.md"],
    "eng_ios_selftest": ["selftest.md"],
    "eng_android_selftest": ["selftest.md"],
    "eng_web_selftest": ["selftest.md"],
    "eng_backend_selftest": ["selftest.md"],
    "eng_agent_selftest": ["selftest.md"],
    "eng_integrate": ["integrate.md"],
    "qa_handoff": ["handoff.md"],
    "qa_verify": ["test_report.json", "test_cases.md"],
    "deploy_preview": ["deploy_plan.md", "deploy_result.json"],
}


def validate_stage_artifacts(stage: str, artifacts: dict[str, str]) -> None:
    required = REQUIRED.get(stage, [])
    missing = [name for name in required if name not in artifacts]
    if missing:
        raise ContractError(f"Stage `{stage}` missing artifacts: {', '.join(missing)}")


def validate_stage_handoff(
    src: AgentContract | None,
    src_stage: str,
    dst_role_key: str,
) -> None:
    """Validate that the agent finishing ``src_stage`` may hand off to ``dst_role_key``.

    Wraps :func:`sortie_deck.agent_contract.validate_handoff` with a stage-aware
    message so orchestration logs point at the offending node.
    """
    try:
        validate_handoff(src, dst_role_key)
    except ContractError as exc:
        role = src.role_key if src else "(none)"
        raise ContractError(f"stage `{src_stage}` (role `{role}`): {exc}") from exc
