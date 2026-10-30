from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from sortie_deck import mission_input as mi
from sortie_deck.models import ExternalMaterial


def _input_dir(data_dir: Path, mission_id: str) -> Path:
    return data_dir / "missions" / mission_id / "input"


def test_save_upload_writes_original_and_text(tmp_path: Path):
    data = tmp_path / "data"
    mat = mi.save_upload(
        data,
        "ini_t1",
        "prd.md",
        "# PRD\n\niOS 与 Web 预发健康检查".encode(),
        kind="prd",
        pinned=True,
    )

    assert mat.extract_status == "ok"
    assert mat.sha256 and len(mat.sha256) == 64
    base = _input_dir(data, "ini_t1")
    assert (base / mat.stored_path).read_text(encoding="utf-8").startswith("# PRD")
    assert (base / mat.text_path).read_text(encoding="utf-8").startswith("# PRD")

    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["mission_id"] == "ini_t1"
    assert [row["id"] for row in manifest["files"]] == [mat.id]
    # text_excerpt 回填，保证老路径（brief 组合）仍能工作
    assert "预发健康检查" in manifest["files"][0]["text_excerpt"]


def test_save_upload_is_idempotent_for_same_content(tmp_path: Path):
    data = tmp_path / "data"
    first = mi.save_upload(data, "ini_t1", "prd.md", b"same-content", kind="prd")
    second = mi.save_upload(data, "ini_t1", "prd.md", b"same-content", kind="prd")

    assert first.id == second.id
    assert len(mi.list_materials(data, "ini_t1")) == 1
    # 文件也只落一份
    files = list((_input_dir(data, "ini_t1") / "files").iterdir())
    assert len(files) == 1


def test_binary_upload_is_kept_but_not_extracted(tmp_path: Path):
    data = tmp_path / "data"
    mat = mi.save_upload(data, "ini_t1", "wireframe.png", b"\x89PNG\r\n\x1a\nfake", kind="design")

    assert mat.extract_status == "skipped"
    assert mat.text_path == ""
    assert mat.extract_error
    assert (_input_dir(data, "ini_t1") / mat.stored_path).exists()


def test_broken_pdf_degrades_to_failed(tmp_path: Path):
    data = tmp_path / "data"
    mat = mi.save_upload(data, "ini_t1", "broken.pdf", b"definitely not a pdf", kind="prd")

    assert mat.extract_status == "failed"
    assert mat.text_path == ""
    # 原件仍然保留，人工可以下载查看
    assert (_input_dir(data, "ini_t1") / mat.stored_path).exists()


def test_single_file_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mi, "MAX_FILE_BYTES", 4)
    with pytest.raises(mi.InputTooLarge):
        mi.save_upload(tmp_path / "data", "ini_t1", "big.txt", b"12345")


def test_mission_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"
    monkeypatch.setattr(mi, "MAX_MISSION_BYTES", 6)
    mi.save_upload(data, "ini_t1", "a.txt", b"1234")
    with pytest.raises(mi.InputTooLarge):
        mi.save_upload(data, "ini_t1", "b.txt", b"5678")


def test_concurrent_uploads_keep_every_entry(tmp_path: Path):
    data = tmp_path / "data"
    size = 20
    errors: list[BaseException] = []

    def _worker(idx: int) -> None:
        try:
            mi.save_upload(data, "ini_race", f"f{idx}.txt", f"content-{idx}".encode())
        except BaseException as exc:  # noqa: BLE001 — 线程内异常要带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(size)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(mi.list_materials(data, "ini_race")) == size
    manifest = json.loads(
        (_input_dir(data, "ini_race") / "manifest.json").read_text(encoding="utf-8")
    )
    assert len(manifest["files"]) == size


def test_rebuild_brief_only_inlines_pinned(tmp_path: Path):
    data = tmp_path / "data"
    mi.save_upload(data, "ini_t1", "prd.md", b"PINNED-TEXT", kind="prd", pinned=True)
    mi.save_upload(data, "ini_t1", "notes.md", b"ARCHIVED-TEXT", kind="other", pinned=False)

    path = mi.rebuild_brief(data, "ini_t1", "原始需求")

    content = path.read_text(encoding="utf-8")
    assert "原始需求" in content
    assert "PINNED-TEXT" in content
    assert "ARCHIVED-TEXT" not in content
    assert path == _input_dir(data, "ini_t1") / "brief.md"


def test_input_index_lists_paths_only(tmp_path: Path):
    data = tmp_path / "data"
    saved = mi.save_upload(data, "ini_t1", "prd.md", b"hello", kind="prd", pinned=True)
    legacy = ExternalMaterial(name="legacy.md", kind="prd", text_excerpt="only excerpt")

    block = mi.input_index_block([saved, legacy])

    assert "## Input index" in block
    assert f"input/{saved.stored_path}" in block
    assert f"input/{saved.text_path}" in block
    assert "legacy.md" not in block
    assert mi.input_index_block([legacy]) == ""


def test_upsert_materials_keeps_stored_fields(tmp_path: Path):
    data = tmp_path / "data"
    saved = mi.save_upload(data, "ini_t1", "prd.md", b"hello", kind="prd", pinned=True)

    # 客户端只知道自己那份快照（没有 stored_path），pin 状态变化要保留原件信息
    client_view = ExternalMaterial(id=saved.id, name="prd.md", kind="prd", pinned=False)
    merged = mi.upsert_materials(data, "ini_t1", [client_view])

    assert len(merged) == 1
    assert merged[0].pinned is False
    assert merged[0].stored_path == saved.stored_path
    assert merged[0].sha256 == saved.sha256


def test_remove_material_deletes_files(tmp_path: Path):
    data = tmp_path / "data"
    mat = mi.save_upload(data, "ini_t1", "prd.md", b"hello", kind="prd")

    removed = mi.remove_material(data, "ini_t1", mat.id)

    assert removed is not None and removed.id == mat.id
    assert mi.list_materials(data, "ini_t1") == []
    assert not (_input_dir(data, "ini_t1") / mat.stored_path).exists()
    assert not (_input_dir(data, "ini_t1") / mat.text_path).exists()


def test_corrupted_manifest_falls_back_to_empty(tmp_path: Path):
    data = tmp_path / "data"
    base = _input_dir(data, "ini_t1")
    base.mkdir(parents=True)
    (base / "manifest.json").write_text("{not json", encoding="utf-8")

    assert mi.list_materials(data, "ini_t1") == []
    # 仍可继续写入，不会被坏文件卡死
    mat = mi.save_upload(data, "ini_t1", "after.md", b"ok")
    assert mat.extract_status == "ok"
