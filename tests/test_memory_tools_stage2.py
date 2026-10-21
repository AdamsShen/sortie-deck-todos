"""阶段二 #6/#7 测试：memory_search/memory_read 元工具 + mem0 候选前置。"""

from __future__ import annotations

import asyncio

import pytest

from sortie_deck.graph import run_memory_meta
from sortie_deck.harness_providers.executor import _parse_toolkit_intents
from sortie_deck.memory import CompositeMemory, CreateMemoryRequest, Mem0Memory, MemoryStore
from sortie_deck.mission_memory import reset_mission_memory
from sortie_deck.settings import settings


@pytest.fixture(autouse=True)
def _missions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)  # missions_dir 由 data_dir 派生
    monkeypatch.setattr(settings, "memory_tools", True)
    monkeypatch.setattr(settings, "memory_read_budget_per_stage", 1)
    reset_mission_memory()
    yield
    reset_mission_memory()


def _store(tmp_path) -> MemoryStore:
    store = MemoryStore(tmp_path / "memory.json")
    for content in ("部署前先跑 lint 与单测", "缓存穿透必须做空值兜底"):
        store.create(
            CreateMemoryRequest(scope="workspace", kind="lesson", content=content),
            author_id="u1",
            author_name="Ada",
        )
    return store


# --- #6 memory_search / memory_read ------------------------------------------


def test_parse_toolkit_intents_includes_memory_keys():
    text = "需要查记忆\n```toolkit_intent\n{\"memory_search\": {\"query\": \"部署\"}}\n```"
    intents = _parse_toolkit_intents(text)
    assert intents["memory_search"] == {"query": "部署"}


def test_memory_search_returns_brief_lines(tmp_path):
    store = _store(tmp_path)
    out = asyncio.run(
        run_memory_meta(
            store,
            stage="impl",
            meta={"memory_search": {"query": "部署 lint"}},
            initiative_id="ini_1",
        )
    )
    assert "Memory search results" in out.brief_append
    assert "[lesson|" in out.brief_append
    assert any(r["op"] == "memory_search" for r in out.records)


def test_memory_read_success_records_usage_log(tmp_path):
    store = _store(tmp_path)
    entry = store.list(scope="workspace")[0]
    out = asyncio.run(
        run_memory_meta(
            store,
            stage="impl",
            meta={"memory_read": {"id": entry.id}},
            initiative_id="ini_1",
            created_by="u1",
        )
    )
    assert entry.content[:100] in out.brief_append
    log = tmp_path / "missions" / "ini_1" / "memory" / "usage.log"
    assert log.exists() and entry.id in log.read_text(encoding="utf-8")


def test_memory_read_budget_exhausted(tmp_path):
    store = _store(tmp_path)
    entry = store.list(scope="workspace")[0]
    out = asyncio.run(
        run_memory_meta(
            store,
            stage="impl",
            meta={"memory_read": {"id": entry.id}},
            initiative_id="ini_1",
            used_reads=1,  # 预算=1 已用尽
        )
    )
    assert "budget exhausted" in out.brief_append
    assert not (tmp_path / "missions" / "ini_1" / "memory" / "usage.log").exists()


def test_memory_read_private_denied(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    private = store.create(
        CreateMemoryRequest(scope="user", scope_id="someone_else", kind="preference", content="私密偏好"),
        author_id="someone_else",
        author_name="Other",
    )
    out = asyncio.run(
        run_memory_meta(
            store,
            stage="impl",
            meta={"memory_read": {"id": private.id}},
            initiative_id="ini_1",
            created_by="u1",  # 任务创建者不是记忆归属者 → 拒绝
        )
    )
    assert "private" in out.brief_append
    assert "私密偏好" not in out.brief_append


def test_memory_tools_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_tools", False)
    store = _store(tmp_path)
    out = asyncio.run(
        run_memory_meta(
            store,
            stage="impl",
            meta={"memory_search": {"query": "部署"}},
            initiative_id="ini_1",
        )
    )
    assert out.brief_append == "" and out.records == []


# --- #7 Plan-time 绑定并入 mem0 -----------------------------------------------


class _FakeMem0(Mem0Memory):
    """search 返回固定文本命中；add 记录调用。"""

    def __init__(self, hits: list[str]) -> None:
        super().__init__(base_url="http://mem0.invalid", api_key=None, user_prefix="t")
        self.hits = hits

    def search(self, query, *, initiative_id, limit=8):
        return self.hits[:limit]

    def add(self, content, *, initiative_id, tags=None, kind=None):
        return "ADD"


def test_composite_context_candidates_boosts_mem0_hits(tmp_path):
    """mem0 命中文本按内容互含回映射本地条目，命中者排前。"""
    store = MemoryStore(tmp_path / "memory.json")
    e_lint = store.create(
        CreateMemoryRequest(scope="workspace", kind="lesson", content="部署前先跑 lint 与单测"),
        author_id="u1",
        author_name="Ada",
    )
    store.create(
        CreateMemoryRequest(scope="workspace", kind="preference", content="产物优先短、可验收"),
        author_id="u1",
        author_name="Ada",
    )
    composite = CompositeMemory(local=store, mem0=_FakeMem0(hits=["部署前先跑 lint 与单测"]))
    picked = composite.context_candidates(query="部署", limit=2)
    assert picked[0].id == e_lint.id


def test_composite_context_candidates_mem0_failure_degrades(tmp_path):
    """mem0 检索异常时降级纯本地序，绝不抛出。"""

    class _BrokenMem0(_FakeMem0):
        def search(self, query, *, initiative_id, limit=8):
            raise RuntimeError("down")

    store = MemoryStore(tmp_path / "memory.json")
    e1 = store.create(
        CreateMemoryRequest(scope="workspace", kind="lesson", content="部署前先跑 lint 与单测"),
        author_id="u1",
        author_name="Ada",
    )
    composite = CompositeMemory(local=store, mem0=_BrokenMem0(hits=[]))
    picked = composite.context_candidates(query="部署", limit=5)
    assert e1.id in [p.id for p in picked]
