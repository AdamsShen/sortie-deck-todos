"""任务共享功能测试：模型持久化 + 访问控制逻辑。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from sortie_deck.api.app import (
    _ensure_mission_access,
    _ensure_mission_control,
    _ensure_mission_delete,
    _ensure_mission_share_owner,
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


def test_access_legacy_task_without_owner_admin_only():
    """无归属历史任务仅 admin 可访问，普通成员需回填后由 owner 显式共享。"""
    with pytest.raises(HTTPException) as ei:
        _ensure_mission_access(_ini(None), _user("usr_any"))
    assert ei.value.status_code == 403
    _ensure_mission_access(_ini(None), _user("usr_root", "admin"))


def test_access_legacy_task_with_explicit_share_allowed():
    """无归属任务被显式共享后，被共享者仍可访问（共享优先于归属兜底）。"""
    _ensure_mission_access(_ini(None, ["usr_shared"]), _user("usr_shared"))


def test_delete_owner_only():
    """解散（删除）：被共享用户与陌生人 403，创建者与 admin 可删。"""
    ini = _ini("usr_owner", ["usr_shared"])
    _ensure_mission_delete(ini, _user("usr_owner"))
    _ensure_mission_delete(ini, _user("usr_root", "admin"))
    with pytest.raises(HTTPException) as ei1:
        _ensure_mission_delete(ini, _user("usr_shared"))
    assert ei1.value.status_code == 403
    with pytest.raises(HTTPException) as ei2:
        _ensure_mission_delete(ini, _user("usr_stranger"))
    assert ei2.value.status_code == 403


def test_control_owner_and_shared_user_allowed():
    """运行控制权：创建者与被显式共享用户可以启动/停止/审批。"""
    ini = _ini("usr_owner", ["usr_shared"])
    _ensure_mission_control(ini, _user("usr_owner"))
    _ensure_mission_control(ini, _user("usr_shared"))


def test_control_admin_readonly_for_others():
    """admin 对他人任务只读：不能控制运行流程（可查看、可解散）。"""
    ini = _ini("usr_owner", ["usr_shared"])
    with pytest.raises(HTTPException) as ei:
        _ensure_mission_control(ini, _user("usr_root", "admin"))
    assert ei.value.status_code == 403
    # 但访问与解散仍然放行
    _ensure_mission_access(ini, _user("usr_root", "admin"))
    _ensure_mission_delete(ini, _user("usr_root", "admin"))


def test_control_admin_as_creator_or_shared_allowed():
    """admin 本人是创建者、或被 owner 显式共享时，同样拥有控制权。"""
    _ensure_mission_control(_ini("usr_root"), _user("usr_root", "admin"))
    _ensure_mission_control(_ini("usr_owner", ["usr_root"]), _user("usr_root", "admin"))


def test_share_management_creator_only():
    """共享管理仅创建者：admin 不能把他人任务共享出去。"""
    ini = _ini("usr_owner", ["usr_shared"])
    _ensure_mission_share_owner(ini, _user("usr_owner"))
    with pytest.raises(HTTPException) as ei_admin:
        _ensure_mission_share_owner(ini, _user("usr_root", "admin"))
    assert ei_admin.value.status_code == 403
    with pytest.raises(HTTPException) as ei_shared:
        _ensure_mission_share_owner(ini, _user("usr_shared"))
    assert ei_shared.value.status_code == 403


def test_visible_initiatives_filter():
    """列表按账号过滤：仅自己的 + 被共享的；无归属任务仅 admin 可见。"""
    mine = _ini("usr_me")
    shared_to_me = _ini("usr_other", ["usr_me"])
    stranger = _ini("usr_other", ["usr_zzz"])
    legacy = _ini(None)
    legacy_shared_to_me = _ini(None, ["usr_me"])
    items = [mine, shared_to_me, stranger, legacy, legacy_shared_to_me]

    me = _visible_initiatives(items, _user("usr_me"))
    assert [i.created_by for i in me] == ["usr_me", "usr_other", None]
    assert [s.user_id for s in me[-1].shared_with] == ["usr_me"]

    admin = _visible_initiatives(items, _user("usr_root", "admin"))
    assert len(admin) == 5
