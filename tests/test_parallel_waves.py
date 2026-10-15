from sortie_deck.parallel import (
    attach_depends_on,
    collapse_to_waves,
    group_wave_branches,
    selftest_id_for,
    wave_node_name,
)
from sortie_deck.pipeline import plan_pipeline


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
