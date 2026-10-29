"""小群可见性：只有任务所有权人（created_by）能看，共享用户与 admin 都不例外。

这些用例直接调用 API 路由函数（不走 ASGI），用 monkeypatch 注入任务列表与
sub-room 服务，覆盖三条入口：列表、对话正文、以及 owner 判定本身。
"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from sortie_deck.auth import PublicUser
from sortie_deck.rooms import RoomStore
from sortie_deck.subroom import SubRoom, SubRoomService, SubRoomStore

# 注意：sortie_deck.api.app 是路由模块本身，直接 from 导入拿到的是 FastAPI 实例
api = import_module("sortie_deck.api.app")


def _user(uid: str, role: str = "member") -> PublicUser:
    return PublicUser(id=uid, username=uid, display_name=uid, role=role)


def _mission(mid: str, owner: str, shared: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        id=mid,
        created_by=owner,
        shared_with=[SimpleNamespace(user_id=u) for u in shared],
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """两个任务：ini_mine 属于 usr_owner（共享给 usr_shared），ini_other 属于 usr_other。"""
    missions = [
        _mission("ini_mine", "usr_owner", shared=("usr_shared",)),
        _mission("ini_other", "usr_other"),
    ]
    fake_orch = SimpleNamespace(
        list_initiatives=lambda: missions,
        get=lambda mid: next((m for m in missions if m.id == mid), None),
    )
    monkeypatch.setattr(api, "orch", lambda: fake_orch)

    store = SubRoomStore(tmp_path / "subrooms.json")
    mine = store.save(
        SubRoom(
            initiative_id="ini_mine",
            parent_stage="build",
            initiator_role="eng",
            goal="对齐验收口径",
        )
    )
    store.save(
        SubRoom(
            initiative_id="ini_other",
            parent_stage="prd",
            initiator_role="product",
            goal="别人的小群",
        )
    )
    service = SubRoomService(store=store, rooms=RoomStore(tmp_path / "rooms"), enabled=True)
    monkeypatch.setattr("sortie_deck.subroom.get_subroom_service", lambda: service)
    return SimpleNamespace(mine=mine, service=service)


async def test_owner_only_sees_own_missions_subrooms(env) -> None:
    rows = await api.list_subrooms(initiative_id=None, user=_user("usr_owner"))
    assert [r["initiative_id"] for r in rows] == ["ini_mine"]


async def test_owner_can_filter_by_own_mission(env) -> None:
    rows = await api.list_subrooms(initiative_id="ini_mine", user=_user("usr_owner"))
    assert len(rows) == 1


async def test_shared_user_cannot_read_subrooms(env) -> None:
    """共享关系不向下传递：被共享用户能看任务，但看不到小群。"""
    with pytest.raises(HTTPException) as exc:
        await api.list_subrooms(initiative_id="ini_mine", user=_user("usr_shared"))
    assert exc.value.status_code == 403


async def test_admin_without_ownership_is_denied(env) -> None:
    with pytest.raises(HTTPException) as exc:
        await api.list_subrooms(initiative_id="ini_mine", user=_user("usr_admin", role="admin"))
    assert exc.value.status_code == 403


async def test_admin_sees_subrooms_of_a_mission_they_own(tmp_path, monkeypatch) -> None:
    """角色不参与判定：admin 作为创建者照样能看到自己的小群。"""
    missions = [_mission("ini_admin", "usr_admin")]
    monkeypatch.setattr(
        api,
        "orch",
        lambda: SimpleNamespace(
            list_initiatives=lambda: missions,
            get=lambda mid: next((m for m in missions if m.id == mid), None),
        ),
    )
    store = SubRoomStore(tmp_path / "subrooms.json")
    store.save(
        SubRoom(initiative_id="ini_admin", parent_stage="build", initiator_role="eng", goal="自己的群")
    )
    service = SubRoomService(store=store, rooms=RoomStore(tmp_path / "rooms"), enabled=True)
    monkeypatch.setattr("sortie_deck.subroom.get_subroom_service", lambda: service)

    rows = await api.list_subrooms(initiative_id=None, user=_user("usr_admin", role="admin"))
    assert [r["initiative_id"] for r in rows] == ["ini_admin"]


async def test_transcript_is_owner_only(env) -> None:
    sub_id = env.mine.id
    assert await api.get_subroom_transcript(sub_id, user=_user("usr_owner")) == []
    for outsider in (_user("usr_shared"), _user("usr_admin", role="admin"), _user("usr_other")):
        with pytest.raises(HTTPException) as exc:
            await api.get_subroom_transcript(sub_id, user=outsider)
        assert exc.value.status_code == 403


async def test_transcript_unknown_id_is_404(env) -> None:
    with pytest.raises(HTTPException) as exc:
        await api.get_subroom_transcript("sub_missing", user=_user("usr_owner"))
    assert exc.value.status_code == 404
