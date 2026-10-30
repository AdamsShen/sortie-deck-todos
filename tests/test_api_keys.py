"""P0：用户级 API Key 与 service 层权限。"""

from __future__ import annotations

import pytest

from sortie_deck import service as service_mod
from sortie_deck.api_keys import KEY_PREFIX, ApiKeyStore
from sortie_deck.auth import AuthStore, CreateUserRequest
from sortie_deck.models import Initiative, MissionShare
from sortie_deck.service import (
    MissionService,
    Principal,
    ServiceError,
    ensure_access,
    ensure_control,
    resolve_principal,
)


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    auth = AuthStore(tmp_path / "users.json", token_secret="test-secret")
    keys = ApiKeyStore(tmp_path / "api_keys.sqlite")
    monkeypatch.setattr(service_mod, "_auth", auth)
    monkeypatch.setattr(service_mod, "_api_keys", keys)
    user = auth.create_user(
        CreateUserRequest(username="ada", password="pw123456", display_name="Ada")
    )
    other = auth.create_user(
        CreateUserRequest(username="bob", password="pw123456", display_name="Bob")
    )
    yield auth, keys, user, other
    keys.close()


def test_key_is_stored_hashed_and_prefix_only(stores):
    _auth, keys, user, _other = stores
    key, plaintext = keys.create(user_id=user.id, name="我的 MacBook")

    assert plaintext.startswith(KEY_PREFIX)
    assert key.prefix == plaintext[: len(key.prefix)]
    # 明文与完整哈希都不应出现在列表结果里
    listed = keys.list_for_user(user.id)
    assert len(listed) == 1
    assert plaintext not in listed[0].model_dump_json()

    raw_row = keys._conn.execute("SELECT key_hash FROM api_keys WHERE id=?", (key.id,)).fetchone()
    assert raw_row is not None
    assert raw_row[0] != plaintext
    assert len(raw_row[0]) == 64  # sha256 hex


def test_verify_resolves_user_and_updates_last_used(stores):
    _auth, keys, user, _other = stores
    _key, plaintext = keys.create(user_id=user.id, name="cli")

    principal = resolve_principal(f"Bearer {plaintext}", source="cli")
    assert principal.user.id == user.id
    assert principal.source == "cli"
    assert principal.key_id is not None

    verified = keys.verify(plaintext)
    assert verified is not None
    assert verified.last_used_at is not None


def test_revoked_key_is_rejected(stores):
    _auth, keys, user, _other = stores
    key, plaintext = keys.create(user_id=user.id, name="to-revoke")
    assert keys.revoke(user_id=user.id, key_id=key.id) is True
    assert keys.verify(plaintext) is None
    with pytest.raises(ServiceError) as exc:
        resolve_principal(f"Bearer {plaintext}")
    assert exc.value.code == "auth_required"


def test_invalid_credential_is_rejected(stores):
    with pytest.raises(ServiceError) as exc:
        resolve_principal("Bearer sk_sortie_not-a-real-key")
    assert exc.value.code == "auth_required"


def test_missing_credential_is_rejected(stores):
    with pytest.raises(ServiceError) as exc:
        resolve_principal(None)
    assert exc.value.code == "auth_required"


def _mission(owner_id: str, *, shared_with: list[str] | None = None) -> Initiative:
    ini = Initiative(title="m", brief="b", created_by=owner_id)
    for uid in shared_with or []:
        ini.shared_with.append(
            MissionShare(
                user_id=uid, username=uid, display_name=uid, added_by=owner_id
            )
        )
    return ini


def test_permission_matrix(stores):
    _auth, _keys, ada, bob = stores
    owner = Principal(user=ada, source="cli")
    stranger = Principal(user=bob, source="cli")

    ini = _mission(ada.id)
    ensure_access(ini, owner)
    ensure_control(ini, owner)

    with pytest.raises(ServiceError) as exc:
        ensure_access(ini, stranger)
    assert exc.value.code == "forbidden"
    with pytest.raises(ServiceError):
        ensure_control(ini, stranger)


def test_shared_user_can_access_but_admin_control_is_readonly(stores):
    _auth, _keys, ada, bob = stores
    shared = Principal(user=bob, source="web")

    ini = _mission(ada.id, shared_with=[bob.id])
    ensure_access(ini, shared)
    ensure_control(ini, shared)  # 被共享者有控制权

    # admin 对他人任务只读
    admin = Principal(user=ada.model_copy(update={"role": "admin"}), source="web")
    other_ini = _mission("usr_someone_else")
    ensure_access(other_ini, admin)
    with pytest.raises(ServiceError):
        ensure_control(other_ini, admin)


def test_list_mine_excludes_others(stores, tmp_path, monkeypatch):
    _auth, _keys, ada, bob = stores
    mine = _mission(ada.id)
    other = _mission(bob.id)

    svc = MissionService(orchestrator=None)
    monkeypatch.setattr(MissionService, "orch", property(lambda self: _FakeOrch([mine, other])))
    listed = svc.list_mine(Principal(user=ada, source="cli"))
    assert [i.id for i in listed] == [mine.id]


class _FakeOrch:
    def __init__(self, items: list[Initiative]) -> None:
        self._items = items

    def list_initiatives(self) -> list[Initiative]:
        return self._items


@pytest.mark.asyncio
async def test_keys_http_roundtrip(stores):
    """HTTP 层：建 Key → 用 Key 当凭据 → 吊销后立即 401。"""
    from httpx import ASGITransport, AsyncClient

    from sortie_deck.api.app import app as fastapi_app

    auth, _keys, _user, _other = stores
    token = auth.login("ada", "pw123456").token
    transport = ASGITransport(app=fastapi_app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/keys",
            json={"name": "macbook"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        plaintext = body["plaintext"]
        assert plaintext.startswith(KEY_PREFIX)
        assert body["mcp"]["url"].endswith("/mcp")
        assert plaintext in body["mcp"]["command"]

        listed = await client.get(
            "/api/keys", headers={"Authorization": f"Bearer {plaintext}"}
        )
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        assert "plaintext" not in rows[0]

        revoked = await client.delete(
            f"/api/keys/{rows[0]['id']}", headers={"Authorization": f"Bearer {token}"}
        )
        assert revoked.status_code == 200

        after = await client.get(
            "/api/keys", headers={"Authorization": f"Bearer {plaintext}"}
        )
        assert after.status_code == 401


def test_service_rejects_cross_user_access(stores, tmp_path):
    """A 的凭据拿不到 B 的任务——MCP/CLI 的越权都收敛在这里。"""
    _auth, _keys, ada, bob = stores
    bob_mission = _mission(bob.id)

    class _StubOrch:
        def get(self, mission_id: str):
            return bob_mission if mission_id == bob_mission.id else None

        def list_initiatives(self):
            return [bob_mission]

    svc = MissionService(_StubOrch())  # type: ignore[arg-type]
    with pytest.raises(ServiceError) as exc:
        svc.get(Principal(user=ada, source="mcp"), bob_mission.id)
    assert exc.value.code == "forbidden"

    # bob 自己能拿到
    assert svc.get(Principal(user=bob, source="mcp"), bob_mission.id).id == bob_mission.id
