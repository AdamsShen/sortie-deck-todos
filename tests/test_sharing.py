"""任务共享功能测试：模型持久化 + 访问控制逻辑。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from sortie_deck.api.app import (
    _ensure_mission_access,
    _ensure_mission_owner,
    _is_mission_shared_with,
    _visible_initiatives,
)
from sortie_deck.auth import PublicUser
from sortie_deck.models import Initiative, MissionShare


def _user(uid: str, role: str = "member") -> PublicUser:
    return PublicUser(id=uid, username=uid, display_name=uid, role=role)  # type: ignore[arg-type]


def _ini(owner: str | None, shared: list[str] | None = None) -> Initiative:
    ini = Initiative(title="t", brief="b", created_by=owner)
    for uid in shared or []:
        ini.shared_with.append(MissionShare(user_id=uid, username=uid))
    return ini


def test_shared_with_persists_in_model_dump():
    """共享记录随任务模型 JSON 序列化往返，store 落盘无需特殊处理。"""
    ini = _ini("usr_owner", ["usr_a", "usr_b"])
    data = ini.model_dump(mode="json")
    restored = Initiative.model_validate(data)
    assert [s.user_id for s in restored.shared_with] == ["usr_a", "usr_b"]


def test_is_mission_shared_with():
    ini = _ini("usr_owner", ["usr_a"])
    assert _is_mission_shared_with(ini, "usr_a")
    assert not _is_mission_shared_with(ini, "usr_b")
    assert not _is_mission_shared_with(_ini(None), "usr_a")


def test_access_owner_and_admin_allowed():
    ini = _ini("usr_owner")
    # 创建者本人放行
    _ensure_mission_access(ini, _user("usr_owner"))
    # admin 放行
    _ensure_mission_access(ini, _user("usr_root", "admin"))


def test_access_shared_user_allowed():
    """被共享用户拥有除删除外的访问权限。"""
    ini = _ini("usr_owner", ["usr_shared"])
    _ensure_mission_access(ini, _user("usr_shared"))


def test_access_stranger_rejected():
    """未共享的其他账号 403。"""
    ini = _ini("usr_owner", ["usr_shared"])
    with pytest.raises(HTTPException) as ei:
        _ensure_mission_access(ini, _user("usr_stranger"))
    assert ei.value.status_code == 403


def test_access_legacy_task_without_owner_open():
    """旧数据 created_by 为 None 时放行（保持历史行为）。"""
    _ensure_mission_access(_ini(None), _user("usr_any"))


def test_delete_owner_only():
    """删除是 owner 专属：被共享用户与陌生人一律 403，仅创建者/admin 可删。"""
    ini = _ini("usr_owner", ["usr_shared"])
    _ensure_mission_owner(ini, _user("usr_owner"))
    _ensure_mission_owner(ini, _user("usr_root", "admin"))
    with pytest.raises(HTTPException) as ei1:
        _ensure_mission_owner(ini, _user("usr_shared"))
    assert ei1.value.status_code == 403
    with pytest.raises(HTTPException) as ei2:
        _ensure_mission_owner(ini, _user("usr_stranger"))
    assert ei2.value.status_code == 403


def test_visible_initiatives_filter():
    """列表按账号过滤：创建者可见自己的 + 被共享的 + 旧数据任务；admin 全可见。"""
    mine = _ini("usr_me")
    shared_to_me = _ini("usr_other", ["usr_me"])
    stranger = _ini("usr_other", ["usr_zzz"])
    legacy = _ini(None)
    items = [mine, shared_to_me, stranger, legacy]

    me = _visible_initiatives(items, _user("usr_me"))
    assert [i.created_by for i in me] == ["usr_me", "usr_other", None]

    admin = _visible_initiatives(items, _user("usr_root", "admin"))
    assert len(admin) == 4
