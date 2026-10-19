from __future__ import annotations

import json

import httpx

from sortie_deck.knowledge import KnowledgeStore
from sortie_deck.knowledge_gateway import ImportKnowledgeRequest, KnowledgeGateway
from sortie_deck.knowledge_pack import load_pack_for_cursor, render_knowledge_pack
from sortie_deck.weknora import WeKnoraClient


def _mock_transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://weknora.test")


def test_weknora_hybrid_search_parses_hits():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/knowledge-bases/kb1/hybrid-search")
        body = json.loads(request.content.decode())
        assert body["query_text"] == "portal health"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": "chunk1",
                        "knowledge_id": "k1",
                        "knowledge_title": "Health API",
                        "content": "GET /api/health",
                        "score": 0.02,
                        "match_type": 0,
                    }
                ],
            },
        )

    with WeKnoraClient("http://weknora.test", "key", client=_mock_transport(handler)) as client:
        hits = client.hybrid_search("kb1", "portal health", match_count=3)
    assert len(hits) == 1
    assert hits[0].knowledge_title == "Health API"
    assert "health" in hits[0].content.lower()


def test_gateway_retrieve_uses_weknora(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/hybrid-search"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "c1",
                            "knowledge_id": "k9",
                            "knowledge_title": "Portal",
                            "content": "office portal preview",
                            "score": 0.01,
                        }
                    ],
                },
            )
        if request.url.path.endswith("/knowledge-bases"):
            return httpx.Response(200, json={"success": True, "data": [{"id": "kb1"}]})
        return httpx.Response(404, json={"success": False, "message": request.url.path})

    store = KnowledgeStore(tmp_path / "knowledge.json")
    client = WeKnoraClient("http://weknora.test", "key", client=_mock_transport(handler))
    gw = KnowledgeGateway(
        store,
        backend="weknora",
        weknora_base_url="http://weknora.test",
        weknora_api_key="key",
        weknora_kb_id="kb1",
        client=client,
    )
    assert gw.uses_weknora
    hits = gw.retrieve("portal", limit=2)
    assert hits[0].source == "weknora"
    assert hits[0].title == "Portal"
    pack = render_knowledge_pack(hits, query="portal")
    assert "Portal" in pack
    assert "office portal preview" in pack


