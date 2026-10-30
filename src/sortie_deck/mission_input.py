"""任务输入快照：``missions/<id>/input/`` 的落盘、清单与文本抽取。

设计要点（与 ``docs/plans/2026-10-30-mission-input-and-per-mission-intake.md`` 对应）：

- ``manifest.json`` 是输入资料的**唯一索引**：持有元数据、相对路径、sha256 与抽取状态；
- 原件落 ``input/files/<material_id>__<safe_name>``，抽取文本落 ``input/text/<material_id>.md``；
- 所有写入走「文件锁 + tmp→os.replace」，避免多线程/多 worker 并发上传互相踩踏；
- 抽取依赖缺失或解析失败时降级为 ``failed``，**不阻断上传**——原件仍然可用；
- 该模块只负责「落盘与索引」，不决定是否进 prompt；注入策略见 ``materials.py``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from sortie_deck.layout import (
    mission_input_dir,
    mission_input_files_dir,
    mission_input_text_dir,
)
from sortie_deck.materials import MAX_EXCERPT_CHARS, compose_brief
from sortie_deck.models import ExternalMaterial, new_id, utc_now

try:  # POSIX 文件锁；Windows 退化为进程内锁（见 _locked）
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — 仅 Windows 走到
    _fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
BRIEF_NAME = "brief.md"
MANIFEST_VERSION = 1

# 单文件 50MB（与知识库导入一致）；单任务 500MB 防止磁盘被单任务吃满
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_MISSION_BYTES = 500 * 1024 * 1024
# 抽取文本落盘上限：超过则截断，避免扫描版 PDF 抽出几百 MB 文本
MAX_TEXT_CHARS = 2_000_000
# 回填 text_excerpt 的上限，与前端 toDraft 的切片保持一致
TEXT_EXCERPT_CHARS = 24_000

_TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".tsv",
    ".log", ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs", ".sql",
    ".sh", ".toml", ".ini", ".cfg", ".xml", ".html", ".htm", ".css",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".fig", ".sketch"}


class InputTooLarge(ValueError):
    """上传超限（单文件或单任务配额）。API 层转 400/413。"""


# --------------------------------------------------------------------------
# 原子写与文件锁
# --------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + fsync + os.replace，保证读侧永远不会看到半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            Path(tmp).unlink()
        raise


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            Path(tmp).unlink()
        raise


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def exclusive_lock(lock_path: Path) -> Iterator[None]:
    """跨线程 + 跨进程（POSIX flock）互斥。

    任务目录内的写操作（input manifest、planner 会话状态）共用同一套锁语义；
    多主机部署时 flock 不跨机器生效，此时依赖「每个任务只有一个写者」的
    业务假设；后续接对象存储版 input 时再换成分布式锁。
    """
    with _THREAD_LOCKS_GUARD:
        tlock = _THREAD_LOCKS.setdefault(str(lock_path), threading.Lock())
    with tlock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as fh:
            if _fcntl is not None:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                if _fcntl is not None:
                    _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# manifest 读写
# --------------------------------------------------------------------------


def manifest_path(data_dir: Path, initiative_id: str) -> Path:
    return mission_input_dir(data_dir, initiative_id) / MANIFEST_NAME


def _empty_manifest(initiative_id: str) -> dict[str, Any]:
    return {
        "version": MANIFEST_VERSION,
        "mission_id": initiative_id,
        "updated_at": utc_now().isoformat(),
        "files": [],
    }


def _read_manifest(path: Path, initiative_id: str) -> dict[str, Any]:
    if not path.exists():
        return _empty_manifest(initiative_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        # manifest 损坏不应让任务不可用：告警后按空清单继续，原件仍在 files/ 下
        logger.warning("input manifest 解析失败（按空清单继续）: %s: %s", path, exc)
        return _empty_manifest(initiative_id)
    if not isinstance(raw, dict):
        return _empty_manifest(initiative_id)
    raw.setdefault("version", MANIFEST_VERSION)
    raw.setdefault("mission_id", initiative_id)
    raw.setdefault("files", [])
    if not isinstance(raw["files"], list):
        raw["files"] = []
    return raw


def load_manifest(data_dir: Path, initiative_id: str) -> dict[str, Any]:
    """只读加载 manifest（不加锁；读侧依赖原子替换保证一致性）。"""
    return _read_manifest(manifest_path(data_dir, initiative_id), initiative_id)


def _update_manifest(
    data_dir: Path,
    initiative_id: str,
    mutate: Callable[[dict[str, Any]], Any],
) -> Any:
    """读-改-写 manifest，全程持锁；返回 ``mutate`` 的返回值。"""
    path = manifest_path(data_dir, initiative_id)
    with exclusive_lock(path.with_suffix(".lock")):
        manifest = _read_manifest(path, initiative_id)
        result = mutate(manifest)
        manifest["updated_at"] = utc_now().isoformat()
        _atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        return result


# --------------------------------------------------------------------------
# 文本抽取
# --------------------------------------------------------------------------


def _safe_name(name: str) -> str:
    """保留中英文/数字/常见符号，其余替换为下划线（文件系统安全 + 可读）。"""
    cleaned = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", name or "file").strip("._")
    return (cleaned or "file")[:120]


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    return text[:MAX_TEXT_CHARS], True


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))  # type: ignore[attr-defined]
    return "\n".join(p.text for p in document.paragraphs if p.text)


def _extract_xlsx(path: Path) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    lines: list[str] = []
    try:
        for sheet in wb.worksheets:
            lines.append(f"## {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                if any(cells):
                    lines.append("\t".join(cells))
    finally:
        wb.close()
    return "\n".join(lines)


_EXTRACTORS: dict[str, Callable[[Path], str]] = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
}
_MISSING_DEP_HINT = {
    ".pdf": "pip install pypdf",
    ".docx": "pip install python-docx",
    ".xlsx": "pip install openpyxl",
}


def extract_text(path: Path) -> tuple[str, str, str]:
    """按扩展名抽取文本，返回 ``(status, text, error)``。

    status: ``ok`` 抽到文本 / ``skipped`` 该类型无文本可抽（图片等）/
    ``failed`` 依赖缺失或解析失败。失败不影响原件可用性。
    """
    ext = path.suffix.lower()
    if ext in _TEXT_EXTS:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return "failed", "", f"读取失败: {exc}"
        text, truncated = _truncate(text)
        if truncated:
            logger.warning("抽取文本超过上限已截断: %s", path)
        return "ok", text, ""
    if ext in _IMAGE_EXTS:
        return "skipped", "", "图片/设计稿无文本层，仅保留原件"
    extractor = _EXTRACTORS.get(ext)
    if extractor is None:
        return "skipped", "", f"暂不支持抽取 {ext or '无扩展名'} 类型"
    try:
        text = extractor(path)
    except ImportError as exc:
        hint = _MISSING_DEP_HINT.get(ext, "")
        return "failed", "", f"缺少依赖（{hint}）: {exc}"
    except Exception as exc:  # noqa: BLE001 — 三方解析器异常类型不可预期
        logger.warning("文本抽取失败: %s: %s", path, exc)
        return "failed", "", f"解析失败: {exc}"
    text = (text or "").strip()
    if not text:
        return "failed", "", "未抽取到文本（可能是扫描件或空文档）"
    text, truncated = _truncate(text)
    if truncated:
        logger.warning("抽取文本超过上限已截断: %s", path)
    return "ok", text, ""


# --------------------------------------------------------------------------
# 写入 / 读取资料
# --------------------------------------------------------------------------


def _material_from_row(row: dict[str, Any]) -> ExternalMaterial:
    known = set(ExternalMaterial.model_fields)
    return ExternalMaterial.model_validate({k: v for k, v in row.items() if k in known})


def list_materials(data_dir: Path, initiative_id: str) -> list[ExternalMaterial]:
    rows = load_manifest(data_dir, initiative_id).get("files") or []
    out: list[ExternalMaterial] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append(_material_from_row(row))
        except Exception:  # noqa: BLE001 — 单条损坏不应拖垮整个任务
            logger.warning("manifest 行损坏（跳过）: %s: %r", initiative_id, row)
    return out


def _mission_bytes(manifest: dict[str, Any]) -> int:
    return sum(int(row.get("size") or 0) for row in manifest.get("files") or [] if isinstance(row, dict))


def save_upload(
    data_dir: Path,
    initiative_id: str,
    filename: str,
    raw: bytes,
    *,
    kind: str = "other",
    mime: str = "",
    pinned: bool = False,
    origin: str = "intake",
    material_id: str | None = None,
) -> ExternalMaterial:
    """保存一份上传原件并抽取文本，返回登记后的资料。

    - 单文件超 ``MAX_FILE_BYTES`` 或任务总量超 ``MAX_MISSION_BYTES`` 抛 ``InputTooLarge``；
    - 同任务内重复上传同内容同文件名（sha256 相同）直接复用已有条目，保证幂等；
    - 抽取失败只体现在 ``extract_status``，不抛异常。
    """
    if len(raw) > MAX_FILE_BYTES:
        raise InputTooLarge(f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 上限")
    sha = hashlib.sha256(raw).hexdigest()
    safe = _safe_name(filename)

    def _mutate(manifest: dict[str, Any]) -> ExternalMaterial:
        for row in manifest["files"]:
            if not isinstance(row, dict):
                continue
            if row.get("sha256") == sha and row.get("name") == filename:
                # 幂等：同内容重复上传不产生新条目（前端重试/刷新页面场景）
                return _material_from_row(row)
        if _mission_bytes(manifest) + len(raw) > MAX_MISSION_BYTES:
            raise InputTooLarge(
                f"任务输入总量超过 {MAX_MISSION_BYTES // (1024 * 1024)}MB 上限"
            )
        mat_id = material_id or new_id("mat_")
        stored_rel = f"files/{mat_id}__{safe}"
        text_rel = ""
        files_dir = mission_input_files_dir(data_dir, initiative_id)
        _atomic_write_bytes(files_dir / f"{mat_id}__{safe}", raw)
        status, text, error = extract_text(files_dir / f"{mat_id}__{safe}")
        if status == "ok":
            text_path = mission_input_text_dir(data_dir, initiative_id) / f"{mat_id}.md"
            _atomic_write_text(text_path, text)
            text_rel = f"text/{mat_id}.md"
        material = ExternalMaterial(
            id=mat_id,
            name=filename,
            kind=kind or "other",
            mime=mime,
            size=len(raw),
            pinned=pinned,
            text_excerpt=text[:TEXT_EXCERPT_CHARS] if status == "ok" else "",
            stored_path=stored_rel,
            text_path=text_rel,
            sha256=sha,
            extract_status=status,
            extract_error=error,
            origin=origin,
        )
        manifest["files"].append(material.model_dump(mode="json"))
        return material

    material = _update_manifest(data_dir, initiative_id, _mutate)
    logger.info(
        "input 落盘: mission=%s material=%s size=%s status=%s",
        initiative_id, material.id, material.size, material.extract_status,
    )
    return material


def upsert_materials(
    data_dir: Path,
    initiative_id: str,
    materials: Iterable[ExternalMaterial],
) -> list[ExternalMaterial]:
    """用客户端快照整体替换资料清单（保留已落盘原件与抽取结果）。

    规划会话每轮都会带一份 materials 快照；在这个阶段（上传接口尚未接入）
    原件可能还不存在，因此只登记元数据与摘录，等上传接口补齐 stored_path。
    """
    incoming = list(materials)

    def _mutate(manifest: dict[str, Any]) -> list[ExternalMaterial]:
        existing = {
            str(row.get("id")): row
            for row in manifest["files"]
            if isinstance(row, dict) and row.get("id")
        }
        merged: list[ExternalMaterial] = []
        for mat in incoming:
            row = dict(existing.get(mat.id) or {})
            payload = mat.model_dump(mode="json")
            # 已落盘字段优先保留旧值：客户端不知道 stored_path/sha/抽取状态
            for keep in ("stored_path", "text_path", "sha256", "extract_status", "extract_error"):
                if row.get(keep):
                    payload[keep] = row[keep]
            merged.append(ExternalMaterial.model_validate(payload))
        manifest["files"] = [m.model_dump(mode="json") for m in merged]
        return merged

    return _update_manifest(data_dir, initiative_id, _mutate)


def update_material(
    data_dir: Path,
    initiative_id: str,
    material_id: str,
    *,
    pinned: bool | None = None,
    kind: str | None = None,
) -> ExternalMaterial | None:
    """更新资料的 pin / kind（不改原件与抽取结果）。"""

    def _mutate(manifest: dict[str, Any]) -> ExternalMaterial | None:
        for row in manifest["files"]:
            if not isinstance(row, dict) or row.get("id") != material_id:
                continue
            if pinned is not None:
                row["pinned"] = bool(pinned)
            cleaned_kind = (kind or "").strip()
            if cleaned_kind:
                row["kind"] = cleaned_kind
            return _material_from_row(row)
        return None

    return _update_manifest(data_dir, initiative_id, _mutate)


def set_pinned(
    data_dir: Path, initiative_id: str, material_id: str, pinned: bool
) -> ExternalMaterial | None:
    """只改置顶状态（``update_material`` 的窄接口，保持既有调用点不变）。"""
    return update_material(data_dir, initiative_id, material_id, pinned=pinned)


def remove_material(
    data_dir: Path, initiative_id: str, material_id: str
) -> ExternalMaterial | None:
    """删除资料：先从 manifest 摘除，再尽力删原件/抽取文本（失败只告警）。"""

    def _mutate(manifest: dict[str, Any]) -> ExternalMaterial | None:
        removed: ExternalMaterial | None = None
        keep: list[dict[str, Any]] = []
        for row in manifest["files"]:
            if isinstance(row, dict) and row.get("id") == material_id:
                removed = _material_from_row(row)
                continue
            keep.append(row)
        manifest["files"] = keep
        return removed

    removed = _update_manifest(data_dir, initiative_id, _mutate)
    if removed is not None:
        for rel in (removed.stored_path, removed.text_path):
            if not rel:
                continue
            target = mission_input_dir(data_dir, initiative_id) / rel
            with suppress(OSError):
                target.unlink()
    return removed


def material_text(data_dir: Path, initiative_id: str, material: ExternalMaterial) -> str:
    """读取抽取文本（不存在时回退 ``text_excerpt``）。"""
    if material.text_path:
        path = mission_input_dir(data_dir, initiative_id) / material.text_path
        if path.exists():
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("读取抽取文本失败: %s: %s", path, exc)
    return material.text_excerpt or ""


def rebuild_brief(
    data_dir: Path,
    initiative_id: str,
    brief_base: str,
    *,
    materials: list[ExternalMaterial] | None = None,
) -> Path:
    """按当前资料重建 ``input/brief.md``（brief 的唯一事实源）。

    只把 pinned 且可读的文本并入，格式与 ``materials.compose_brief`` 完全一致，
    避免出现「两个地方各有一份 brief 且内容不同」的历史问题。
    """
    mats = materials if materials is not None else list_materials(data_dir, initiative_id)
    hydrated: list[ExternalMaterial] = []
    for mat in mats:
        text = material_text(data_dir, initiative_id, mat)
        hydrated.append(mat.model_copy(update={"text_excerpt": text[:MAX_EXCERPT_CHARS]}))
    content = compose_brief(brief_base, hydrated)
    path = mission_input_dir(data_dir, initiative_id) / BRIEF_NAME
    _atomic_write_text(path, content + "\n")
    return path


def list_input_rows(data_dir: Path, initiative_id: str) -> list[dict[str, Any]]:
    """manifest 原始行（StageContext / prompt 索引用，不做模型校验，开销最小）。"""
    return [
        row
        for row in load_manifest(data_dir, initiative_id).get("files") or []
        if isinstance(row, dict)
    ]


def render_input_index(rows: Iterable[dict[str, Any]]) -> str:
    """按 manifest 行渲染「输入索引」：只给路径与摘要，正文让 agent 按需读取。

    这是「动态加载」的核心：落盘保证资料完整，索引保证上下文不被撑爆。
    """
    lines: list[str] = []
    for row in rows:
        stored = str(row.get("stored_path") or "")
        if not stored:
            continue
        kb = max(1, round(int(row.get("size") or 0) / 1024))
        flags = [str(row.get("kind") or "other"), f"{kb}KB"]
        if row.get("pinned"):
            flags.append("pinned")
        flags.append(f"text:{row.get('extract_status') or 'pending'}")
        detail = f"- `input/{stored}` · " + " · ".join(flags)
        text_path = str(row.get("text_path") or "")
        if text_path:
            detail += f" · 抽取文本 `input/{text_path}`"
        lines.append(detail)
    if not lines:
        return ""
    return (
        "## Input index\n"
        "任务输入已快照在 `input/` 下。摘要不足以支撑实现时，按需读取下列文件，"
        "不要把假设当事实。\n\n" + "\n".join(lines) + "\n"
    )


def input_index_block(materials: Iterable[ExternalMaterial]) -> str:
    """``render_input_index`` 的模型入参版本（供已有调用点复用）。"""
    return render_input_index(m.model_dump(mode="json") for m in materials)


__all__ = [
    "BRIEF_NAME",
    "MANIFEST_NAME",
    "MAX_FILE_BYTES",
    "MAX_MISSION_BYTES",
    "InputTooLarge",
    "exclusive_lock",
    "extract_text",
    "input_index_block",
    "list_input_rows",
    "list_materials",
    "load_manifest",
    "render_input_index",
    "manifest_path",
    "material_text",
    "rebuild_brief",
    "remove_material",
    "save_upload",
    "set_pinned",
    "update_material",
    "upsert_materials",
]
