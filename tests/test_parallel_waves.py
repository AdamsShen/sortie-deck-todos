from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from sortie_deck.artifacts import ArtifactStore
from sortie_deck.graph import build_graph_from_template
from sortie_deck.models import StageResult
from sortie_deck.parallel import (
    attach_depends_on,
    clear_wave_checkpoint,
    collapse_to_waves,
    group_wave_branches,
    merge_wave_checkpoint,
    pending_wave_members,
    selftest_id_for,
    wave_node_name,
)
from sortie_deck.pipeline import plan_pipeline
from sortie_deck.registry import ExecutorRegistry


def test_collapse_to_waves_groups_parallel():
    stages = [
        {"name": "intake"},
        {"name": "eng_web", "parallel_group": "eng_build"},
        {"name": "eng_backend", "parallel_group": "eng_build"},
        {"name": "eng_integrate"},
        {"name": "done"},
    ]
    waves = collapse_to_waves(stages)
    assert waves[0]["kind"] == "single"
    assert waves[1]["kind"] == "parallel"
    assert waves[1]["group"] == "eng_build"
    assert [m["name"] for m in waves[1]["members"]] == ["eng_web", "eng_backend"]
    assert waves[2]["stage"]["name"] == "eng_integrate"
    assert wave_node_name("eng_build") == "__wave__eng_build"


def test_collapse_branch_chains():
    stages = [
        {"name": "qa_cases"},
        {"name": "eng_web", "parallel_group": "eng_delivery", "branch": "web"},
        {"name": "eng_web_selftest", "parallel_group": "eng_delivery", "branch": "web"},
        {"name": "eng_backend", "parallel_group": "eng_delivery", "branch": "backend"},
        {"name": "eng_backend_selftest", "parallel_group": "eng_delivery", "branch": "backend"},
        {"name": "eng_integrate"},
        {"name": "done"},
    ]
    waves = collapse_to_waves(stages)
    assert waves[1]["kind"] == "parallel"
    branches = waves[1]["branches"]
    assert [b["id"] for b in branches] == ["web", "backend"]
    assert [m["name"] for m in branches[0]["members"]] == ["eng_web", "eng_web_selftest"]
    assert [m["name"] for m in branches[1]["members"]] == ["eng_backend", "eng_backend_selftest"]


def test_attach_depends_on_fan_out_and_in():
    stages = [
        {"id": "qa_cases", "name": "qa_cases"},
        {"id": "eng_web", "name": "eng_web", "parallel_group": "eng_delivery", "branch": "web"},
        {
            "id": "eng_web_selftest",
            "name": "eng_web_selftest",
            "parallel_group": "eng_delivery",
            "branch": "web",
        },
        {
            "id": "eng_backend",
            "name": "eng_backend",
            "parallel_group": "eng_delivery",
            "branch": "backend",
        },
        {
            "id": "eng_backend_selftest",
            "name": "eng_backend_selftest",
            "parallel_group": "eng_delivery",
            "branch": "backend",
        },
        {"id": "eng_integrate", "name": "eng_integrate"},
    ]
    out = attach_depends_on(stages)
    by = {s["id"]: s for s in out}
    assert by["eng_web"]["depends_on"] == ["qa_cases"]
    assert by["eng_backend"]["depends_on"] == ["qa_cases"]
    assert by["eng_web_selftest"]["depends_on"] == ["eng_web"]
    assert by["eng_backend_selftest"]["depends_on"] == ["eng_backend"]
    assert set(by["eng_integrate"]["depends_on"]) == {"eng_web_selftest", "eng_backend_selftest"}