def test_gateway_import_manual_and_sync(tmp_path):
    state = {"docs": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/knowledge/manual") and request.method == "POST":
            body = json.loads(request.content.decode())
            kid = "remote_1"
            row = {
                "id": kid,
                "title": body["title"],
                "description": "",
                "folder_path": "",
                "knowledge_base_id": "kb1",
                "parse_status": "completed",
            }
            state["docs"].append(row)
            return httpx.Response(200, json={"success": True, "data": row})
        if "/knowledge-bases/kb1/knowledge" in path and request.method == "GET":
            return httpx.Response(200, json={"success": True, "data": state["docs"]})
        if path.endswith("/knowledge/folders"):
            return httpx.Response(200, json={"success": True, "data": {"folders": ["/产品"]}})
        return httpx.Response(404, json={"success": False, "message": path})

    store = KnowledgeStore(tmp_path / "knowledge.json")
    # wipe seeds for predictable counts
    store._write([])
    client = WeKnoraClient("http://weknora.test", "key", client=_mock_transport(handler))
    gw = KnowledgeGateway(
        store,
        backend="weknora",
        weknora_base_url="http://weknora.test",
        weknora_kb_id="kb1",
        client=client,
    )
    doc = gw.import_doc(
        ImportKnowledgeRequest(title="API Spec", body="# hello\n", tags=["api"]),
        author_id="u1",
        author_name="Ada",
    )
    assert doc.backend == "weknora"
    assert doc.remote_id == "remote_1"
    assert doc.body == ""
    synced = gw.sync_catalog()
    assert synced["remote_count"] == 1
    assert any(d.remote_id == "remote_1" for d in store.list())


def test_knowledge_chat_references_parses_sse():
    """graph 模式底层：SSE 流中 references 事件应转成 WeKnoraHit，拿到后不等正文。"""

    sse = (
        'data:{"response_type":"thinking","content":"..."}\n\n'
        "data:"
        + json.dumps(
            {
                "response_type": "references",
                "knowledge_references": [
                    {
                        "id": "chunk9",
                        "knowledge_id": "k7",
                        "knowledge_title": "Design",
                        "content": "full body",
                        "matched_content": "matched slice",
                        "score": 0.99,
                        "knowledge_base_id": "kb1",
                        "match_type": 2,
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n\n"
        'data:{"response_type":"answer","content":"不应该被读取"}\n\n'
        'data:{"response_type":"complete","done":true}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/knowledge-chat/sess1")
        body = json.loads(request.content.decode())
        assert body["query"] == "打招呼"
        assert body["knowledge_base_ids"] == ["kb1"]
        return httpx.Response(200, content=sse.encode(), headers={"Content-Type": "text/event-stream"})

    with WeKnoraClient("http://weknora.test", "key", client=_mock_transport(handler)) as client:
        hits = client.knowledge_chat_references("sess1", "打招呼", ["kb1"])
    assert len(hits) == 1
    assert hits[0].knowledge_id == "k7"
    # 优先取 matched_content（图谱扩展召回的片段）
    assert hits[0].content == "matched slice"
    assert hits[0].score == 0.99


def test_gateway_graph_mode_falls_back_to_hybrid(tmp_path):
    """graph 模式下 knowledge-chat 失败应降级 hybrid-search，检索不中断。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sessions") and request.method == "POST":
            return httpx.Response(200, json={"success": True, "data": {"id": "s1"}})
        if "/knowledge-chat/" in path:
            # 模拟图谱链路故障
            return httpx.Response(500, json={"success": False, "message": "boom"})
        if path.endswith("/hybrid-search"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "c1",
                            "knowledge_id": "k9",
                            "knowledge_title": "Portal",
                            "content": "office portal preview",
                            "score": 0.01,
                        }
                    ],
                },
            )
        if path.endswith("/knowledge-bases"):
            return httpx.Response(200, json={"success": True, "data": [{"id": "kb1"}]})
        return httpx.Response(404, json={"success": False, "message": path})

    store = KnowledgeStore(tmp_path / "knowledge.json")
    store._write([])
    client = WeKnoraClient("http://weknora.test", "key", client=_mock_transport(handler))
    gw = KnowledgeGateway(
        store,
        backend="weknora",
        weknora_base_url="http://weknora.test",
        weknora_api_key="key",
        weknora_kb_id="kb1",
        weknora_retrieval_mode="graph",
        client=client,
    )
    hits = gw.retrieve("portal", limit=2)
    assert len(hits) == 1
    assert hits[0].source == "weknora"
    assert hits[0].title == "Portal"


def test_gateway_hybrid_mode_skips_graph_pipeline(tmp_path):
    """hybrid 模式不应触碰 sessions/knowledge-chat，直接走 hybrid-search。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert "/sessions" not in path
        assert "/knowledge-chat/" not in path
        if path.endswith("/hybrid-search"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "c1",
                            "knowledge_id": "k9",
                            "knowledge_title": "Portal",
                            "content": "office portal preview",
                            "score": 0.01,
                        }
                    ],
                },
            )
        if path.endswith("/knowledge-bases"):
            return httpx.Response(200, json={"success": True, "data": [{"id": "kb1"}]})
        return httpx.Response(404, json={"success": False, "message": path})

    store = KnowledgeStore(tmp_path / "knowledge.json")
    store._write([])
    client = WeKnoraClient("http://weknora.test", "key", client=_mock_transport(handler))
    gw = KnowledgeGateway(
        store,
        backend="weknora",
        weknora_base_url="http://weknora.test",
        weknora_api_key="key",
        weknora_kb_id="kb1",
        client=client,
    )
    hits = gw.retrieve("portal", limit=2)
    assert hits[0].title == "Portal"


def test_load_pack_via_gateway(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": "c",
                        "knowledge_id": "k",
                        "knowledge_title": "Design",
                        "content": "use tokens",
                        "score": 0.03,
                    }
                ],
            },
        )

    store = KnowledgeStore(tmp_path / "knowledge.json")
    store._write([])
    client = WeKnoraClient("http://weknora.test", client=_mock_transport(handler))
    gw = KnowledgeGateway(
        store, backend="weknora", weknora_kb_id="kb1", client=client, weknora_base_url="http://x"
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    meta = load_pack_for_cursor(store, ws, "design tokens", gateway=gw)
    assert meta["source"] == "weknora"
    assert meta["hits"] == "1"
    assert (ws / ".cursor" / "rules" / "sortie-knowledge.mdc").exists()
    assert "use tokens" in (ws / ".sortie-deck" / "knowledge-pack.md").read_text(encoding="utf-8")
