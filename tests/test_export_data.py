"""导出接口单测：结构契约、用户隔离、脱敏与边界（PRD A–E）。

直接调用路由函数与 ``build_user_export``（不起 TestClient）：导出逻辑只依赖
注入的存储单例，用假存储即可覆盖数据隔离与敏感字段剔除，避免写脏真实 data/。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from sortie_deck import export_data
from sortie_deck.auth import PublicUser

app_mod = importlib.import_module("sortie_deck.api.app")

OWNER = "usr_ada"
OTHER = "usr_bob"
# 构造的已知敏感值：导出全文里绝不能出现（PRD B-5/E-5）
PASSWORD_HASH = "$pbkdf2-sha256$secret-hash-value"
FULL_KEY_SECRET = "sk_sortie_full_secret_do_not_export"
BEARER_TOKEN = "usr_ada.payload.signature"


def _user(uid: str = OWNER, username: str = "ada") -> PublicUser:
    return PublicUser(
        id=uid,
        username=username,
        display_name=username.title(),
        role="member",  # type: ignore[arg-type]
        status="active",
    )


class _AuthStore:
    """假 AuthStore：只暴露导出需要的 get_by_id（内部 User 含 password_hash）。"""

    def get_by_id(self, user_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=user_id,
            created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            password_hash=PASSWORD_HASH,
        )


class _ApiKeyStore:
    def __init__(self, keys: list[SimpleNamespace]) -> None:
        self._keys = keys

    def list_for_user(self, user_id: str) -> list[SimpleNamespace]:
        return [k for k in self._keys if k.user_id == user_id]


class _MemoryStore:
    """假 MemoryStore：记录调用参数并模拟 user 域可见性。"""

    def __init__(self, entries: list[SimpleNamespace]) -> None:
        self.entries = entries
        self.calls: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> list[SimpleNamespace]:
        self.calls.append(kwargs)
        return [
            e
            for e in self.entries
            if e.scope == kwargs.get("scope")
            and e.scope_id == kwargs.get("user_id")
        ]


class _RoomStore:
    def __init__(self, messages: dict[str, list[SimpleNamespace]]) -> None:
        self.messages = messages
        self.calls: list[tuple[str, int]] = []

    def list(self, initiative_id: str, limit: int = 500, **_kw: object) -> list[SimpleNamespace]:
        self.calls.append((initiative_id, limit))
        return self.messages.get(initiative_id, [])[-limit:]


def _mission(
    mission_id: str,
    title: str,
    *,
    created_by: str = OWNER,
    shared_with: list[str] | None = None,
    status: str = "draft",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=mission_id,
        title=title,
        status=status,
        current_stage="intake",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 2, 2, tzinfo=UTC),
        created_by=created_by,
        shared_with=[SimpleNamespace(user_id=uid) for uid in (shared_with or [])],
    )


def _key(key_id: str, user_id: str = OWNER) -> SimpleNamespace:
    return SimpleNamespace(
        id=key_id,
        user_id=user_id,
        name="laptop",
        prefix="sk_sortie_ab12",
        created_at=1_700_000_000.0,
        last_used_at=None,
        expires_at=1_800_000_000.0,
        revoked_at=None,
        key_hash=FULL_KEY_SECRET,  # 模拟内部字段：导出不得带出
    )


def _build(
    *,
    user: PublicUser | None = None,
    initiatives: list[SimpleNamespace] | None = None,
    keys: list[SimpleNamespace] | None = None,
    bases: list[SimpleNamespace] | None = None,
    docs: list[SimpleNamespace] | None = None,
    memories: list[SimpleNamespace] | None = None,
    messages: dict[str, list[SimpleNamespace]] | None = None,
) -> dict:
    return export_data.build_user_export(
        user or _user(),
        auth_store=_AuthStore(),
        api_key_store=_ApiKeyStore(keys or []),
        initiatives=initiatives or [],
        knowledge_base_store=SimpleNamespace(list=lambda: bases or []),
        knowledge_store=SimpleNamespace(list=lambda: docs or []),
        memory_store=_MemoryStore(memories or []),
        room_store=_RoomStore(messages or {}),
    )


def test_export_has_exact_top_level_structure() -> None:
    """E-1/E-2/B-4：顶层仅 7 个 key，meta 字段齐全。"""
    payload = _build()

    assert set(payload) == {
        "meta",
        "profile",
        "api_keys",
        "missions",
        "knowledge",
        "memory",
        "rooms",
    }
    assert payload["meta"]["schema_version"] == export_data.EXPORT_SCHEMA_VERSION == 1
    assert payload["meta"]["app"] == "sortie-deck"
    assert payload["meta"]["owner"] == OWNER
    # exported_at 必须能被 ISO-8601 解析
    datetime.fromisoformat(payload["meta"]["exported_at"])


def test_profile_is_whitelisted_object() -> None:
    """B-1/B-5：profile 是对象且含 created_at；不出现 password_hash。"""
    profile = _build()["profile"]

    assert profile["id"] == OWNER
    assert profile["username"] == "ada"
    assert profile["role"] == "member"
    assert profile["created_at"].startswith("2026-01-02T03:04:05")


def test_only_own_data_is_exported() -> None:
    """B-3：他人任务/密钥/知识库/记忆不得进入导出。"""
    initiatives = [
        _mission("ini_own", "mine"),
        _mission("ini_shared", "shared to me", created_by=OTHER, shared_with=[OWNER]),
        _mission("ini_other", "not mine", created_by=OTHER),
    ]
    bases = [
        SimpleNamespace(
            id="kbs_mine",
            name="Mine",
            description="",
            created_by=OWNER,
            created_at="2026-03-01T00:00:00+00:00",
            updated_at="2026-03-01T00:00:00+00:00",
        ),
        SimpleNamespace(
            id="kbs_other",
            name="Other",
            description="",
            created_by=OTHER,
            created_at="2026-03-01T00:00:00+00:00",
            updated_at="2026-03-01T00:00:00+00:00",
        ),
    ]
    docs = [
        SimpleNamespace(base_id="kbs_mine"),
        SimpleNamespace(base_id="kbs_mine"),
        SimpleNamespace(base_id="kbs_other"),
    ]
    memories = [
        SimpleNamespace(
            id="mem_mine",
            scope="user",
            scope_id=OWNER,
            content="my note",
            tags=["tag"],
            created_at="2026-04-01T00:00:00+00:00",
            updated_at="2026-04-01T00:00:00+00:00",
        ),
        SimpleNamespace(
            id="mem_other",
            scope="user",
            scope_id=OTHER,
            content="other note",
            tags=[],
            created_at="2026-04-01T00:00:00+00:00",
            updated_at="2026-04-01T00:00:00+00:00",
        ),
    ]

    payload = _build(
        initiatives=initiatives,
        keys=[_key("key_mine", OWNER), _key("key_other", OTHER)],
        bases=bases,
        docs=docs,
        memories=memories,
        messages={"ini_own": [], "ini_shared": [], "ini_other": []},
    )

    assert [m["id"] for m in payload["missions"]] == ["ini_own", "ini_shared"]
    assert [k["id"] for k in payload["api_keys"]] == ["key_mine"]
    assert [b["id"] for b in payload["knowledge"]] == ["kbs_mine"]
    assert payload["knowledge"][0]["doc_count"] == 2
    assert [m["id"] for m in payload["memory"]] == ["mem_mine"]
    # rooms 与可见任务一一对应，不泄露他人任务
    assert [r["id"] for r in payload["rooms"]] == ["ini_own", "ini_shared"]


def test_memory_query_is_scoped_to_owner() -> None:
    """回归：list(scope="user") 不带 user_id 会读到他人 user 域记忆。"""
    store = _MemoryStore([])
    export_data.build_user_export(
        _user(),
        auth_store=_AuthStore(),
        api_key_store=_ApiKeyStore([]),
        initiatives=[],
        knowledge_base_store=SimpleNamespace(list=lambda: []),
        knowledge_store=SimpleNamespace(list=lambda: []),
        memory_store=store,
        room_store=_RoomStore({}),
    )

    assert store.calls == [{"scope": "user", "user_id": OWNER}]


def test_room_message_count_counts_all_messages() -> None:
    """rooms.message_count 用真实条数，不受 RoomStore.list 默认 500 条截断。"""
    messages = {"ini_big": [SimpleNamespace(id=f"msg_{i}") for i in range(750)]}
    room_store = _RoomStore(messages)
    payload = export_data.build_user_export(
        _user(),
        auth_store=_AuthStore(),
        api_key_store=_ApiKeyStore([]),
        initiatives=[_mission("ini_big", "big room")],
        knowledge_base_store=SimpleNamespace(list=lambda: []),
        knowledge_store=SimpleNamespace(list=lambda: []),
        memory_store=_MemoryStore([]),
        room_store=room_store,
    )

    assert payload["rooms"][0]["message_count"] == 750
    # 实现必须显式传足够大的 limit，而不是依赖默认值
    assert all(limit > 500 for _mid, limit in room_store.calls)


def test_room_message_count_prefers_streaming_count() -> None:
    """FIND-03：存储提供 count 时按行计数，不再整文件构造消息对象。"""

    class _CountingRoomStore:
        def __init__(self) -> None:
            self.count_calls: list[str] = []
            self.list_calls = 0

        def count(self, initiative_id: str) -> int:
            self.count_calls.append(initiative_id)
            return 750

        def list(self, *_a: object, **_k: object) -> list[object]:
            self.list_calls += 1
            return []

    store = _CountingRoomStore()
    payload = export_data.build_user_export(
        _user(),
        auth_store=_AuthStore(),
        api_key_store=_ApiKeyStore([]),
        initiatives=[_mission("ini_big", "big room")],
        knowledge_base_store=SimpleNamespace(list=lambda: []),
        knowledge_store=SimpleNamespace(list=lambda: []),
        memory_store=_MemoryStore([]),
        room_store=store,
    )

    assert payload["rooms"][0]["message_count"] == 750
    assert store.count_calls == ["ini_big"]
    assert store.list_calls == 0


def test_serialize_export_limited_matches_and_aborts() -> None:
    """FIND-02：分块序列化输出与整体序列化一致，超限在分区边界提前抛错。"""
    payload = _build(initiatives=[_mission("ini_own", "mine")])

    assert export_data.serialize_export_limited(payload) == export_data.serialize_export(payload)
    with pytest.raises(export_data.ExportTooLargeError):
        export_data.serialize_export_limited(payload, limit=10)


def test_no_sensitive_values_anywhere() -> None:
    """B-5/E-5：全文不含 password_hash、Key 明文、令牌值。"""
    payload = _build(
        initiatives=[_mission("ini_own", "mine")],
        keys=[_key("key_mine")],
        messages={},
    )
    dumped = json.dumps(payload, ensure_ascii=False)

    assert "password_hash" not in dumped
    assert PASSWORD_HASH not in dumped
    assert FULL_KEY_SECRET not in dumped
    assert BEARER_TOKEN not in dumped
    # 递归断言：任何层级的 key 都不叫 password_hash
    def walk(node: object) -> None:
        if isinstance(node, dict):
            assert "password_hash" not in node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)


def test_empty_user_export_is_valid_shapes() -> None:
    """D-1：空数据用户 6 个分区齐全，profile 为对象、其余为空数组。"""
    payload = _build()
    # meta 也必须是对象
    assert isinstance(payload["meta"], dict)
    assert isinstance(payload["profile"], dict)
    for key in ("api_keys", "missions", "knowledge", "memory", "rooms"):
        assert payload[key] == []


def test_filename_matches_contract() -> None:
    """C-1：sortie-data-{username}-{8 位日期}-{6 位时间}.json。"""
    name = export_data.export_filename(_user(), now=datetime(2026, 11, 2, 10, 15, 0, tzinfo=UTC))
    assert re.fullmatch(r"sortie-data-ada-\d{8}-\d{6}\.json", name)
    assert name == "sortie-data-ada-20261102-101500.json"


def test_serialize_outputs_valid_utf8_json() -> None:
    """E-4/C-2：序列化结果是合法 JSON，且中文不转义。"""
    body = export_data.serialize_export(_build(memories=[]))
    parsed = json.loads(body.decode("utf-8"))
    assert parsed["meta"]["owner"] == OWNER


# ---------------------------------------------------------------------------
# 路由层：Content-Disposition / 413 / 鉴权依赖
# ---------------------------------------------------------------------------


def _patch_route_deps(
    monkeypatch: pytest.MonkeyPatch, room_store: _RoomStore | None = None
) -> None:
    from sortie_deck import service as service_mod

    monkeypatch.setattr(app_mod, "get_auth", lambda: _AuthStore())
    monkeypatch.setattr(service_mod, "get_api_key_store", lambda: _ApiKeyStore([]))
    monkeypatch.setattr(app_mod, "get_knowledge_bases", lambda: SimpleNamespace(list=lambda: []))
    monkeypatch.setattr(app_mod, "get_knowledge", lambda: SimpleNamespace(list=lambda: []))
    monkeypatch.setattr(
        app_mod, "get_memory", lambda: _MemoryStore([])
    )
    monkeypatch.setattr(
        app_mod,
        "orch",
        lambda: SimpleNamespace(list_initiatives=lambda: [], rooms=room_store or _RoomStore({})),
    )


def test_route_returns_json_with_download_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-1：200 + application/json + Content-Disposition 文件名。"""
    _patch_route_deps(monkeypatch)

    response = asyncio.run(app_mod.export_user_data(user=_user()))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="sortie-data-ada-')
    assert disposition.endswith('.json"')
    assert response.headers["cache-control"] == "no-store"
    body = json.loads(response.body)
    assert body["meta"]["owner"] == OWNER


def test_route_rejects_oversized_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-4：超过 20 MB 上限返回 413 与明确文案，不返回半截文件。"""
    _patch_route_deps(monkeypatch)
    monkeypatch.setattr(export_data, "MAX_EXPORT_BYTES", 16)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(app_mod.export_user_data(user=_user()))

    assert excinfo.value.status_code == 413
    assert "导出数据过大" in str(excinfo.value.detail)
