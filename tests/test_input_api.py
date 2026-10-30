"""任务输入快照 API：上传 / 列表 / 置顶 / 删除 + 权限与配额。

直接调用路由函数（而不是起 TestClient）：路由只依赖 ``settings`` 与两个全局
单例，单测里把它们指到 tmp_path 即可，避免把真实 data/ 写脏。
"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from sortie_deck.auth import PublicUser
from sortie_deck.settings import Settings

# 包 __init__ 里的 app 是 FastAPI 实例，这里要的是模块本身
app_mod = import_module("sortie_deck.api.app")


class _StubUpload:
    def __init__(self, name: str, data: bytes, content_type: str = "text/markdown"):
        self.filename = name
        self._data = data
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._data


def _user(uid: str, role: str = "member") -> PublicUser:
    return PublicUser(id=uid, username=uid, display_name=uid, role=role)  # type: ignore[arg-type]


def _patch(monkeypatch: pytest.MonkeyPatch, tmp_path, *, owner: str = "usr_ada", mission_exists=False):
    cfg = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr(app_mod, "settings", cfg)

    class _Orch:
        def get(self, _mission_id):
            if mission_exists:
                return SimpleNamespace(
                    id=_mission_id, created_by=owner, shared_with=[], status="draft"
                )
            return None

    class _Store:
        def get(self, mission_id):
            return SimpleNamespace(id=mission_id, owner_id=owner)

    monkeypatch.setattr(app_mod, "orch", lambda: _Orch())
    monkeypatch.setattr(app_mod, "get_briefing", lambda: SimpleNamespace(store=_Store()))
    return cfg


def _upload(mission_id: str, name: str, data: bytes, user: PublicUser, **kw):
    return asyncio.run(
        app_mod.upload_mission_input(
            mission_id,
            file=_StubUpload(name, data),
            kind=kw.get("kind", "prd"),
            pinned=kw.get("pinned", True),
            _user=user,
        )
    )


def test_upload_list_pin_delete(monkeypatch, tmp_path):
    cfg = _patch(monkeypatch, tmp_path)
    ada = _user("usr_ada")

    saved = _upload("ini_in", "prd.md", "# PRD\n预发检查".encode(), ada)
    assert saved["extract_status"] == "ok"
    assert saved["stored_path"].startswith("files/")
    files_dir = cfg.data_dir / "missions" / "ini_in" / "input" / "files"
    assert any(files_dir.iterdir())

    listed = asyncio.run(app_mod.list_mission_input("ini_in", _user=ada))
    assert [m["name"] for m in listed["materials"]] == ["prd.md"]

    pinned = asyncio.run(
        app_mod.update_mission_input(
            "ini_in", saved["id"], app_mod.UpdateMissionInputBody(pinned=False), _user=ada
        )
    )
    assert pinned["pinned"] is False

    rekinded = asyncio.run(
        app_mod.update_mission_input(
            "ini_in", saved["id"], app_mod.UpdateMissionInputBody(kind="design"), _user=ada
        )
    )
    assert rekinded["kind"] == "design"

    removed = asyncio.run(app_mod.delete_mission_input("ini_in", saved["id"], _user=ada))
    assert removed["removed"]["id"] == saved["id"]
    assert asyncio.run(app_mod.list_mission_input("ini_in", _user=ada))["materials"] == []


def test_upload_rejects_other_user(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, owner="usr_ada")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            app_mod._mission_input_guard("ini_in", user=_user("usr_evil"))
        )
    assert exc.value.status_code == 403


def test_guard_allows_admin_and_owner(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, owner="usr_ada")
    # guard 是同步依赖（FastAPI 允许），直接调用即可
    assert app_mod._mission_input_guard("ini_in", user=_user("usr_ada")).id == "usr_ada"
    assert (
        app_mod._mission_input_guard("ini_in", user=_user("usr_admin", role="admin")).id
        == "usr_admin"
    )


def test_guard_404_for_unknown_mission(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)

    class _Missing:
        def get(self, _mission_id):
            raise KeyError(_mission_id)

    monkeypatch.setattr(app_mod, "get_briefing", lambda: SimpleNamespace(store=_Missing()))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app_mod._mission_input_guard("ini_missing", user=_user("usr_ada")))
    assert exc.value.status_code == 404


def test_upload_quota_returns_413(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    from sortie_deck import mission_input

    monkeypatch.setattr(mission_input, "MAX_FILE_BYTES", 4)
    with pytest.raises(HTTPException) as exc:
        _upload("ini_in", "big.txt", b"12345", _user("usr_ada"))
    assert exc.value.status_code == 413


def test_mission_exists_uses_control_guard(monkeypatch, tmp_path):
    """任务已落库时走运行控制权限：非创建者（普通成员）403。"""
    _patch(monkeypatch, tmp_path, owner="usr_ada", mission_exists=True)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(app_mod._mission_input_guard("ini_in", user=_user("usr_bob")))
    assert exc.value.status_code == 403
