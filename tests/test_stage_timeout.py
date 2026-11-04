"""阶段超时解析：per-stage 配置 > 编码类默认 3600s > 全局默认 1800s。"""

from __future__ import annotations

from sortie_deck.graph import _resolve_stage_timeout
from sortie_deck.settings import Settings


def test_build_stage_defaults_to_one_hour() -> None:
    """编码类阶段（ENG_BUILD_STAGES）未显式配置时默认 1 小时。"""
    cfg = Settings()
    assert _resolve_stage_timeout("eng_backend", {}, cfg) == 3600.0
    assert _resolve_stage_timeout("eng_web", None, cfg) == 3600.0


def test_non_build_stage_defaults_to_thirty_minutes() -> None:
    """非编码阶段未显式配置时默认 30 分钟。"""
    cfg = Settings()
    assert _resolve_stage_timeout("product_prd", {}, cfg) == 1800.0
    assert _resolve_stage_timeout("qa_cases", None, cfg) == 1800.0


def test_per_stage_config_wins_over_defaults() -> None:
    """阶段 spec 显式配置优先于一切默认值。"""
    cfg = Settings()
    assert _resolve_stage_timeout("eng_backend", {"timeout_seconds": 5400}, cfg) == 5400.0
    assert _resolve_stage_timeout("intake", {"timeout_seconds": 60}, cfg) == 60.0


def test_global_env_override_applies() -> None:
    """全局 env（Settings 注入）覆盖默认值；编码类与普通阶段各读各的。"""
    cfg = Settings(stage_timeout_seconds=1200, eng_stage_timeout_seconds=7200)
    assert _resolve_stage_timeout("product_prd", {}, cfg) == 1200.0
    assert _resolve_stage_timeout("eng_backend", {}, cfg) == 7200.0


def test_spec_timeout_survives_normalize_and_materialize() -> None:
    """PipelineStageSpec.timeout_seconds 要能穿过 normalize_specs → materialize_template。"""
    from sortie_deck.models import PipelineStageSpec
    from sortie_deck.pipeline import materialize_template, normalize_specs

    specs = normalize_specs(
        [
            PipelineStageSpec(
                id="eng_backend",
                label="后端开发",
                role="eng_backend",
                timeout_seconds=5400,
            ).model_dump(),
            PipelineStageSpec(
                id="product_prd", label="产品", role="product"
            ).model_dump(),
        ]
    )
    by_id = {s["id"]: s for s in specs}
    assert by_id["eng_backend"]["timeout_seconds"] == 5400.0
    # 未配置的阶段不携带该键，图端走阶段类型默认
    assert "timeout_seconds" not in by_id["product_prd"]

    tpl = materialize_template(specs)
    stages = {s["name"]: s for s in tpl["stages"]}
    assert stages["eng_backend"]["timeout_seconds"] == 5400.0
    assert "timeout_seconds" not in stages["product_prd"]
