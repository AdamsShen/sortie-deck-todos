"""产物读取 API 的二进制下载：code-archive zip 走 FileResponse 附件而非 JSON 预览。

直接调用路由函数（不起 TestClient），monkeypatch settings 与 orch 单例到
tmp_path，风格同 test_input_api.py。
"""

from __future__ import annotations

import asyncio
import zipfile
from importlib import import_module
from types import SimpleNamespace

import pytest

from sortie_deck.artifacts import LocalArtifactStore
from sortie_deck.auth import PublicUser
from sortie_deck.settings import Settings

app_mod = import_module("sortie_deck.api.app")


def _user(uid: str, role: str = "member") -> PublicUser:
    return PublicUser(id=uid, username=uid, display_name=uid, role=role)  # type: ignore[arg-type]


def _patch(monkeypatch: pytest.MonkeyPatch, tmp_path, ini: str):
    cfg = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr(app_mod, "settings", cfg)

    store = LocalArtifactStore(cfg.missions_dir, layout="mission")

    class _Orch:
        artifacts = store

        def get(self, _mission_id):
            return SimpleNamespace(
                id=ini, created_by="usr_ada", shared_with=[], status="done", artifacts=[]
            )

    monkeypatch.setattr(app_mod, "orch", lambda: _Orch())
    return cfg


def test_read_artifact_zip_returns_attachment(monkeypatch, tmp_path):
    """zip 产物（read_text 会 UnicodeDecodeError）转附件下载，浏览器直接另存。"""
    ini = "ini_dl"
    cfg = _patch(monkeypatch, tmp_path, ini)
    zip_rel = f"{ini}/artifacts/code-archive/code-archive-x.zip"
    zip_abs = cfg.missions_dir / zip_rel
    zip_abs.parent.mkdir(parents=True)
    with zipfile.ZipFile(zip_abs, "w") as zf:
        zf.writestr("repos/app.py", "x = 1\n")

    resp = asyncio.run(
        app_mod.read_artifact(ini, "code-archive/code-archive-x.zip", _user=_user("usr_ada"))
    )

    assert resp.media_type == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]
    assert "code-archive-x.zip" in resp.headers["content-disposition"]
    assert str(zip_abs) == str(resp.path)


def test_read_artifact_text_still_json(monkeypatch, tmp_path):
    """文本产物不受影响：仍走 JSON content 预览。"""
    ini = "ini_dl2"
    cfg = _patch(monkeypatch, tmp_path, ini)
    md_abs = cfg.missions_dir / ini / "artifacts" / "code-archive" / "manifest.json"
    md_abs.parent.mkdir(parents=True)
    md_abs.write_text('{"file_count": 2}', encoding="utf-8")

    data = asyncio.run(
        app_mod.read_artifact(ini, "code-archive/manifest.json", _user=_user("usr_ada"))
    )

    assert data["path"].endswith("manifest.json")
    assert "file_count" in data["content"]
