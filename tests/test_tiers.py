"""航线复杂度分档：任务类型判定、档位推荐、planner 提案收敛。"""

from __future__ import annotations

import pytest

from sortie_deck.contracts import REQUIRED
from sortie_deck.models import Initiative
from sortie_deck.orchestrator import Orchestrator
from sortie_deck.pipeline import catalog_by_id, plan_pipeline
from sortie_deck.pipeline_llm import plan_pipeline_smart
from sortie_deck.settings import Settings
from sortie_deck.tiers import (
    TIER_ALLOWED,
    enforce_tier,
    list_tier_definitions,
    resolve_task_kind,
    resolve_tier,
)

SNAKE = "帮我写一个可运行贪吃蛇小游戏，支持用户登陆注册"


def _spec(sid: str, **extra: object) -> dict[str, object]:
    return {"id": sid, "name": sid, **extra}


def _snake_proposal() -> dict[str, object]:
    """复刻贪吃蛇任务的 15 阶段提案（LLM 自由发挥的结果）。"""
    return {
        "stages": [
            _spec("intake"),
            _spec("product_prd"),
            _spec("design_ui"),
            _spec("qa_cases"),
            _spec("eng_web_tech_design", parallel_group="eng_planning"),
            _spec("eng_backend_tech_design", parallel_group="eng_planning"),
            _spec("eng_web", parallel_group="eng_delivery", branch="web", depends_on=["eng_web_tech_design"]),
            _spec("eng_web_selftest", parallel_group="eng_delivery", branch="web", depends_on=["eng_web"]),
            _spec("eng_backend", parallel_group="eng_delivery", branch="backend", depends_on=["eng_backend_tech_design"]),
            _spec("eng_backend_selftest", parallel_group="eng_delivery", branch="backend", depends_on=["eng_backend"]),
            _spec("eng_integrate", depends_on=["eng_web_selftest", "eng_backend_selftest"]),
            _spec("qa_handoff", depends_on=["eng_integrate"]),
            _spec("qa_verify", depends_on=["qa_handoff", "qa_cases"]),
            _spec("deploy_preview", depends_on=["qa_verify"]),
            _spec("done"),
        ]
    }


# --- 任务类型判定 -------------------------------------------------------------


def test_task_kind_delivery_for_snake() -> None:
    kind, reason = resolve_task_kind("贪吃蛇", SNAKE)
    assert kind == "delivery"
    assert reason


def test_task_kind_knowledge_for_office_request() -> None:
    kind, _ = resolve_task_kind("周报", "帮我整理这周的会议纪要并写一份周报")
    assert kind == "knowledge"


def test_task_kind_repos_force_delivery() -> None:
    kind, _ = resolve_task_kind("周报", "写一份周报", has_repos=True)
    assert kind == "delivery"


def test_task_kind_explicit_wins() -> None:
    kind, _ = resolve_task_kind("贪吃蛇", SNAKE, requested="knowledge")
    assert kind == "knowledge"


# --- 档位推荐 ---------------------------------------------------------------


def test_tier_express_for_small_delivery() -> None:
    tier, _ = resolve_tier("delivery", "贪吃蛇", SNAKE)
    assert tier == "express"


def test_tier_standard_for_persistent_delivery() -> None:
    tier, _ = resolve_tier("delivery", "星轨跳跃", "做一个游戏，局结束后写入排行榜，数据需要持久化")
    assert tier == "standard"


def test_tier_full_for_multi_platform() -> None:
    tier, _ = resolve_tier("delivery", "多端", "做一个多端应用，需要联调 iOS 和 Android")
    assert tier == "full"


def test_tier_knowledge_ladder() -> None:
    assert resolve_tier("knowledge", "周报", "写一份本周周报")[0] == "express"
    assert resolve_tier("knowledge", "整理", "整理本周的资料并做对比分析")[0] == "standard"
    assert resolve_tier("knowledge", "调研", "做一份调研报告，给出结论和行动建议")[0] == "full"


def test_tier_explicit_wins() -> None:
    assert resolve_tier("delivery", "贪吃蛇", SNAKE, requested="full")[0] == "full"


# --- 档位定义 ---------------------------------------------------------------


def test_tier_definitions_cover_both_kinds() -> None:
    defs = list_tier_definitions(None, catalog_by_id())
    assert [d["task_kind"] for d in defs] == ["delivery", "knowledge"]
    for group in defs:
        assert [t["tier"] for t in group["tiers"]] == ["express", "standard", "full"]


def test_express_tier_is_five_nodes() -> None:
    catalog = catalog_by_id()
    defs = list_tier_definitions("delivery", catalog)
    express = defs[0]["tiers"][0]
    assert [s["id"] for s in express["stages"]] == TIER_ALLOWED[("delivery", "express")]
    assert express["stage_count"] == 5
    assert express["eta_minutes"][0] > 0


def test_knowledge_stages_registered_in_catalog_and_contracts() -> None:
    catalog = catalog_by_id()
    for sid in ("office_collect", "office_produce", "office_recommend"):
        assert sid in catalog
        assert catalog[sid]["kind"] == "knowledge"
        assert REQUIRED[sid], f"{sid} 缺少产物契约"
    # research_* 保留给历史任务，同样要能执行
    for sid in ("research_scan", "research_synthesize", "research_recommend"):
        assert sid in catalog
        assert REQUIRED[sid], f"{sid} 缺少产物契约"


# --- 提案收敛 ---------------------------------------------------------------