def test_planner_emits_branch_selftest_graph():
    proposal = plan_pipeline(
        title="门户",
        brief="需要 Web 前端与后端 API，可并行开发后联调",
        hint_track="standard",
    )
    stages = proposal["stages"]
    by_id = {s["id"]: s for s in stages}
    assert "eng_web" in by_id
    assert "eng_backend" in by_id
    assert "eng_web_selftest" in by_id
    assert "eng_backend_selftest" in by_id
    assert "eng_selftest" not in by_id
    assert by_id["eng_web"].get("parallel_group") == "eng_delivery"
    assert by_id["eng_web"].get("branch") == "web"
    assert by_id["eng_backend_selftest"].get("branch") == "backend"
    assert by_id["eng_web_selftest"]["depends_on"] == ["eng_web"]
    assert set(by_id["eng_integrate"]["depends_on"]) == {
        "eng_web_selftest",
        "eng_backend_selftest",
    }
    # 编排图应体现「多端并行 + 各端自测 + 联调」的并行结构（rationale/notes 实际描述）
    assert any(
        k in proposal["rationale"] or any(k in n for n in proposal["notes"])
        for k in ("并行", "自测", "联调", "∥")
    )


def test_group_wave_branches_default_singleton():
    members = [
        {"name": "eng_web", "parallel_group": "eng_build"},
        {"name": "eng_backend", "parallel_group": "eng_build"},
    ]
    branches = group_wave_branches(members)
    assert len(branches) == 2
    assert branches[0]["members"][0]["name"] == "eng_web"


def test_selftest_id_for():
    assert selftest_id_for("eng_web") == "eng_web_selftest"
    assert selftest_id_for("eng_implement") == "eng_selftest"


def _delivery_branches():
    return group_wave_branches(
        [
            {"name": "eng_web", "parallel_group": "eng_delivery", "branch": "web"},
            {"name": "eng_web_selftest", "parallel_group": "eng_delivery", "branch": "web"},
            {"name": "eng_backend", "parallel_group": "eng_delivery", "branch": "backend"},
            {"name": "eng_backend_selftest", "parallel_group": "eng_delivery", "branch": "backend"},
        ]
    )


def test_pending_wave_members_skips_completed_branch():
    branches = _delivery_branches()
    # web 分支整条完成 → 只剩 backend 分支待跑
    assert pending_wave_members(branches, {"eng_web", "eng_web_selftest"}) == [
        "eng_backend",
        "eng_backend_selftest",
    ]
    # 分支中途被卡（eng_backend 完成、selftest 未跑）只补后半个
    assert pending_wave_members(branches, {"eng_web", "eng_web_selftest", "eng_backend"}) == [
        "eng_backend_selftest"
    ]


def test_pending_wave_members_without_checkpoint_runs_all():
    branches = _delivery_branches()
    assert pending_wave_members(branches, set()) == [
        "eng_web",
        "eng_web_selftest",
        "eng_backend",
        "eng_backend_selftest",
    ]


def test_merge_wave_checkpoint_keeps_plan_order():
    order = ["eng_web", "eng_web_selftest", "eng_backend", "eng_backend_selftest"]
    done = merge_wave_checkpoint(order, ["eng_web"], ["eng_backend", "eng_web_selftest"])
    assert done == ["eng_web", "eng_web_selftest", "eng_backend"]


def test_clear_wave_checkpoint_drops_only_target_group():
    done = {"eng_delivery": ["eng_web"], "qa_fix": ["qa_web"]}
    results = {
        "eng_delivery::eng_web": {"status": "ok"},
        "qa_fix::qa_web": {"status": "ok"},
    }
    next_done, next_results = clear_wave_checkpoint("eng_delivery", done, results)
    assert next_done == {"qa_fix": ["qa_web"]}
    assert next_results == {"qa_fix::qa_web": {"status": "ok"}}


def test_pipeline_state_declares_wave_checkpoint_fields():
    """LangGraph 按 schema 过滤 state：漏声明字段会被静默丢弃，断点续跑直接失效。"""
    from sortie_deck.graph import PipelineState

    for key in ("wave_done", "wave_results", "wave_resume"):
        assert key in PipelineState.__annotations__


# --- 波次 HITL 断点续跑（行为级） --------------------------------------------


