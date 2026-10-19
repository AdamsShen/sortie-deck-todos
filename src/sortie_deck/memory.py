from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Literal

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
    # 一句话简介（写入时 LLM 生成；失败降级为 content 截断）。注入提示词时
    # 只带简介行，Agent 需要时按 id 取全文（Claude Code MEMORY.md 模式）。
    summary: str = ""
    # 简介生成状态：pending=LLM 生成中 / done=已生成 / failed=生成失败（展示截断）。
    # 旧数据为空串，语义等同 done 的截断兜底。
    summary_status: Literal["", "pending", "done", "failed"] = ""
    tags: list[str] = Field(default_factory=list)
    source: str = "manual"
    author_id: str = ""
    author_name: str = ""
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    def display_summary(self) -> str:
        """简介行：summary 为空时降级为 content 截断（旧数据兼容）。"""
        return (self.summary or self.content or "").strip()[:40]


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


def _terms(text: str) -> set[str]:
    """检索分词：拉丁词 + CJK 双字组（bigram），小写化。"""
    import re

    lowered = (text or "").lower()
    terms: set[str] = set(re.findall(r"[a-z0-9]{2,}", lowered))
    cjk = re.findall(r"[\u4e00-\u9fff]", lowered)
    terms.update(f"{a}{b}" for a, b in zip(cjk, cjk[1:]))
    return terms


