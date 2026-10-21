"""记忆 WeKnora 语义检索（混合检索 + rerank + graph）单元测试。

覆盖：镜像 upsert/remove、检索回映射、可见性过滤（跨任务/隐私条目丢弃）、
本地打分补足、失败降级本地打分。全部用 FakeClient，零外部依赖。
"""

from __future__ import annotations

import pytest

from sortie_deck import memory_mirror as mm
from sortie_deck.memory import CreateMemoryRequest, MemoryStore
from sortie_deck.settings import settings
from sortie_deck.weknora import WeKnoraHit, WeKnoraKnowledge


class FakeWeKnoraClient:
    """内存版 WeKnoraClient：记录镜像文档，hybrid_search 按 query 关键词线性召回。"""

    def __init__(self) -> None:
        self.docs: dict[str, WeKnoraKnowledge] = {}
        self.deleted: list[str] = []
        self.fail_hybrid = False

    def create_manual(self, kb_id: str, *, title: str, content: str, **kwargs):
        doc = WeKnoraKnowledge(id=f"kbdoc_{len(self.docs) + 1}", title=title)
        doc.raw = {"content": content}
        self.docs[title] = doc
        return doc

    def move_to_folder(self, *, knowledge_ids, folder_path, knowledge_base_id):
        return None

    def delete_knowledge(self, knowledge_id: str) -> None:
        self.deleted.append(knowledge_id)
        self.docs = {t: d for t, d in self.docs.items() if d.id != knowledge_id}

    def list_knowledge(self, kb_id: str):
        return list(self.docs.values())

    def hybrid_search(self, kb_id: str, query: str, *, match_count: int = 6, **kwargs):
        if self.fail_hybrid:
            raise mm.WeKnoraError("boom")
        hits = []
        for title, doc in self.docs.items():
            if title.startswith("mem:") and any(
                w and w in (doc.raw or {}).get("content", "") for w in query.split()
            ):
                hits.append(
                    WeKnoraHit(knowledge_id=doc.id, knowledge_title=title, content="", score=1.0)
                )
        return hits[:match_count]


@pytest.fixture()
def weknora_env(tmp_path, monkeypatch):
    """启用 weknora 检索路径 + 注入 FakeClient 单例。"""
    client = FakeWeKnoraClient()
    monkeypatch.setattr(settings, "memory_retrieval", "weknora")
    monkeypatch.setattr(settings, "weknora_base_url", "http://weknora.test")
    monkeypatch.setattr(settings, "weknora_memory_kb_id", "kb_mem")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    mirror = mm.MemoryMirror(client, "kb_mem", tmp_path / "memory_mirror.json")  # type: ignore[arg-type]
    monkeypatch.setattr(mm, "_MIRROR", mirror)
    yield mirror, client
    mm.reset_memory_mirror()


def _create(store: MemoryStore, content: str, **kwargs):
    return store.create(
        CreateMemoryRequest(content=content, **kwargs), author_id="u1", author_name="Ada"
    )


def test_mirror_upsert_and_remove(weknora_env, tmp_path):
    """镜像 upsert 生成 mem:<id> title 文档；remove 删除远端并清状态。"""
    mirror, client = weknora_env
    store = MemoryStore(tmp_path / "memory.json")
    entry = _create(store, "部署前先跑 lint 与单测", scope="workspace", kind="lesson")
    assert f"mem:{entry.id}" in client.docs
    assert mirror._state[entry.id]
    store.delete(entry.id)
    assert f"mem:{entry.id}" not in client.docs
    assert entry.id not in mirror._state


def test_superseded_entry_mirror_removed(weknora_env, tmp_path):
    """冲突被取代的旧条目自动撤镜像（治理态不上检索）。"""
    mirror, client = weknora_env
    store = MemoryStore(tmp_path / "memory.json")
    old = _create(store, "数据库慢查询要先补索引再上线", scope="workspace", kind="lesson")
    _create(store, "数据库慢查询要先补索引再上线，用覆盖索引", scope="workspace", kind="lesson")
    assert f"mem:{old.id}" not in client.docs


def test_context_candidates_uses_weknora_order(weknora_env, tmp_path):
    """启用 WeKnora 后候选排序以语义召回序为准（本地打分只补足）。"""
    mirror, client = weknora_env
    store = MemoryStore(tmp_path / "memory.json")
    # 先建条目（此时已有镜像），再直接向 FakeClient 注入一条新镜像模拟远端已有数据
    e_low = _create(store, "通用约定：产物保持简短", scope="workspace", kind="preference")
    e_hit = _create(store, "缓存穿透必须做空值兜底", scope="workspace", kind="lesson")
    _create(store, "完全无关的记忆内容", scope="workspace", kind="fact")
    # hybrid_search 按 query 分词在 content 里找命中：FakeClient 返回 [e_hit, e_low] 序
    recalled = store.context_candidates(query="缓存 空值 简短", limit=3)
    assert recalled[0].id == e_hit.id
    assert e_low.id in [r.id for r in recalled]


def test_weknora_recall_filters_invisible_entries(weknora_env, tmp_path):
    """其他任务 / 他人隐私条目即使被召回也被本地可见性过滤丢弃。"""
    mirror, client = weknora_env
    store = MemoryStore(tmp_path / "memory.json")
    _create(store, "他任务专属记忆：部署脚本 v2", scope="mission", scope_id="ini_other")
    mine = _create(store, "我的部署部署偏好", scope="user", scope_id="u1", kind="preference")
    picked = store.context_candidates(query="部署", user_id="u1", limit=3)
    ids = [r.id for r in picked]
    assert mine.id in ids
    assert all(r.scope_id != "ini_other" for r in picked)


def test_weknora_failure_falls_back_to_local_scoring(weknora_env, tmp_path):
    """检索抛异常时降级本地打分，绝不抛出。"""
    mirror, client = weknora_env
    client.fail_hybrid = True
    store = MemoryStore(tmp_path / "memory.json")
    hit = _create(store, "缓存穿透必须做空值兜底", scope="workspace", kind="lesson")
    picked = store.context_candidates(query="缓存 空值", limit=3)
    assert hit.id in [r.id for r in picked]


def test_graph_mode_used_when_configured(weknora_env, tmp_path, monkeypatch):
    """weknora_retrieval_mode=graph 时走图谱扩展，失败降级 hybrid。"""
    mirror, client = weknora_env
    monkeypatch.setattr(settings, "weknora_retrieval_mode", "graph")

    graph_calls: list[str] = []

    class GraphClient(FakeWeKnoraClient):
        def create_session(self, title: str = "sortie-retrieval") -> str:
            return "sess_1"

        def delete_session(self, session_id: str) -> None:
            return None

        def knowledge_chat_references(self, session_id, query, kb_ids, **kwargs):
            graph_calls.append(query)
            raise mm.WeKnoraError("graph unavailable")  # 失败 → 降级 hybrid

    import sortie_deck.memory_mirror as m

    mirror.client = GraphClient()
    store = MemoryStore(tmp_path / "memory.json")
    hit = _create(store, "缓存穿透必须做空值兜底", scope="workspace", kind="lesson")
    picked = store.context_candidates(query="缓存", limit=3)
    assert graph_calls == ["缓存"]  # graph 先行
    assert hit.id in [r.id for r in picked]  # hybrid 降级兜住
