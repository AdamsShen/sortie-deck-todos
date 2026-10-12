from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from sortie_deck.models import new_id, utc_now

MemoryScope = Literal["workspace", "user", "mission", "agent"]
MemoryKind = Literal["fact", "preference", "episode", "lesson"]

# Hard cap for injected memory briefs (prompt safety).
CONTEXT_BLOCK_MAX_CHARS = 6000


def stage_tag(entry: MemoryEntry) -> str | None:
    """The ``stage:<name>`` tag a memory is scoped to, if any."""
    for tag in entry.tags:
        if tag.startswith("stage:"):
            return tag.split(":", 1)[1].strip()
    return None


def stage_applies(entry: MemoryEntry, stage: str | None) -> bool:
    """Stage-scoped memories only reach the stage that owns them.

    Untagged memories apply everywhere; tagged ones are hidden from sibling
    parallel stages so a sub-room conclusion cannot leak across branches.
    """
    tagged = stage_tag(entry)
    if tagged is None:
        return True
    return bool(stage) and tagged == stage


class MemoryEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem_"))
    scope: MemoryScope = "workspace"
    scope_id: str = "global"
    kind: MemoryKind = "fact"
    content: str
    tags: list[str] = Field(default_factory=list)
    source: str = "manual"
    author_id: str = ""
    author_name: str = ""
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


class CreateMemoryRequest(BaseModel):
    scope: MemoryScope = "workspace"
    scope_id: str = "global"
    kind: MemoryKind = "fact"
    content: str
    tags: list[str] = Field(default_factory=list)
    source: str = "manual"


class UpdateMemoryRequest(BaseModel):
    content: str | None = None
    tags: list[str] | None = None
    kind: MemoryKind | None = None


_SEED: list[dict] = [
    {
        "scope": "workspace",
        "scope_id": "global",
        "kind": "lesson",
        "tags": ["process"],
        "content": "默认先对齐再出击：讨论未收敛时不要 /start。",
    },
    {
        "scope": "workspace",
        "scope_id": "global",
        "kind": "preference",
        "tags": ["style"],
        "content": "产物优先短、可验收；避免空话 PRD。",
    },
]


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            entries = [
                MemoryEntry(
                    scope=s["scope"],  # type: ignore[arg-type]
                    scope_id=s["scope_id"],
                    kind=s["kind"],  # type: ignore[arg-type]
                    content=s["content"],
                    tags=s["tags"],
                    author_name="Sortie",
                    source="seed",
                )
                for s in _SEED
            ]
            self._write(entries)

    def _read(self) -> list[MemoryEntry]:
        return [MemoryEntry.model_validate(x) for x in json.loads(self.path.read_text(encoding="utf-8"))]

    def _write(self, entries: list[MemoryEntry]) -> None:
        self.path.write_text(
            json.dumps([e.model_dump(mode="json") for e in entries], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(
        self,
        *,
        scope: str | None = None,
        scope_id: str | None = None,
        q: str | None = None,
        user_id: str | None = None,
    ) -> list[MemoryEntry]:
        with self._lock:
            entries = self._read()
        out: list[MemoryEntry] = []
        for e in entries:
            if e.scope == "user" and user_id and e.scope_id != user_id and e.author_id != user_id:
                # private user memories only visible to owner
                continue
            if scope and e.scope != scope:
                continue
            if scope_id and e.scope_id != scope_id:
                continue
            if q:
                needle = q.lower()
                if (
                    needle not in e.content.lower()
                    and not any(needle in t.lower() for t in e.tags)
                ):
                    continue
            out.append(e)
        return sorted(out, key=lambda x: x.updated_at, reverse=True)

    def get(self, mem_id: str) -> MemoryEntry:
        for e in self.list():
            if e.id == mem_id:
                return e
        raise KeyError(mem_id)

    def create(
        self, req: CreateMemoryRequest, *, author_id: str, author_name: str
    ) -> MemoryEntry:
        scope_id = req.scope_id
        if req.scope == "user" and (not scope_id or scope_id == "global"):
            scope_id = author_id
        entry = MemoryEntry(
            scope=req.scope,
            scope_id=scope_id,
            kind=req.kind,
            content=req.content.strip(),
            tags=[t.strip() for t in req.tags if t.strip()],
            source=req.source,
            author_id=author_id,
            author_name=author_name,
        )
        with self._lock:
            entries = self._read()
            entries.append(entry)
            self._write(entries)
        return entry

    def update(self, mem_id: str, req: UpdateMemoryRequest) -> MemoryEntry:
        with self._lock:
            entries = self._read()
            for i, e in enumerate(entries):
                if e.id != mem_id:
                    continue
                data = e.model_dump()
                patch = req.model_dump(exclude_unset=True)
                if "content" in patch and patch["content"] is not None:
                    patch["content"] = patch["content"].strip()
                if "tags" in patch and patch["tags"] is not None:
                    patch["tags"] = [t.strip() for t in patch["tags"] if t.strip()]
                data.update(patch)
                data["updated_at"] = utc_now().isoformat()
                entries[i] = MemoryEntry.model_validate(data)
                self._write(entries)
                return entries[i]
        raise KeyError(mem_id)

    def delete(self, mem_id: str) -> None:
        with self._lock:
            entries = self._read()
            next_entries = [e for e in entries if e.id != mem_id]
            if len(next_entries) == len(entries):
                raise KeyError(mem_id)
            self._write(next_entries)

    def list_for_stage(
        self,
        *,
        mission_id: str,
        stage: str,
        limit: int = 12,
    ) -> list[MemoryEntry]:
        """Mission memories that may be injected into ``stage``.

        Used for sub-room conclusions: they are stored with a ``stage:<name>``
        tag so only the requesting step sees them.
        """
        out = [
            e
            for e in self.list(scope="mission", scope_id=mission_id)
            if stage_applies(e, stage)
        ]
        return out[:limit]

    def context_block(
        self,
        *,
        user_id: str | None = None,
        mission_id: str | None = None,
        agent_ids: list[str] | None = None,
        stage: str | None = None,
        limit: int = 12,
    ) -> str:
        """Compact memory brief for agent prompts / chat injection."""
        chunks: list[str] = []
        for e in self.list(scope="workspace", scope_id="global"):
            chunks.append(f"- [{e.kind}] {e.content}")
        if user_id:
            for e in self.list(scope="user", scope_id=user_id, user_id=user_id):
                chunks.append(f"- [you/{e.kind}] {e.content}")
        if mission_id:
            for e in self.list(scope="mission", scope_id=mission_id):
                if not stage_applies(e, stage):
                    continue
                chunks.append(f"- [mission/{e.kind}] {e.content}")
        for aid in agent_ids or []:
            for e in self.list(scope="agent", scope_id=aid):
                chunks.append(f"- [agent/{e.kind}] {e.content}")
        if not chunks:
            return ""
        body = "Memory brief:\n" + "\n".join(chunks[:limit])
        if len(body) <= CONTEXT_BLOCK_MAX_CHARS:
            return body
        trimmed = body[: CONTEXT_BLOCK_MAX_CHARS - 3].rstrip()
        return trimmed + "..."
