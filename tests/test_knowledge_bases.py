"""知识库库实体（KnowledgeBaseStore / scope 权限）测试。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from sortie_deck.api.app import _ensure_base_write
from sortie_deck.auth import PublicUser
from sortie_deck.knowledge import (
    CreateKnowledgeBaseRequest,
    CreateKnowledgeRequest,
    KnowledgeBaseStore,
    KnowledgeStore,
    UpdateKnowledgeBaseRequest,
)
from sortie_deck.knowledge_gateway import ImportKnowledgeRequest, KnowledgeGateway


def _user(uid: str, role: str = "member") -> PublicUser:
    return PublicUser(id=uid, username=uid, display_name=uid, role=role)  # type: ignore[arg-type]


def test_base_store_seeds_default_global(tmp_path):
    store = KnowledgeBaseStore(tmp_path / "kb_bases.json")
    default = store.ensure_default()
    assert default.scope == "global"
    assert default.scope_id == "global"
    # 幂等：重复调用不产生新库
    again = store.ensure_default()
    assert again.id == default.id
    assert len([b for b in store.list() if b.scope == "global"]) == 1


def test_base_store_scope_id_rules(tmp_path):
    store = KnowledgeBaseStore(tmp_path / "kb_bases.json")
    store.ensure_default()
    mine = store.create(
        CreateKnowledgeBaseRequest(name="我的笔记", scope="user"),
        user_id="usr_a",
        user_name="Ada",
    )
    assert mine.scope == "user"
    assert mine.scope_id == "usr_a"
    assert mine.created_by == "usr_a"

    task = store.create(
        CreateKnowledgeBaseRequest(name="任务资料", scope="task", task_id="ini_123"),
        user_id="usr_a",
        user_name="Ada",
    )
    assert task.scope_id == "ini_123"

    global_base = store.create(
        CreateKnowledgeBaseRequest(name="共享手册", scope="global"),
        user_id="usr_root",
        user_name="Root",
    )
    assert global_base.scope_id == "global"

    updated = store.update(mine.id, UpdateKnowledgeBaseRequest(description="个人沉淀"))
    assert updated.description == "个人沉淀"

    store.delete(task.id)
    assert all(b.id != task.id for b in store.list())
    with pytest.raises(KeyError):
        store.get(task.id)


def test_doc_base_id_filter_and_default(tmp_path):
    doc_store = KnowledgeStore(tmp_path / "knowledge.json")
    base_store = KnowledgeBaseStore(tmp_path / "kb_bases.json")
    default = base_store.ensure_default()
    mine = base_store.create(
        CreateKnowledgeBaseRequest(name="我的", scope="user"),
        user_id="usr_a",
        user_name="Ada",
    )
    # 存量种子文档 base_id 为空 → 归默认全局库
    seeded = doc_store.list()
    assert all(d.base_id == "" for d in seeded)

    mine_doc = doc_store.create(
        CreateKnowledgeRequest(title="私人文档", body="secret", base_id=mine.id),
        author_id="usr_a",
        author_name="Ada",
    )
    assert doc_store.list(base_id=mine.id)[0].id == mine_doc.id
    assert all(d.base_id != mine.id for d in doc_store.list(base_id=default.id))


def test_gateway_local_import_carries_base_id(tmp_path):
    doc_store = KnowledgeStore(tmp_path / "knowledge.json")
    gw = KnowledgeGateway(doc_store, backend="local")
    doc = gw.import_doc(
        ImportKnowledgeRequest(title="导入的笔记", body="# md 内容", base_id="kbs_x"),
        author_id="usr_a",
        author_name="Ada",
    )
    assert doc.base_id == "kbs_x"


def test_base_write_permissions():
    global_base = KnowledgeBaseStore.__new__(KnowledgeBaseStore)  # 不落盘，仅用模型字段
    from sortie_deck.knowledge import KnowledgeBaseModel

    global_base = KnowledgeBaseModel(name="g", scope="global", scope_id="global")
    mine = KnowledgeBaseModel(name="m", scope="user", scope_id="usr_a", created_by="usr_a")
    other = KnowledgeBaseModel(name="o", scope="user", scope_id="usr_b", created_by="usr_b")

    # global：member 403，admin 放行
    with pytest.raises(HTTPException) as ei:
        _ensure_base_write(_user("usr_a"), global_base)
    assert ei.value.status_code == 403
    _ensure_base_write(_user("usr_root", "admin"), global_base)

    # user 库：所有者放行，他人 403
    _ensure_base_write(_user("usr_a"), mine)
    with pytest.raises(HTTPException) as ei2:
        _ensure_base_write(_user("usr_a"), other)
    assert ei2.value.status_code == 403
    _ensure_base_write(_user("usr_b"), other)
