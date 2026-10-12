from __future__ import annotations

from sortie_deck.artifacts import ArtifactStore
from sortie_deck.graph import build_graph_from_template
from sortie_deck.pipeline import TRACK_FILES, list_tracks
from sortie_deck.plugins.factory import build_default_registry
from sortie_deck.templates import load_pipeline_template


def test_research_track_is_registered() -> None:
    assert TRACK_FILES["research"] == "research_pipeline.yaml"
    assert "research" in {track["id"] for track in list_tracks()}


def test_research_track_has_a_non_engineering_shape() -> None:
    tpl = load_pipeline_template("research")
    names = [s["name"] for s in tpl["stages"]]
    assert names[0] == "intake"
    assert names[-1] == "done"
    assert "research_scan" in names
    assert "research_synthesize" in names
    # deliberately no PRD / build / QA stages
    assert "product_prd" not in names
    assert "eng_implement" not in names


def test_research_track_compiles_into_a_graph(tmp_path) -> None:
    tpl = load_pipeline_template("research")
    registry = build_default_registry(repo_root=tmp_path, worktrees_root=tmp_path / "wt")
    graph = build_graph_from_template(tpl, registry, ArtifactStore(tmp_path / "artifacts"))

    for node in ("intake", "research_scan", "research_synthesize", "research_recommend", "done"):
        assert node in graph.nodes
    # hitl_after stages get their gate wired
    assert "hitl__research_synthesize" in graph.nodes
    assert "hitl__research_recommend" in graph.nodes