def _score_entry(entry: MemoryEntry, terms: set[str]) -> int:
    """关键词重合度打分：content + summary + tags 命中越多分越高。"""
    if not terms:
        return 0
    hay = _terms(f"{entry.content} {entry.summary} {' '.join(entry.tags)}")
    return len(terms & hay)


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

    @property
    def index_path(self) -> Path:
        """记忆索引文件（派生物，从 entries 重建，不作为数据源）。"""
        return self.path.parent / "memory" / "MEMORY_INDEX.md"

    def rebuild_index(self) -> None:
        """重建记忆索引文件：每条一行「id | 作用域/类型 | 简介」。"""
        with self._lock:
            self._write_index(self._read())

    def _write_index(self, entries: list[MemoryEntry]) -> None:
        scope_label = {
            "workspace": "全局约定",
            "user": "我的",
            "mission": "任务",
            "agent": "Agent",
        }
        lines = ["# Memory Index", ""]
        for e in entries:
            label = scope_label.get(e.scope, e.scope)
            target = "" if e.scope == "workspace" else f" · {e.scope_id[:10]}"
            lines.append(f"- {e.id} | {label}{target} / {e.kind} | {e.display_summary()}")
        try:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            self.index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass  # 索引是派生物，写失败不影响数据

    def set_summary(self, mem_id: str, summary: str, *, status: str = "done") -> MemoryEntry:
        """写入 LLM 生成的简介并刷新索引（status 标记本轮生成结果）。"""
        with self._lock:
            entries = self._read()
            for i, e in enumerate(entries):
                if e.id != mem_id:
                    continue
                data = e.model_dump()
                data["summary"] = summary.strip()[:60]
                data["summary_status"] = status
                data["updated_at"] = utc_now().isoformat()
                entries[i] = MemoryEntry.model_validate(data)
                self._write(entries)
                return entries[i]
        raise KeyError(mem_id)

    def set_summary_status(self, mem_id: str, status: str) -> MemoryEntry:
        """仅流转简介状态（pending/failed），不动 summary 内容。"""
        with self._lock:
            entries = self._read()
            for i, e in enumerate(entries):
                if e.id != mem_id:
                    continue
                data = e.model_dump()
                data["summary_status"] = status
                data["updated_at"] = utc_now().isoformat()
                entries[i] = MemoryEntry.model_validate(data)
                self._write(entries)
                return entries[i]
        raise KeyError(mem_id)

    def _read(self) -> list[MemoryEntry]:
        return [MemoryEntry.model_validate(x) for x in json.loads(self.path.read_text(encoding="utf-8"))]

    def _write(self, entries: list[MemoryEntry]) -> None:
        self.path.write_text(
            json.dumps([e.model_dump(mode="json") for e in entries], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 索引是派生物，随每次数据变更重建（调用方已持锁，禁止再走 list()/加锁）
        self._write_index(entries)

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

    def context_candidates(
        self,
        *,
        user_id: str | None = None,
        mission_id: str | None = None,
        agent_ids: list[str] | None = None,
        stage: str | None = None,
        query: str | None = None,
        limit: int = 8,
    ) -> list[MemoryEntry]:
        """任务注入候选集：四层作用域合并 + query 相关性打分排序。

        Plan-time 绑定（meta.memory_ids）与 stage 注入共用此入口；返回
        MemoryEntry 列表，注入文本由 render_block 渲染为简介行。
        """
        terms = _terms(query or "")
        buckets: list[list[MemoryEntry]] = []
        buckets.append(self.list(scope="workspace", scope_id="global"))
        if user_id:
            buckets.append(self.list(scope="user", scope_id=user_id, user_id=user_id))
        if mission_id:
            buckets.append(
                [
                    e
                    for e in self.list(scope="mission", scope_id=mission_id)
                    if stage_applies(e, stage)
                ]
            )
        for aid in agent_ids or []:
            buckets.append(self.list(scope="agent", scope_id=aid))
        # 打分排序：相关度优先，同分保持 updated_at 倒序（list 已排序）
        scored = [(e, _score_entry(e, terms)) for b in buckets for e in b]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [e for e, _ in scored[:limit]]

    def render_block(self, entries: list[MemoryEntry]) -> str:
        """候选集 → 提示词简介块（只注入 summary 行，全文由 Agent 按需取）。"""
        chunks = [f"- [{e.kind}|{e.id}] {e.display_summary()}" for e in entries]
        if not chunks:
            return ""
        body = "Memory brief:\n" + "\n".join(chunks)
        if len(body) <= CONTEXT_BLOCK_MAX_CHARS:
            return body
        return body[: CONTEXT_BLOCK_MAX_CHARS - 3].rstrip() + "..."

    def context_block(
        self,
        *,
        user_id: str | None = None,
        mission_id: str | None = None,
        agent_ids: list[str] | None = None,
        stage: str | None = None,
        query: str | None = None,
        limit: int = 12,
    ) -> str:
        """Compact memory brief for agent prompts / chat injection."""
        entries = self.context_candidates(
            user_id=user_id,
            mission_id=mission_id,
            agent_ids=agent_ids,
            stage=stage,
            query=query,
            limit=limit,
        )
        return self.render_block(entries)


class Mem0Memory:
    """mem0 shared-memory backend (REST) — runs *alongside* the local store.

    Entries are isolated per initiative via ``user_id = "<prefix>:<initiative_id>"``.
    All calls degrade silently (network failures never break a stage); the local
    :class:`MemoryStore` remains the source of truth.
    """

    def __init__(self, *, base_url: str, api_key: str | None, user_prefix: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_prefix = user_prefix or "sortie"
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _user_id(self, initiative_id: str | None) -> str:
        return f"{self.user_prefix}:{initiative_id or 'global'}"

    def add(self, content: str, *, initiative_id: str | None, tags: list[str] | None = None) -> bool:
        try:
            import httpx

            resp = httpx.post(
                f"{self.base_url}/v2/memories",
                json={
                    "memory": content.strip(),
                    "user_id": self._user_id(initiative_id),
                    "metadata": {"tags": tags or []},
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            return resp.status_code < 500
        except Exception:  # noqa: BLE001 — mem0 is an enhancement, never a dependency
            return False

    def search(self, query: str, *, initiative_id: str | None, limit: int = 8) -> list[str]:
        try:
            import httpx

            resp = httpx.post(
                f"{self.base_url}/v2/memories/search",
                json={
                    "query": query,
                    "user_id": self._user_id(initiative_id),
                    "limit": limit,
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            if resp.status_code >= 500:
                return []
            rows = resp.json()
            if isinstance(rows, dict):
                rows = rows.get("results") or rows.get("memories") or []
            out: list[str] = []
            for row in rows or []:
                if isinstance(row, dict):
                    text = row.get("memory") or row.get("text") or ""
                    if text:
                        out.append(str(text))
                elif isinstance(row, str):
                    out.append(row)
            return out
        except Exception:  # noqa: BLE001
            return []

    def health(self) -> bool:
        try:
            import httpx

            resp = httpx.get(f"{self.base_url}/health", headers=self._headers(), timeout=self.timeout)
            return resp.status_code < 500
        except Exception:  # noqa: BLE001
            return False


class CompositeMemory:
    """Local :class:`MemoryStore` + optional mem0 — one interface for all callers.

    Writes always hit the local store; mem0 receives a mirror when configured.
    Reads merge mem0 search hits into the context block (dedup by content).
    """

    def __init__(self, local: MemoryStore, mem0: Mem0Memory | None = None) -> None:
        self.local = local
        self.mem0 = mem0

    # --- delegated local store API ------------------------------------------

    def list(self, **kwargs: Any) -> list[MemoryEntry]:
        return self.local.list(**kwargs)

    def get(self, mem_id: str) -> MemoryEntry:
        return self.local.get(mem_id)

    def create(self, req: CreateMemoryRequest, *, author_id: str, author_name: str) -> MemoryEntry:
        entry = self.local.create(req, author_id=author_id, author_name=author_name)
        if self.mem0 is not None and entry.scope == "mission":
            self.mem0.add(entry.content, initiative_id=entry.scope_id, tags=entry.tags)
        return entry

    def update(self, mem_id: str, req: UpdateMemoryRequest) -> MemoryEntry:
        return self.local.update(mem_id, req)

    def delete(self, mem_id: str) -> None:
        self.local.delete(mem_id)

    def list_for_stage(self, **kwargs: Any) -> list[MemoryEntry]:
        return self.local.list_for_stage(**kwargs)

    # --- merged context --------------------------------------------------------

    def context_block(
        self,
        *,
        user_id: str | None = None,
        mission_id: str | None = None,
        agent_ids: list[str] | None = None,
        stage: str | None = None,
        limit: int = 12,
    ) -> str:
        block = self.local.context_block(
            user_id=user_id,
            mission_id=mission_id,
            agent_ids=agent_ids,
            stage=stage,
            limit=limit,
        )
        if self.mem0 is None:
            return block
        query = " ".join(x for x in (mission_id, stage) if x).strip() or "mission context"
        hits = self.mem0.search(query, initiative_id=mission_id, limit=6)
        if not hits:
            return block
        mem0_section = "Shared memory (mem0):\n" + "\n".join(f"- {h}" for h in hits)
        merged = f"{block}\n\n{mem0_section}".strip()
        if len(merged) <= CONTEXT_BLOCK_MAX_CHARS:
            return merged
        return merged[: CONTEXT_BLOCK_MAX_CHARS - 3].rstrip() + "..."


def build_memory_store(path: Path | None = None) -> MemoryStore | CompositeMemory:
    """Factory honouring ``TDT_MEMORY_BACKEND`` — local only, or local + mem0."""
    from sortie_deck.settings import settings

    store = MemoryStore(path or settings.memory_db)
    if settings.memory_backend.strip().lower() == "mem0" and settings.mem0_base_url:
        mem0 = Mem0Memory(
            base_url=settings.mem0_base_url,
            api_key=settings.mem0_api_key,
            user_prefix=settings.mem0_user_prefix,
            timeout=settings.mem0_timeout,
        )
        return CompositeMemory(local=store, mem0=mem0)
    return store
