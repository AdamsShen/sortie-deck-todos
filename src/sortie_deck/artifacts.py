from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sortie_deck.models import ArtifactRef, utc_now


class ArtifactPathError(ValueError):
    """Raised when a relative path escapes the artifact root."""


@runtime_checkable
class ArtifactBackend(Protocol):
    def write_text(
        self,
        initiative_id: str,
        stage: str,
        filename: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> ArtifactRef: ...

    def write_json(
        self,
        initiative_id: str,
        stage: str,
        filename: str,
        data: Any,
        meta: dict[str, Any] | None = None,
    ) -> ArtifactRef: ...

    def read_text(self, relative_path: str) -> str: ...

    def read_json(self, relative_path: str) -> Any: ...

    def clear(self, initiative_id: str) -> None: ...


class LocalArtifactStore:
    """Filesystem artifact store.

    layout="flat"（历史默认）：``<root>/<initiative_id>/<stage>/<file>``；
    layout="mission"（任务目录收敛）：``<root>/<initiative_id>/artifacts/<stage>/<file>``，
    root 传 missions 根（data/missions），产物归入任务目录（见 layout.py）。
    """

    def __init__(self, root: Path, *, layout: str = "flat") -> None:
        self.root = root.resolve()
        self.layout = "mission" if layout == "mission" else "flat"
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve_safe(self, relative_path: str) -> Path:
        if not relative_path or relative_path.startswith(("/", "\\")):
            raise ArtifactPathError("absolute paths are not allowed")
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactPathError("path escapes artifact root") from exc
        return candidate

    def initiative_dir(self, initiative_id: str) -> Path:
        path = self.root / initiative_id
        if self.layout == "mission":
            path = path / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_text(
        self,
        initiative_id: str,
        stage: str,
        filename: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        stage_dir = self.initiative_dir(initiative_id) / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        path = stage_dir / filename
        safe = self.resolve_safe(str(path.relative_to(self.root)))
        safe.write_text(content, encoding="utf-8")
        return ArtifactRef(
            kind=filename,
            path=str(safe.relative_to(self.root)),
            stage=stage,
            created_at=utc_now(),
            meta=meta or {},
        )

    def write_json(
        self,
        initiative_id: str,
        stage: str,
        filename: str,
        data: Any,
        meta: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        return self.write_text(
            initiative_id,
            stage,
            filename,
            json.dumps(data, ensure_ascii=False, indent=2),
            meta=meta,
        )

    def read_text(self, relative_path: str) -> str:
        return self.resolve_safe(relative_path).read_text(encoding="utf-8")

    def read_json(self, relative_path: str) -> Any:
        return json.loads(self.read_text(relative_path))

    def absolute(self, relative_path: str) -> Path:
        return self.resolve_safe(relative_path)

    def list_files(self, initiative_id: str) -> list[Path]:
        base = self.initiative_dir(initiative_id)
        return sorted(p for p in base.rglob("*") if p.is_file())

    def clear(self, initiative_id: str) -> None:
        import shutil

        path = self.root / initiative_id
        if path.exists():
            shutil.rmtree(path)


# Backward-compatible alias
ArtifactStore = LocalArtifactStore
