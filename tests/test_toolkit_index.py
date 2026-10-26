"""方案 A：工具箱条目 ↔ WeKnora 镜像 + 混合检索（hybrid+rerank）召回测试。

WeKnoraClient 以 Fake 替身注入（monkeypatch toolkit_index.build_client），
覆盖映射、过滤、降级与全量重建路径，不发真实网络请求。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sortie_deck import toolkit_index
from sortie_deck.loadout import recall
from sortie_deck.settings import settings
from sortie_deck.toolkit import CreateToolkitRequest, ToolkitItem, ToolkitStore
from sortie_deck.weknora import WeKnoraError, WeKnoraHit, WeKnoraKnowledge


class FakeClient:
    """WeKnoraClient 替身：内存维护 knowledge 列表 + 预置检索 hits。"""

    def __init__(self, hits: list[WeKnoraHit] | None = None) -> None:
        self.knowledge: list[tuple[str, str]] = []
        self.contents: dict[str, str] = {}
        self.hits = hits or []
        self.deleted: list[str] = []

    def list_knowledge(self, kb_id: str, page: int = 1, page_size: int = 100) -> list[WeKnoraKnowledge]:
        rows = self.knowledge[(page - 1) * page_size : page * page_size]
        return [WeKnoraKnowledge(id=i, title=t, knowledge_base_id=kb_id) for i, t in rows]

    def delete_knowledge(self, knowledge_id: str) -> None:
        self.deleted.append(knowledge_id)
        self.knowledge = [(i, t) for i, t in self.knowledge if i != knowledge_id]

    def create_manual(self, kb_id: str, *, title: str, content: str, channel: str = "sortie") -> WeKnoraKnowledge:
        kid = f"kn_{len(self.knowledge) + 1}"
        self.knowledge.append((kid, title))
        self.contents[title] = content
        return WeKnoraKnowledge(id=kid, title=title)

    def hybrid_search(self, kb_id: str, query: str, **kwargs: Any) -> list[WeKnoraHit]:
        return self.hits


@pytest.fixture()
def store(tmp_path: Path) -> ToolkitStore:
    return ToolkitStore(tmp_path / "toolkit.json")


@pytest.fixture()
def weknora_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "toolkit_recall_backend", "weknora")
    monkeypatch.setattr(settings, "toolkit_weknora_kb_id", "kb_toolkit")
    monkeypatch.setattr(settings, "weknora_base_url", "http://127.0.0.1:8080")
    # _add() 会触发镜像同步：默认注入空替身，避免用例打到真实 WeKnora（需要命中的用例自行覆盖）
    monkeypatch.setattr(toolkit_index, "build_client", lambda: FakeClient())


def _add(store: ToolkitStore, **kwargs: Any) -> ToolkitItem:
    return store.create(CreateToolkitRequest(**kwargs), author_id="t", author_name="T")


# --- item_content：可检索文本与密钥隔离 ---------------------------------------


def test_item_content_excludes_env(store: ToolkitStore) -> None:
    item = _add(
        store,
        kind="mcp",
        name="pg",
        summary="postgres 工具",
        transport="stdio",
        command="npx",
        env={"PG_PASSWORD": "super-secret"},
    )
    content = toolkit_index.item_content(item)
    assert "postgres" in content and "npx" in content
    assert "super-secret" not in content and "PG_PASSWORD" not in content, "env 永不进镜像"


def test_item_title_carries_id_anchor(store: ToolkitStore) -> None:
    item = _add(store, kind="tool", name="git-diff", runtime="cli")
    title = toolkit_index.item_title(item)
    assert title.endswith(f"#{item.id}")
    assert title.startswith("toolkit/tool/git-diff")


# --- mirror_upsert / mirror_remove --------------------------------------------


def test_mirror_upsert_create_then_update(store: ToolkitStore, weknora_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(toolkit_index, "build_client", lambda: fake)
    item = _add(store, kind="tool", name="git-diff", runtime="cli")

    # create() 的镜像钩子已建 1 条
    title = toolkit_index.item_title(item)
    assert [t for _, t in fake.knowledge if t == title] == [title]

    assert toolkit_index.mirror_upsert(item) == "updated", "同 title 走删旧建新"
    assert toolkit_index.mirror_upsert(item) == "updated"
    assert len([t for _, t in fake.knowledge if t == title]) == 1, "同 title 不应重复建"


def test_mirror_remove_by_item_id(store: ToolkitStore, weknora_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(toolkit_index, "build_client", lambda: fake)
    item = _add(store, kind="tool", name="git-diff", runtime="cli")
    toolkit_index.mirror_upsert(item)

    assert toolkit_index.mirror_remove(item.id) == "removed:1"
    assert fake.deleted and fake.knowledge == []


def test_mirror_skipped_when_backend_off(store: ToolkitStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "toolkit_recall_backend", "local")
    item = _add(store, kind="tool", name="git-diff", runtime="cli")
    assert toolkit_index.mirror_upsert(item) == "skipped"


# --- weknora_recall：混合检索 → title 锚点映射回条目 ---------------------------


def test_weknora_recall_maps_hits_to_items(store: ToolkitStore, weknora_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _add(store, kind="mcp", name="pg", summary="postgres 工具", transport="stdio", command="npx")
    hit = WeKnoraHit(
        knowledge_id="kn_1",
        knowledge_title=toolkit_index.item_title(item),
        content="",
        score=0.87,
    )
    monkeypatch.setattr(toolkit_index, "build_client", lambda: FakeClient(hits=[hit]))

    rows = toolkit_index.weknora_recall(store, "postgres 工具")
    assert rows is not None and len(rows) == 1
    assert rows[0][0].id == item.id
    assert rows[0][1] == pytest.approx(0.87)


def test_weknora_recall_filters(store: ToolkitStore, weknora_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _add(store, kind="mcp", name="pg", summary="postgres 工具", transport="stdio", command="npx")
    other = _add(store, kind="tool", name="git", summary="git 工具", runtime="cli")
    hits = [
        WeKnoraHit(knowledge_id="1", knowledge_title=toolkit_index.item_title(item), content="", score=0.9),
        WeKnoraHit(knowledge_id="2", knowledge_title=toolkit_index.item_title(other), content="", score=0.8),
    ]
    monkeypatch.setattr(toolkit_index, "build_client", lambda: FakeClient(hits=hits))

    rows = toolkit_index.weknora_recall(store, "工具", kinds=["mcp"])
    assert [r[0].id for r in rows] == [item.id]

    rows = toolkit_index.weknora_recall(store, "工具", allowed_ids={other.id})
    assert [r[0].id for r in rows] == [other.id]

    # 停用条目被过滤
    store.update(item.id, type("U", (), {"model_dump": lambda self, **k: {"enabled": False}})())
    rows = toolkit_index.weknora_recall(store, "工具")
    assert all(r[0].id != item.id for r in rows)


def test_weknora_recall_unconfigured_returns_none(store: ToolkitStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "toolkit_recall_backend", "local")
    monkeypatch.setattr(settings, "toolkit_weknora_kb_id", None)
    assert toolkit_index.weknora_recall(store, "x") is None


def test_weknora_recall_ignores_foreign_titles(store: ToolkitStore, weknora_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _add(store, kind="tool", name="git-diff", runtime="cli")
    hits = [
        WeKnoraHit(knowledge_id="1", knowledge_title="别的知识库文档", content="", score=0.9),
        WeKnoraHit(knowledge_id="2", knowledge_title="toolkit/tool/unknown#tk_ghost", content="", score=0.8),
    ]
    monkeypatch.setattr(toolkit_index, "build_client", lambda: FakeClient(hits=hits))
    rows = toolkit_index.weknora_recall(store, "git")
    assert rows == [], "非镜像命中 / 镜像滞后的条目必须被跳过"


# --- recall() 降级链：weknora 失败 → 本地打分 ---------------------------------


def test_recall_falls_back_to_local_on_weknora_error(
    store: ToolkitStore, weknora_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _add(store, kind="mcp", name="pg", summary="postgres 工具", transport="stdio", command="npx")

    class BoomClient(FakeClient):
        def hybrid_search(self, kb_id: str, query: str, **kwargs: Any) -> list[WeKnoraHit]:
            raise WeKnoraError("connection refused")

    monkeypatch.setattr(toolkit_index, "build_client", lambda: BoomClient())
    rows = recall(store, query="postgres")
    assert [r[0].id for r in rows] == [item.id], "WeKnora 异常必须降级本地打分而非抛出"


def test_recall_falls_back_to_local_on_empty_hits(
    store: ToolkitStore, weknora_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WeKnora 命中为空（如镜像刚建、解析尚未完成）时必须回退本地打分，而不是返回空。"""
    fake = FakeClient(hits=[])
    monkeypatch.setattr(toolkit_index, "build_client", lambda: fake)
    item = _add(store, kind="mcp", name="pg", summary="postgres 工具", transport="stdio", command="npx")

    rows = recall(store, query="postgres")
    assert [r[0].id for r in rows] == [item.id], "空命中必须由本地三层打分兜住"


