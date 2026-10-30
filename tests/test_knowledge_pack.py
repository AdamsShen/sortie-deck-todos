from __future__ import annotations

from sortie_deck.knowledge import KnowledgeDoc, KnowledgeStore
from sortie_deck.knowledge_pack import (
    load_pack_for_cursor,
    render_knowledge_pack,
    score_doc,
    search_for_mission,
)


def test_score_and_search(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.json")
    # force enterprise-ish docs via create
    from sortie_deck.knowledge import CreateKnowledgeRequest

    store.create(
        CreateKnowledgeRequest(
            title="Payment API",
            summary="charge endpoint",
            body="POST /v1/charges",
            tags=["api", "openapi"],
            category="reference",
        ),
        author_id="u1",
        author_name="t",
    )
    store.create(
        CreateKnowledgeRequest(
            title="Unrelated cookbook",
            summary="pasta",
            body="boil water",
            tags=["food"],
            category="lore",
        ),
        author_id="u1",
        author_name="t",
    )
    hits = search_for_mission(store, "implement payment charges API openapi", limit=3)
    assert hits
    assert "Payment" in hits[0][0].title
    assert score_doc(hits[0][0], "payment api") > score_doc(
        KnowledgeDoc(title="x", body="y"), "payment api"
    )


def test_load_pack_for_cursor(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.json")
    from sortie_deck.knowledge import CreateKnowledgeRequest

    store.create(
        CreateKnowledgeRequest(
            title="Portal health",
            summary="health check",
            body="GET /api/health",
            tags=["api", "portal", "health"],
            category="reference",
        ),
        author_id="u1",
        author_name="t",
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    meta = load_pack_for_cursor(store, ws, "office portal preview health endpoint")
    assert (ws / meta["rule"]).exists()
    assert (ws / meta["pack"]).exists()
    text = (ws / meta["rule"]).read_text(encoding="utf-8")
    assert "alwaysApply" in text
    assert "Portal health" in text or "health" in text.lower()
    pack = render_knowledge_pack(search_for_mission(store, "portal health"), query="portal")
    assert "Sortie Deck knowledge pack" in pack


def test_load_pack_archives_into_mission_logs(tmp_path):
    """workspace 会被回收：知识包必须同时归档到 missions/<id>/logs/knowledge-pack/。"""
    from sortie_deck.knowledge import CreateKnowledgeRequest

    store = KnowledgeStore(tmp_path / "knowledge.json")
    store.create(
        CreateKnowledgeRequest(
            title="Portal health",
            summary="health check",
            body="GET /api/health",
            tags=["api", "portal"],
            category="reference",
        ),
        author_id="u1",
        author_name="t",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    mission_root = tmp_path / "missions" / "ini_x"

    meta = load_pack_for_cursor(
        store,
        workspace,
        "portal health endpoint",
        mission_root=mission_root,
        stage="build",
    )

    archive = mission_root / "logs" / "knowledge-pack" / "build.md"
    assert archive.exists()
    assert meta["archive"] == str(archive)
    text = archive.read_text(encoding="utf-8")
    assert "stage=build" in text
    assert "alwaysApply" in text


def test_load_pack_archive_is_optional(tmp_path):
    """未传 mission_root 时不产生归档（保持旧调用方的行为）。"""
    store = KnowledgeStore(tmp_path / "knowledge.json")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    meta = load_pack_for_cursor(store, workspace, "anything")
    assert meta["archive"] == ""
