from __future__ import annotations

import pytest
from fastapi import HTTPException

from sortie_deck.artifacts import ArtifactPathError, ArtifactStore


def test_path_traversal_rejected(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    store.write_text("ini_x", "product", "brief.md", "hello")
    with pytest.raises(ArtifactPathError):
        store.read_text("../secrets.txt")
    with pytest.raises(ArtifactPathError):
        store.read_text("ini_x/../../etc/passwd")
    with pytest.raises(ArtifactPathError):
        store.resolve_safe("/etc/passwd")
    assert store.read_text("ini_x/product/brief.md") == "hello"


def test_artifact_path_spellings_resolve(tmp_path):
    """完整存储路径 / `stage/file` / 纯文件名三种写法都要能读到同一份产物。

    列表接口返回的是完整 key，而用户和模型常写后两种；三层入口共用
    ``artifact_candidates``，行为必须一致。
    """
    from types import SimpleNamespace

    from sortie_deck.auth import PublicUser
    from sortie_deck.models import Initiative, InitiativeStatus
    from sortie_deck.service import MissionService, Principal, artifact_candidates

    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.write_text("ini_x", "qa_verify", "test_cases.md", "hello")

    ini = Initiative(
        id="ini_x",
        title="t",
        brief="b",
        status=InitiativeStatus.RUNNING,
        created_by="usr_1",
    )
    ini.artifacts = [ref]
    orch = SimpleNamespace(artifacts=store, get=lambda _id: ini)
    svc = MissionService(orch)  # type: ignore[arg-type]
    principal = Principal(
        user=PublicUser(
            id="usr_1", username="ada", display_name="Ada", role="member", status="active"
        ),
        source="cli",
    )

    assert ref.path in artifact_candidates("ini_x", "qa_verify/test_cases.md")
    assert svc.read_artifact(principal, "ini_x", ref.path) == "hello"
    assert svc.read_artifact(principal, "ini_x", "qa_verify/test_cases.md") == "hello"
    assert svc.read_artifact(principal, "ini_x", "test_cases.md") == "hello"

    with pytest.raises(Exception) as excinfo:
        svc.read_artifact(principal, "ini_x", "nope.md")
    assert getattr(excinfo.value, "code", "") == "not_found"


def test_sync_artifacts_dedups_same_path():
    """并行合并后裸 key 与作用域 key 指向同一路径时，只登记一次。

    回归背景：并行分支合并的 upstream 里同时存在
    ``tech_design.md``（last-wins 指向最后一个分支）和
    ``eng_backend_tech_design::tech_design.md``（同一路径），旧逻辑只在函数
    入口拍一次 path 快照，导致面板里出现两份完全相同的产物。
    """
    from sortie_deck.models import Initiative, InitiativeStatus
    from sortie_deck.orchestrator import Orchestrator

    ini = Initiative(id="ini_x", title="t", brief="b", status=InitiativeStatus.RUNNING)
    backend = "ini_x/artifacts/eng_backend_tech_design/tech_design.md"
    web = "ini_x/artifacts/eng_web_tech_design/tech_design.md"

    Orchestrator._sync_artifacts_from_upstream(
        None,  # type: ignore[arg-type] — 该方法不依赖实例状态
        ini,
        {
            "tech_design.md": backend,
            "eng_backend_tech_design::tech_design.md": backend,
            "eng_web_tech_design::tech_design.md": web,
        },
        "eng_web",
    )

    assert [a.path for a in ini.artifacts] == [backend, web]
    assert [a.stage for a in ini.artifacts] == [
        "eng_backend_tech_design",
        "eng_web_tech_design",
    ]


def test_sync_artifacts_heals_existing_duplicates():
    """存量索引里的同路径重复项在下次同步时被顺手清掉。"""
    from sortie_deck.models import ArtifactRef, Initiative, InitiativeStatus
    from sortie_deck.orchestrator import Orchestrator

    path = "ini_x/artifacts/eng_backend_tech_design/tech_design.md"
    ini = Initiative(id="ini_x", title="t", brief="b", status=InitiativeStatus.RUNNING)
    ini.artifacts = [
        ArtifactRef(kind="tech_design.md", path=path, stage="eng_backend_tech_design"),
        ArtifactRef(kind="tech_design.md", path=path, stage="eng_backend_tech_design"),
    ]

    Orchestrator._sync_artifacts_from_upstream(None, ini, {"tech_design.md": path}, "eng_web")  # type: ignore[arg-type]

    assert [a.path for a in ini.artifacts] == [path]


def test_download_requires_auth():
    from sortie_deck.api.app import require_user

    with pytest.raises(HTTPException) as ei:
        require_user(None)
    assert ei.value.status_code == 401

    with pytest.raises(HTTPException) as ei2:
        require_user("Bearer not-a-valid-token")
    assert ei2.value.status_code == 401
