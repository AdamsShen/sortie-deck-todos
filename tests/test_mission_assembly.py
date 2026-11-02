"""装配链路的服务层与 REST 端点契约：草稿可绑、启动后 409。"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from sortie_deck.auth import PublicUser
from sortie_deck.models import Initiative, InitiativeStatus
from sortie_deck.service import MissionService, Principal, ServiceError


def _principal(source: str = "cli") -> Principal:
    return Principal(
        user=PublicUser(
            id="usr_1", username="ada", display_name="Ada", role="member", status="active"
        ),
        source=source,
    )


def _orch(mission: Initiative) -> SimpleNamespace:
    """只实现 bind_repos 用到的 get / store.upsert / cfg。"""
    return SimpleNamespace(
        get=lambda _mission_id: mission,
        store=SimpleNamespace(upsert=lambda ini: ini),
        cfg=SimpleNamespace(data_dir="/tmp/sortie-assembly-test"),
    )


def test_bind_repos_writes_meta_on_draft():
    draft = Initiative(
        id="ini_x", title="t", brief="b", status=InitiativeStatus.DRAFT, created_by="usr_1"
    )
    svc = MissionService(_orch(draft))
    ini = svc.bind_repos(
        _principal(), "ini_x", {"eng": [{"url": "git@x/y.git", "branch": "dev", "name": "y"}]}
    )
    assert ini.meta["role_repos"]["eng"][0]["url"] == "git@x/y.git"
    assert ini.meta["role_repos"]["eng"][0]["branch"] == "dev"


def test_bind_repos_after_start_is_conflict():
    running = Initiative(
        id="ini_y", title="t", brief="b", status=InitiativeStatus.RUNNING, created_by="usr_1"
    )
    svc = MissionService(_orch(running))
    with pytest.raises(ServiceError) as excinfo:
        svc.bind_repos(_principal(), "ini_y", {"eng": [{"url": "git@x/y.git"}]})
    assert excinfo.value.code == "conflict"
    assert "启动前" in excinfo.value.message


def test_rest_repos_route_maps_conflict(monkeypatch):
    """路由要把 ServiceError 翻成 HTTP 状态码，而不是 500。"""
    app_mod = importlib.import_module("sortie_deck.api.app")

    def boom(self, principal, mission_id, role_repos):
        assert principal.source == "web"
        raise ServiceError("conflict", "任务已是 running，仓库需在启动前绑定")

    monkeypatch.setattr(MissionService, "bind_repos", boom)
    user = PublicUser(
        id="usr_1", username="ada", display_name="Ada", role="member", status="active"
    )
    req = app_mod.BindReposRequest(role_repos={})
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(app_mod.patch_initiative_repos("ini_x", req, user))
    assert excinfo.value.status_code == 409


def test_rest_repos_route_returns_payload(monkeypatch):
    app_mod = importlib.import_module("sortie_deck.api.app")
    draft = Initiative(
        id="ini_x", title="t", brief="b", status=InitiativeStatus.DRAFT, created_by="usr_1"
    )
    draft.meta = {"role_repos": {"eng": [{"url": "git@x/y.git", "branch": "", "name": ""}]}}

    monkeypatch.setattr(MissionService, "bind_repos", lambda self, p, m, r: draft)
    user = PublicUser(
        id="usr_1", username="ada", display_name="Ada", role="member", status="active"
    )
    payload = asyncio.run(
        app_mod.patch_initiative_repos("ini_x", app_mod.BindReposRequest(role_repos={}), user)
    )
    assert payload["id"] == "ini_x"
    assert payload["meta"]["role_repos"]["eng"][0]["url"] == "git@x/y.git"


def test_attach_materials_merges_by_name(tmp_path):
    """同名覆盖、其余保留：CLI 的追加语义依赖这个合并结果。"""
    from sortie_deck.artifacts import ArtifactStore

    mission = Initiative(
        id="ini_x", title="t", brief="b", status=InitiativeStatus.DRAFT, created_by="usr_1"
    )
    orch = _orch(mission)
    orch.artifacts = ArtifactStore(tmp_path / "artifacts")
    calls: list[list] = []

    def fake_set_materials(mission_id, rows):
        calls.append(rows)
        mission.materials = rows  # 真实实现会把投影写回 initiative
        return mission

    orch.set_materials = fake_set_materials
    svc = MissionService(orch)
    svc.attach_materials(_principal(), "ini_x", [{"name": "a.md", "content": "A"}])
    svc.attach_materials(_principal(), "ini_x", [{"name": "b.md", "content": "B"}])
    assert [m.name for m in calls[-1]] == ["a.md", "b.md"]
