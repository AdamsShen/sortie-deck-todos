from __future__ import annotations

import pytest

from sortie_deck.agents import (
    AgentCatalog,
    CreateAgentRequest,
    UpdateAgentRequest,
)
from sortie_deck.memory import (
    CONTEXT_BLOCK_MAX_CHARS,
    CreateMemoryRequest,
    MemoryStore,
    UpdateMemoryRequest,
)


def test_memory_scopes_and_context(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    assert store.list(scope="workspace")
    mine = store.create(
        CreateMemoryRequest(scope="user", kind="preference", content="Prefer TypeScript."),
        author_id="usr_a",
        author_name="Ada",
    )
    assert mine.scope_id == "usr_a"
    store.create(
        CreateMemoryRequest(
            scope="mission",
            scope_id="ini_1",
            kind="episode",
            content="Customer wants SSO first.",
            tags=["customer"],
        ),
        author_id="usr_a",
        author_name="Ada",
    )
    brief = store.context_block(user_id="usr_a", mission_id="ini_1")
    assert "TypeScript" in brief
    assert "SSO" in brief
    store.update(mine.id, UpdateMemoryRequest(content="Prefer TypeScript strict."))
    store.delete(mine.id)


def test_agent_catalog_publish_and_slot_map(tmp_path):
    catalog = AgentCatalog(tmp_path / "agents.json")
    assert len(catalog.list()) >= 3
    created = catalog.create(
        CreateAgentRequest(
            title="Payments QA",
            slot="qa",
            summary="Knows PCI + refund paths",
            persona="You are a payments QA specialist.",
            tags=["payments", "qa"],
        ),
        author_id="usr_b",
        author_name="Bob",
    )
    assert created.slug
    mapped = catalog.resolve_slot_map({"qa": created.id})
    assert "qa" in {r.value for r in mapped}
    catalog.update(
        created.id,
        UpdateAgentRequest(summary="PCI + refund + chargeback"),
        editor_id="usr_b",
        is_admin=False,
    )
    catalog.delete(created.id, editor_id="usr_b", is_admin=False)


def test_non_author_cannot_update_or_delete_agent(tmp_path):
    catalog = AgentCatalog(tmp_path / "agents.json")
    agent = catalog.create(
        CreateAgentRequest(
            title="Owner only",
            slot="eng",
            summary="private edits",
            persona="Eng persona",
        ),
        author_id="owner",
        author_name="Owner",
    )
    with pytest.raises(PermissionError):
        catalog.update(
            agent.id,
            UpdateAgentRequest(summary="hijacked"),
            editor_id="intruder",
            is_admin=False,
        )
    with pytest.raises(PermissionError):
        catalog.delete(agent.id, editor_id="intruder", is_admin=False)


def test_memory_context_block_truncates(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    for i in range(150):
        store.create(
            CreateMemoryRequest(scope="workspace", kind="fact", content=f"Line {i}: " + ("x" * 400)),
            author_id="u1",
            author_name="Ada",
        )
    block = store.context_block(limit=150)
    assert block.startswith("Memory brief:")
    assert len(block) <= CONTEXT_BLOCK_MAX_CHARS
    assert block.endswith("...")


def test_memory_context_block_uses_summaries_and_scoring(tmp_path):
    """注入只带简介行 + query 相关性排序（新注入架构语义）。"""
    store = MemoryStore(tmp_path / "memory.json")
    store.create(
        CreateMemoryRequest(scope="workspace", kind="lesson", content="部署前先跑 lint 与单测，双绿再发车。"),
        author_id="u1",
        author_name="Ada",
    )
    store.create(
        CreateMemoryRequest(scope="workspace", kind="preference", content="回复与代码注释统一用中文。"),
        author_id="u1",
        author_name="Ada",
    )
    entries = store.context_candidates(query="部署 lint 单测", limit=8)
    assert entries[0].content.startswith("部署前先跑 lint")
    block = store.render_block(entries)
    assert "mem_" in block  # 简介行带 id，供 Agent 按需取全文
    assert "x" * 400 not in block  # 不注入全文


def test_memory_index_rebuilt_on_write(tmp_path):
    """索引文件随写入重建，包含 id 与简介行。"""
    store = MemoryStore(tmp_path / "memory.json")
    store.create(
        CreateMemoryRequest(scope="workspace", kind="fact", content="测试索引。"),
        author_id="u1",
        author_name="Ada",
    )
    assert store.index_path.exists()
    text = store.index_path.read_text(encoding="utf-8")
    assert "mem_" in text and "测试索引" in text