def test_enforce_tier_prunes_snake_to_express() -> None:
    result = enforce_tier(
        _snake_proposal(), task_kind="delivery", tier="express", catalog=catalog_by_id()
    )
    assert result is not None
    ids = [s["id"] for s in result["stages"]]
    assert ids == ["intake", "eng_implement", "qa_verify", "deploy_preview", "done"]
    assert result["tier"] == "express"
    pruned = {row["id"] for row in result["tier_pruned"]}
    assert {"product_prd", "design_ui", "eng_web_tech_design", "eng_integrate"} <= pruned


def test_enforce_tier_collapses_build_and_selftest() -> None:
    proposal = {
        "stages": [
            _spec("intake"),
            _spec("eng_web", parallel_group="eng_delivery", branch="web"),
            _spec("eng_backend", parallel_group="eng_delivery", branch="backend"),
            _spec("eng_web_selftest"),
            _spec("eng_backend_selftest"),
            _spec("qa_verify"),
            _spec("deploy_preview"),
            _spec("done"),
        ]
    }
    result = enforce_tier(
        proposal, task_kind="delivery", tier="express", catalog=catalog_by_id()
    )
    assert result is not None
    ids = [s["id"] for s in result["stages"]]
    assert ids == ["intake", "eng_implement", "qa_verify", "deploy_preview", "done"]
    # 折叠后的单节点不能再带原分支的并行属性
    impl = next(s for s in result["stages"] if s["id"] == "eng_implement")
    assert "parallel_group" not in impl and "branch" not in impl


def test_enforce_tier_drops_dangling_dependencies() -> None:
    result = enforce_tier(
        _snake_proposal(), task_kind="delivery", tier="express", catalog=catalog_by_id()
    )
    assert result is not None
    kept = {s["id"] for s in result["stages"]}
    for spec in result["stages"]:
        for dep in spec.get("depends_on") or []:
            assert dep in kept, f"{spec['id']} 依赖了被裁掉的 {dep}"


def test_enforce_tier_rejects_proposal_without_core_stage() -> None:
    proposal = {"stages": [_spec("intake"), _spec("product_prd"), _spec("done")]}
    assert (
        enforce_tier(
            proposal, task_kind="delivery", tier="express", catalog=catalog_by_id()
        )
        is None
    )


def test_enforce_tier_knowledge_keeps_office_stages() -> None:
    proposal = {
        "stages": [
            _spec("intake"),
            _spec("office_collect"),
            _spec("office_produce"),
            _spec("office_recommend"),
            _spec("done"),
        ]
    }
    result = enforce_tier(
        proposal, task_kind="knowledge", tier="standard", catalog=catalog_by_id()
    )
    assert result is not None
    assert [s["id"] for s in result["stages"]] == [
        "intake",
        "office_collect",
        "office_produce",
        "done",
    ]


def test_enforce_tier_folds_legacy_research_into_office() -> None:
    """LLM 仍可能从 catalog 提 research_*：统一折叠到 office_*。"""
    proposal = {
        "stages": [
            _spec("intake"),
            _spec("research_scan"),
            _spec("research_synthesize"),
            _spec("research_recommend"),
            _spec("done"),
        ]
    }
    result = enforce_tier(
        proposal, task_kind="knowledge", tier="full", catalog=catalog_by_id()
    )
    assert result is not None
    assert [s["id"] for s in result["stages"]] == [
        "intake",
        "office_collect",
        "office_produce",
        "office_recommend",
        "done",
    ]


# --- 规则版航线与 smart 入口 --------------------------------------------------


def test_plan_pipeline_with_tier_returns_budget_nodes() -> None:
    proposal = plan_pipeline(title="贪吃蛇", brief=SNAKE, task_kind="delivery", tier="express")
    ids = [s["id"] for s in proposal["stages"]]
    assert ids == ["intake", "eng_implement", "qa_verify", "deploy_preview", "done"]
    assert proposal["track_hint"] == "express"
    assert proposal["tier"] == "express"


def test_plan_pipeline_smart_auto_picks_express_for_snake() -> None:
    import asyncio

    proposal = asyncio.run(plan_pipeline_smart(title="贪吃蛇", brief=SNAKE, use_llm=False))
    assert proposal["task_kind"] == "delivery"
    assert proposal["tier"] == "express"
    assert len(proposal["stages"]) == 5


def test_plan_pipeline_smart_respects_hint_track() -> None:
    import asyncio

    proposal = asyncio.run(
        plan_pipeline_smart(
            title="贪吃蛇", brief=SNAKE, hint_track="express", use_llm=False
        )
    )
    assert proposal["tier"] == "express"
    assert proposal["track_hint"] == "express"


# --- 手动编辑的档位预算 -------------------------------------------------------


def test_manual_edit_rejects_stage_outside_tier(tmp_path) -> None:
    """手动编辑航线也不能加超出档位的阶段，否则档位预算形同虚设。"""
    orch = Orchestrator(Settings(data_dir=tmp_path / "data"))
    ini = Initiative(
        title="贪吃蛇",
        brief=SNAKE,
        meta={"task_kind": "delivery", "tier": "express"},
    )
    with pytest.raises(RuntimeError, match="超出当前 express 档预算"):
        orch._assert_tier_budget(
            ini,
            [{"id": "intake"}, {"id": "product_prd"}, {"id": "done"}],
        )
    # 预算内放行
    orch._assert_tier_budget(
        ini,
        [{"id": "intake"}, {"id": "eng_implement"}, {"id": "done"}],
    )


def test_manual_edit_skips_budget_for_legacy_mission(tmp_path) -> None:
    """旧任务没有 task_kind/tier，保持升级前的编辑行为。"""
    orch = Orchestrator(Settings(data_dir=tmp_path / "data"))
    ini = Initiative(title="旧任务", brief="无档位字段")
    orch._assert_tier_budget(ini, [{"id": "intake"}, {"id": "product_prd"}, {"id": "done"}])
