"""记忆治理（阶段一 #1-#5）单元测试。

覆盖：衰减打分、冲突失效标记、并行分支合并仲裁、分层摘要降级、
反思沉淀淘汰上限、治理态条目可见性。全部零外部依赖（无 mem0 / 无 LLM）。
"""

from __future__ import annotations

from sortie_deck.memory import (
    CreateMemoryRequest,
    MemoryEntry,
    MemoryStore,
    enforce_agent_lesson_cap,
    merge_stage_conclusions,
)
from sortie_deck.mission_memory import build_stage_summary


def _create(store: MemoryStore, content: str, **kwargs) -> MemoryEntry:
    return store.create(
        CreateMemoryRequest(content=content, **kwargs),
        author_id="u1",
        author_name="Ada",
    )


# --- 衰减与治理态可见性 -------------------------------------------------------


def test_superseded_entries_hidden_by_default(tmp_path):
    """被取代条目默认不进列表与注入，include_inactive 显式可见（bi-temporal）。"""
    store = MemoryStore(tmp_path / "memory.json")
    old = _create(
        store, "部署前先跑 lint 与单测", scope="workspace", kind="lesson"
    )
    new = _create(
        store, "部署前先跑 lint 与单测，双绿再发车", scope="workspace", kind="lesson"
    )
    # create() 返回的是标记前的快照，冲突标记以 store 状态为准，重新取回
    old = store.get(old.id)
    assert old.status == "superseded"
    assert old.superseded_by == new.id
    active_ids = {e.id for e in store.list(scope="workspace")}
    assert new.id in active_ids and old.id not in active_ids
    inactive = {e.id for e in store.list(scope="workspace", include_inactive=True)}
    assert old.id in inactive and new.id in inactive
    # 索引文件标注失效态
    index_text = store.index_path.read_text(encoding="utf-8")
    assert "已失效" in index_text


def test_recency_bonus_decays(tmp_path):
    """新近度加权：新条目排在旧条目前（同等内容，靠衰减拉开差距）。"""
    store = MemoryStore(tmp_path / "memory.json")
    _create(store, "早期约定：产物保持简短可验收", scope="workspace", kind="preference")
    # 人为把第一条更新时间推旧 30 天，制造衰减差
    entries = store.list(scope="workspace")
    aged = entries[0].model_dump()
    from datetime import datetime, timedelta
    from sortie_deck.models import utc_now

    aged["updated_at"] = (utc_now() - timedelta(days=30)).isoformat()
    aged_entry = MemoryEntry.model_validate(aged)
    data = [e.model_dump(mode="json") for e in store.list(include_inactive=True)]
    for d in data:
        if d["id"] == aged_entry.id:
            d["updated_at"] = aged_entry.updated_at
    store.path.write_text(
        __import__("json").dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    _create(store, "新的约定：产物保持简短可验收", scope="workspace", kind="preference")
    ranked = store.context_candidates(query="产物简短", limit=8)
    assert ranked[0].content.startswith("新的约定")


# --- 冲突检测 ----------------------------------------------------------------


def test_conflict_detection_marks_old_entry(tmp_path):
    """同域同类型高重合 → 旧条目 superseded；不同类型/不同域不受影响。"""
    store = MemoryStore(tmp_path / "memory.json")
    _create(store, "数据库连接使用 PolarDB 测试库", scope="workspace", kind="fact")
    conflict_new = _create(
        store, "数据库连接使用 PolarDB 测试库，需加白名单", scope="workspace", kind="fact"
    )
    all_active = store.list(scope="workspace", include_inactive=True)
    statuses = {e.id: e.status for e in all_active}
    assert any(s == "superseded" for s in statuses.values())
    assert conflict_new.status == "active"
    # 不同 kind 不互冲突
    _create(store, "数据库连接使用 PolarDB 测试库", scope="workspace", kind="lesson")
    assert not any(
        e.kind == "lesson" and e.status == "superseded"
        for e in store.list(scope="workspace", include_inactive=True)
    )


# --- 并行分支合并仲裁 ---------------------------------------------------------


def test_merge_stage_conclusions_prefers_manual_and_keeps_reference(tmp_path):
    """同主题多分支：人工写入权重高于反思沉淀，落选者带 branch:reference 标签。"""
    store = MemoryStore(tmp_path / "memory.json")
    manual = _create(
        store,
        "子房间结论：采用方案 A 分库分表",
        scope="mission",
        scope_id="ini_1",
        kind="episode",
        tags=["stage:design"],
        source="manual",
    )
    reflection = _create(
        store,
        "子房间结论：采用方案 A 分库分表（含索引细节）",
        scope="mission",
        scope_id="ini_1",
        kind="episode",
        tags=["stage:design"],
        source="reflection",
    )
    merged, refs = merge_stage_conclusions(
        store.list(scope="mission", scope_id="ini_1")
    )
    assert merged[0].source == "manual"
    assert [r.id for r in refs] == [reflection.id]
    assert "branch:reference" in refs[0].tags
    block = store.render_block(merged)
    assert "另一分支结论" in block
    assert manual.id in block and reflection.id in block


# --- 分层摘要降级 -------------------------------------------------------------


def test_build_stage_summary_degrades_without_llm(monkeypatch):
    """无 LLM / 非协调角色时降级为截断流水，绝不抛出。"""
    from sortie_deck.settings import settings

    monkeypatch.setattr(settings, "memory_stage_summary_llm", False)
    out = build_stage_summary.__wrapped__ if hasattr(build_stage_summary, "__wrapped__") else None
    import asyncio

    text = "决" * 1000
    summary = asyncio.run(
        build_stage_summary(stage="intake", role="product", reply_text=text, harness="mock")
    )
    assert summary.startswith("Stage `intake`")
    assert len(summary) < 800  # 截断流水而非全文
    # 执行角色即使开关打开也走流水（COODINATOR 集合外）
    monkeypatch.setattr(settings, "memory_stage_summary_llm", True)
    summary_eng = asyncio.run(
        build_stage_summary(stage="impl", role="eng", reply_text=text, harness="mock")
    )
    assert summary_eng.startswith("Stage `impl`")
    assert out is None


# --- 反思沉淀淘汰上限 ---------------------------------------------------------


def test_enforce_agent_lesson_cap_evicts_oldest(tmp_path):
    """agent 作用域 lesson 超 cap 淘汰最旧；非 lesson 与其他作用域不受影响。"""
    store = MemoryStore(tmp_path / "memory.json")
    # 内容互不重合，避免触发冲突检测干扰本测试
    contents = [
        "数据库慢查询要先补索引再上线",
        "缓存穿透必须做空值兜底",
        "队列堆积要分批消费",
        "异步回调失败要幂等重试",
        "批量任务要支持断点续跑",
    ]
    ids = []
    for content in contents:
        e = _create(
            store,
            content,
            scope="agent",
            scope_id="eng",
            kind="lesson",
            source="reflection",
        )
        ids.append(e.id)
    # 干扰项：不同 kind / 不同作用域不参与淘汰
    _create(store, "偏好 1", scope="agent", scope_id="eng", kind="preference")
    _create(store, "任务记忆 1", scope="mission", scope_id="ini_9", kind="lesson")
    removed = enforce_agent_lesson_cap(store, cap=3)
    assert removed == 2
    remain = {e.id for e in store.list(scope="agent", scope_id="eng") if e.kind == "lesson"}
    assert remain == set(ids[-3:])