def test_recall_uses_weknora_when_enabled(store: ToolkitStore, weknora_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _add(store, kind="mcp", name="pg", summary="postgres 工具", transport="stdio", command="npx")
    hit = WeKnoraHit(
        knowledge_id="1", knowledge_title=toolkit_index.item_title(item), content="", score=0.66
    )
    monkeypatch.setattr(toolkit_index, "build_client", lambda: FakeClient(hits=[hit]))
    rows = recall(store, query="postgres")
    assert [(r[0].id, r[1]) for r in rows] == [(item.id, pytest.approx(0.66))]


# --- sync_all：全量重建（多退少补） --------------------------------------------


def test_sync_all_reconciles(store: ToolkitStore, weknora_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(toolkit_index, "build_client", lambda: fake)
    a = _add(store, kind="tool", name="git-diff", runtime="cli")
    _add(store, kind="mcp", name="pg", summary="postgres", transport="stdio", command="npx")
    stale_kid = "kn_stale"
    stale_title = "toolkit/tool/old#tk_gone"
    fake.knowledge = [
        (stale_kid, stale_title),  # 本地已删的残留
        ("kn_a", toolkit_index.item_title(a)),  # 已有镜像 → updated
    ]

    stats = toolkit_index.sync_all(store)

    # store 含 6 条种子 + 2 个新增，其中仅 git-diff 已有镜像：其余 7 条走 created
    assert stats["created"] == len(store.list()) - 1
    assert stats["updated"] == 1 and stats["removed"] == 1
    assert stale_kid in fake.deleted
    remaining = {t for _, t in fake.knowledge}
    assert toolkit_index.item_title(a) in remaining
    assert any(t.endswith("#tk_gone") is False for t in remaining)


def test_sync_all_skipped_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "toolkit_recall_backend", "local")
    assert toolkit_index.sync_all(None)["skipped"] == 1
