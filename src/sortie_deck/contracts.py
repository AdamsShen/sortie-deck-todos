from __future__ import annotations

from sortie_deck.agent_contract import AgentContract, ContractError, validate_handoff

__all__ = [
    "OPTIONAL",
    "REQUIRED",
    "ContractError",
    "optional_artifacts",
    "StageContractError",
    "validate_stage_artifacts",
    "validate_handoff",
    "validate_stage_handoff",
]


class StageContractError(ContractError):
    """阶段产物契约错误，携带违规阶段。

    并行波次里各成员共享一个 ``current_stage``，异常冒泡时它可能已指向别的
    成员（本次现场即把 `eng_backend_tech_design` 的失败记到了 `qa_cases`）；
    编排层靠 ``stage`` 把错误归位。
    """

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage

_ENG_BUILD_ARTIFACTS = ["implementation.md", "tasks.json", "diff.patch"]

# 可选产物：有就归档、缺了不算失败。用于「方法论增强」类文件
# （实现清单 / 验证命令日志 / 自审报告），避免把它们塞进契约后
# 卡死历史任务与 mock 执行器。
OPTIONAL: dict[str, list[str]] = {
    "eng_implement": ["checklist.md", "selftest_log.json"],
    "eng_ios": ["checklist.md", "selftest_log.json"],
    "eng_android": ["checklist.md", "selftest_log.json"],
    "eng_web": ["checklist.md", "selftest_log.json"],
    "eng_backend": ["checklist.md", "selftest_log.json"],
    "eng_agent": ["checklist.md", "selftest_log.json"],
    "eng_selftest": ["checklist.md", "selftest_log.json"],
    "eng_ios_selftest": ["checklist.md", "selftest_log.json"],
    "eng_android_selftest": ["checklist.md", "selftest_log.json"],
    "eng_web_selftest": ["checklist.md", "selftest_log.json"],
    "eng_backend_selftest": ["checklist.md", "selftest_log.json"],
    "eng_agent_selftest": ["checklist.md", "selftest_log.json"],
    "eng_integrate": ["selftest_log.json"],
}
# 技术方案阶段的实现清单（按后缀匹配：eng_backend_tech_design / eng_web_tech_design …）
_TECH_DESIGN_OPTIONAL = ["checklist.md"]


def optional_artifacts(stage: str) -> list[str]:
    """本阶段的可选产物名（缺失不报错，只影响归档完整度）。"""
    if stage in OPTIONAL:
        return list(OPTIONAL[stage])
    if stage.endswith("_tech_design"):
        return list(_TECH_DESIGN_OPTIONAL)
    return []


REQUIRED: dict[str, list[str]] = {
    # 集结阶段只交一页 `intake.md`（需求复述 / 范围 / 假设 / 待确认 / 交接要点）。
    # 之前这里没有 intake 项，模型拿不到产出规格，就去写完整 PRD + 验收用例，
    # 既越界又把旧版 prd.md 留在 workspace 里盖住 product_prd 的正式版。
    "intake": ["intake.md"],
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
        raise StageContractError(
            stage, f"Stage `{stage}` missing artifacts: {', '.join(missing)}"
        )


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