class _CountingExecutor:
    """前 ``hitl_times`` 次返回 needs_hitl（模拟沙箱权限拦截），之后成功并把调用次数留痕。"""

    def __init__(self, name: str, *, hitl_times: int = 0) -> None:
        self.name = name
        self.hitl_times = hitl_times
        self.calls = 0

    async def run(self, ctx):
        self.calls += 1
        artifacts = {
            f"{ctx.stage}.md": "body",
            "implementation.md": "body",
            "tasks.json": "[]",
            "diff.patch": "",
            "selftest_log.json": "{}",
            "selftest.md": "body",
            "checklist.md": "- [x] done",
        }
        if self.calls <= self.hitl_times:
            return StageResult(
                status="needs_hitl",
                message=f"{ctx.stage} blocked on sandbox permission",
                artifacts=artifacts,
                meta={
                    "permission_block": True,
                    "retry_permission_mode": "bypassPermissions",
                },
            )
        return StageResult(
            status="ok",
            message=f"{ctx.stage} ran",
            artifacts=artifacts,
        )


def _wave_template() -> dict:
    def stage(name: str, role: str, *, group: str = "", branch: str = "") -> dict:
        out = {"name": name, "role": role, "executor": f"ex_{name}"}
        if group:
            out["parallel_group"] = group
            out["branch"] = branch
        return out

    return {
        "name": "wave-resume",
        "stages": [
            stage("intake", "product"),
            stage("eng_web", "eng_web", group="eng_delivery", branch="web"),
            stage("eng_web_selftest", "eng_web", group="eng_delivery", branch="web"),
            stage("eng_backend", "eng_backend", group="eng_delivery", branch="backend"),
            stage("eng_backend_selftest", "eng_backend", group="eng_delivery", branch="backend"),
            stage("done", "product"),
        ],
    }


def _wave_payload() -> dict:
    return {
        "initiative_id": "ini_wave",
        "thread_id": "thr_wave",
        "brief": "并行波次断点续跑",
        "title": "wave",
        "stage": "intake",
        "human_instruction": None,
        "coding_executor": "mock",
        "deploy_executor": "mock",
        "artifact_root": "",
        "workspace_dir": None,
        "upstream": {},
        "qa_attempt": 1,
        "events": [],
        "status": "running",
        "stopped": False,
    }


async def test_wave_hitl_resume_skips_completed_branch(tmp_path) -> None:
    tpl = _wave_template()
    registry = ExecutorRegistry()
    counters: dict[str, _CountingExecutor] = {}
    for s in tpl["stages"]:
        ex = _CountingExecutor(
            s["executor"], hitl_times=1 if s["name"] == "eng_backend" else 0
        )
        counters[s["name"]] = ex
        registry.register(ex)

    builder = build_graph_from_template(tpl, registry, ArtifactStore(tmp_path / "artifacts"))
    compiled = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "thr_wave"}}

    first = await compiled.ainvoke(_wave_payload(), config)
    assert "__interrupt__" in dict(first)
    # 首次：web 分支两条都完成，backend 卡在 eng_backend，自测没跑
    assert counters["eng_web"].calls == 1
    assert counters["eng_web_selftest"].calls == 1
    assert counters["eng_backend"].calls == 1
    assert counters["eng_backend_selftest"].calls == 0
    # 断点记下了已完成的三个成员
    assert set((dict(first).get("wave_done") or {})["eng_delivery"]) == {
        "eng_web",
        "eng_web_selftest",
    }

    resumed = await compiled.ainvoke(Command(resume={"action": "approve"}), config)

    # 关键断言：放行后 web 分支不重跑（原来整波重放要多烧 20+ 分钟）
    assert counters["eng_web"].calls == 1
    assert counters["eng_web_selftest"].calls == 1
    assert counters["eng_backend"].calls == 2
    assert counters["eng_backend_selftest"].calls == 1
    # 波次之后照常走到终态
    assert dict(resumed).get("stage") == "done"
    assert any(e.get("stage") == "done" for e in dict(resumed).get("events") or [])
    # 跑完即清断点：QA 打回时该波次必须能全量重跑
    assert "eng_delivery" not in (dict(resumed).get("wave_done") or {})
    assert not [
        k for k in (dict(resumed).get("wave_results") or {}) if str(k).startswith("eng_delivery::")
    ]
