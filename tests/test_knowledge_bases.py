"""知识库库实体（KnowledgeBaseStore / scope 权限）测试。"""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from httpx import MockTransport, Response

from sortie_deck.api.app import _ensure_base_write
from sortie_deck.auth import PublicUser
from sortie_deck.knowledge import (
    CreateKnowledgeBaseRequest,
    CreateKnowledgeRequest,
    KnowledgeBaseModel,
    KnowledgeBaseStore,
    KnowledgeDoc,
    KnowledgeStore,
    UpdateKnowledgeBaseRequest,
    UpdateKnowledgeRequest,
)
from sortie_deck.knowledge_gateway import ImportKnowledgeRequest, KnowledgeGateway
from sortie_deck.weknora import WeKnoraClient


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


def _store_with_weknora_doc(tmp_path, base_id: str, remote_id: str, parse_status: str) -> tuple[KnowledgeStore, str]:
    doc_store = KnowledgeStore(tmp_path / "docs.json")
    doc = doc_store.create(
        CreateKnowledgeRequest(title="远端文档", body="", base_id=base_id),
        author_id="usr_a",
        author_name="Ada",
    )
    doc_store.update(doc.id, UpdateKnowledgeRequest(parse_status=parse_status))
    # 按 id 精确补 remote_id（_read 按更新时间排序，不能用下标定位）
    with doc_store._lock:
        docs = doc_store._read()
        for i, d in enumerate(docs):
            if d.id != doc.id:
                continue
            data = d.model_dump()
            data["remote_id"] = remote_id
            data["backend"] = "weknora"
            docs[i] = KnowledgeDoc.model_validate(data)
        doc_store._write(docs)
    return doc_store, doc.id


def test_refresh_parse_status_updates_weknora_docs(tmp_path):
    """refresh_parse_status：按 remote_id 映射 WeKnora 最新 parse_status 并回写 catalog。"""
    base_store = KnowledgeBaseStore(tmp_path / "bases.json")
    base = base_store.create(
        CreateKnowledgeBaseRequest(name="刷新测试", scope="user"),
        user_id="usr_a",
        user_name="Ada",
    )
    doc_store, doc_id = _store_with_weknora_doc(tmp_path, base.id, "rk_1", "processing")

    def handler(request):
        row = {"id": "rk_1", "title": "远端文档", "parse_status": "completed"}
        return Response(200, json={"data": [row]})

    client = WeKnoraClient("http://fake", client=httpx.Client(transport=MockTransport(handler)))
    gw = KnowledgeGateway(doc_store, backend="weknora", weknora_kb_id="kb1", client=client)
    updated = gw.refresh_parse_status(base.id)
    assert updated[0].parse_status == "completed"
    assert doc_store.get(doc_id).parse_status == "completed"


def test_refresh_parse_status_local_mode_noop(tmp_path):
    """local 模式无远程状态：原样返回，不报错。"""
    base_store = KnowledgeBaseStore(tmp_path / "bases.json")
    base = base_store.create(
        CreateKnowledgeBaseRequest(name="本地库", scope="user"),
        user_id="usr_a",
        user_name="Ada",
    )
    doc_store = KnowledgeStore(tmp_path / "docs.json")
    doc_store.create(
        CreateKnowledgeRequest(title="本地文档", body="x", base_id=base.id),
        author_id="usr_a",
        author_name="Ada",
    )
    gw = KnowledgeGateway(doc_store, backend="local")
    updated = gw.refresh_parse_status(base.id)
    assert len(updated) == 1
    assert updated[0].parse_status == ""
