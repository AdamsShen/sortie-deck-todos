"""Knowledge gateway: local catalog + optional WeKnora enterprise backend.

Sortie Deck owns import + directory/catalog. Retrieval always delegates to WeKnora
when `kb_backend=weknora`; local JSON is catalog/fallback only.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from sortie_deck.knowledge import (
    CreateKnowledgeRequest,
    KnowledgeDoc,
    KnowledgeStore,
    UpdateKnowledgeRequest,
)
from sortie_deck.models import new_id, utc_now
from sortie_deck.weknora import WeKnoraClient, WeKnoraError, WeKnoraHit, WeKnoraKnowledge

logger = logging.getLogger(__name__)


class RetrieveHit(BaseModel):
    title: str
    content: str
    score: float = 0.0
    remote_id: str | None = None
    source: Literal["weknora", "local"] = "local"
    folder_path: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class ImportKnowledgeRequest(BaseModel):
    """Import into enterprise KB (and mirror into local catalog)."""

    title: str | None = None
    body: str | None = None
    url: str | None = None
    file_path: str | None = None
    folder_path: str = ""
    tags: list[str] = Field(default_factory=list)
    category: Literal["playbook", "runbook", "lore", "reference"] = "reference"
    # 归属知识库（库实体）id；空串表示默认全局库
    base_id: str = ""


class KnowledgeGateway:
    def __init__(
        self,
        store: KnowledgeStore,
        *,
        backend: str = "local",
        weknora_base_url: str | None = None,
        weknora_api_key: str | None = None,
        weknora_kb_id: str | None = None,
        weknora_retrieval_mode: str = "hybrid",
        weknora_chat_timeout: float = 90.0,
        client: WeKnoraClient | None = None,
    ) -> None:
        self.store = store
        self.backend = (backend or "local").strip().lower()
        self.weknora_kb_id = (weknora_kb_id or "").strip() or None
        self.retrieval_mode = (weknora_retrieval_mode or "hybrid").strip().lower()
        self.chat_timeout = float(weknora_chat_timeout or 90.0)
        self._client = client
        self._weknora_base_url = weknora_base_url
        self._weknora_api_key = weknora_api_key

    @property
    def uses_weknora(self) -> bool:
        return self.backend == "weknora" and bool(self.weknora_kb_id)

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "backend": self.backend if self.uses_weknora else "local",
            "weknora_configured": self.uses_weknora,
            "weknora_kb_id": self.weknora_kb_id,
            "catalog_count": len(self.store.list()),
        }
        if self.uses_weknora:
            try:
                health = self.client().health()
                info["weknora"] = health
            except WeKnoraError as exc:
                info["weknora"] = {"ok": False, "error": str(exc)}
            info["retrieval_mode"] = self.retrieval_mode
            try:
                detail = self.client().knowledge_base_detail(self.weknora_kb_id or "")
                info["graph_enabled"] = bool((detail.get("capabilities") or {}).get("graph"))
            except WeKnoraError as exc:
                logger.warning("weknora status: kb detail failed: %s", exc)
        return info

    def client(self) -> WeKnoraClient:
        if not self.uses_weknora:
            raise WeKnoraError("WeKnora backend is not configured (set TDT_KB_BACKEND=weknora)")
        if self._client is None:
            if not self._weknora_base_url:
                raise WeKnoraError("TDT_WEKNORA_BASE_URL is required")
            self._client = WeKnoraClient(self._weknora_base_url, self._weknora_api_key)
        return self._client

    def list_catalog(self, q: str | None = None) -> list[KnowledgeDoc]:
        return self.store.list(q)

    def get(self, doc_id: str) -> KnowledgeDoc:
        return self.store.get(doc_id)

    def create_local(
        self, req: CreateKnowledgeRequest, *, author_id: str, author_name: str
    ) -> KnowledgeDoc:
        """Create catalog entry; when WeKnora is on, also push manual content."""
        if self.uses_weknora and (req.body or "").strip():
            remote = self.client().create_manual(
                self.weknora_kb_id or "",
                title=req.title.strip(),
                content=req.body,
            )
            doc = KnowledgeDoc(
                title=req.title.strip(),
                summary=req.summary.strip() or remote.description,
                body="",  # body lives in WeKnora
                tags=[t.strip() for t in req.tags if t.strip()],
                category=req.category,
                base_id=req.base_id,
                author_id=author_id,
                author_name=author_name,
                backend="weknora",
                remote_id=remote.id,
                remote_kb_id=remote.knowledge_base_id or self.weknora_kb_id,
                folder_path=remote.folder_path,
                parse_status=remote.parse_status,
            )
            with self.store._lock:
                docs = self.store._read()
                docs.append(doc)
                self.store._write(docs)
            return doc
        return self.store.create(req, author_id=author_id, author_name=author_name)

    def update(self, doc_id: str, req: UpdateKnowledgeRequest) -> KnowledgeDoc:
        return self.store.update(doc_id, req)

    def delete(self, doc_id: str, *, also_remote: bool = True) -> None:
        doc = self.store.get(doc_id)
        if also_remote and doc.remote_id and self.uses_weknora:
            with contextlib.suppress(WeKnoraError):
                self.client().delete_knowledge(doc.remote_id)
        self.store.delete(doc_id)

    def import_doc(
        self,
        req: ImportKnowledgeRequest,
        *,
        author_id: str,
        author_name: str,
    ) -> KnowledgeDoc:
        if not self.uses_weknora:
            # Local fallback: treat as create
            return self.store.create(
                CreateKnowledgeRequest(
                    title=req.title or "Imported",
                    summary="",
                    body=req.body or req.url or "",
                    tags=req.tags,
                    category=req.category,
                    base_id=req.base_id,
                ),
                author_id=author_id,
                author_name=author_name,
            )

        client = self.client()
        kb_id = self.weknora_kb_id or ""
        remote: WeKnoraKnowledge
        if req.file_path:
            remote = client.create_from_file(kb_id, Path(req.file_path))
        elif req.url:
            remote = client.create_from_url(kb_id, req.url)
        elif req.body is not None:
            remote = client.create_manual(
                kb_id,
                title=(req.title or "Untitled").strip(),
                content=req.body,
            )
        else:
            raise ValueError("import requires file_path, url, or body")

        if req.folder_path and remote.id:
            try:
                client.move_to_folder(
                    knowledge_ids=[remote.id],
                    folder_path=req.folder_path,
                    knowledge_base_id=kb_id,
                )
                remote.folder_path = req.folder_path
            except WeKnoraError:
                pass

        return self._upsert_catalog_from_remote(
            remote,
            tags=req.tags,
            category=req.category,
            author_id=author_id,
            author_name=author_name,
            title_override=req.title,
            base_id=req.base_id,
        )

    def refresh_parse_status(self, base_id: str) -> list[KnowledgeDoc]:
        """刷新某库内 WeKnora 文档的解析状态（一次 list_knowledge 全量映射，避免逐文档请求）。

        local 模式无远程状态，直接返回现状；WeKnoraError 由路由层转 502。
        远端已删除的文档保持原状态不动。
        """
        docs = self.store.list(base_id=base_id)
        if not self.uses_weknora:
            return docs
        remote_rows = self.client().list_knowledge(self.weknora_kb_id or "")
        by_remote = {rk.id: rk for rk in remote_rows if rk.id}
        out: list[KnowledgeDoc] = []
        for d in docs:
            if not d.remote_id:
                out.append(d)
                continue
            rk = by_remote.get(d.remote_id)
            if rk is None or rk.parse_status == d.parse_status:
                out.append(d)
                continue
            out.append(self.store.update(d.id, UpdateKnowledgeRequest(parse_status=rk.parse_status)))
        return out

    def sync_catalog(self, *, author_name: str = "WeKnora") -> dict[str, Any]:
        """Refresh local catalog from WeKnora list (directory management)."""
        if not self.uses_weknora:
            return {"synced": 0, "backend": "local"}
        remote_rows = self.client().list_knowledge(self.weknora_kb_id or "")
        existing = {d.remote_id: d for d in self.store.list() if d.remote_id}
        added = 0
        updated = 0
        with self.store._lock:
            docs = self.store._read()
            by_remote = {d.remote_id: i for i, d in enumerate(docs) if d.remote_id}
            for remote in remote_rows:
                if not remote.id:
                    continue
                if remote.id in by_remote:
                    i = by_remote[remote.id]
                    data = docs[i].model_dump()
                    data.update(
                        {
                            "title": remote.title or docs[i].title,
                            "summary": remote.description or docs[i].summary,
                            "folder_path": remote.folder_path,
                            "parse_status": remote.parse_status,
                            "remote_kb_id": remote.knowledge_base_id or self.weknora_kb_id,
                            "backend": "weknora",
                            "updated_at": utc_now().isoformat(),
                        }
                    )
                    docs[i] = KnowledgeDoc.model_validate(data)
                    updated += 1
                else:
                    docs.append(
                        KnowledgeDoc(
                            title=remote.title or remote.file_name or remote.id,
                            summary=remote.description,
                            body="",
                            tags=[],
                            category="reference",
                            author_name=author_name,
                            backend="weknora",
                            remote_id=remote.id,
                            remote_kb_id=remote.knowledge_base_id or self.weknora_kb_id,
                            folder_path=remote.folder_path,
                            parse_status=remote.parse_status,
                        )
                    )
                    added += 1
            self.store._write(docs)
        folders = None
        try:
            folders = self.client().list_folders(self.weknora_kb_id or "")
        except WeKnoraError:
            folders = None
        return {
            "synced": added + updated,
            "added": added,
            "updated": updated,
            "remote_count": len(remote_rows),
            "catalog_count": len(self.store.list()),
            "known_before": len(existing),
            "folders": folders,
        }

    def list_folders(self) -> Any:
        if not self.uses_weknora:
            # Derive folders from local catalog paths
            paths = sorted({d.folder_path for d in self.store.list() if d.folder_path})
            return {"folders": paths}
        return self.client().list_folders(self.weknora_kb_id or "")

    def retrieve(self, query: str, *, limit: int = 6, initiative_id: str | None = None) -> list[RetrieveHit]:
        """Mission retrieval — WeKnora hybrid search, or local keyword fallback.

        When ``initiative_id`` is given, the mission namespace (docs tagged
        ``ini:<id>``) is searched first and ranked ahead of the global pool —
        each requirement's knowledge stays isolated yet still benefits from
        the shared library.
        """
        q = (query or "").strip()
        if not q:
            return []
        mission_hits: list[RetrieveHit] = []
        if initiative_id:
            mission_hits = self._retrieve_mission(q, initiative_id, limit=limit)
            if len(mission_hits) >= limit:
                return mission_hits[:limit]
        remaining = max(1, limit - len(mission_hits))
        seen_ids = {h.remote_id for h in mission_hits}
        if self.uses_weknora:
            global_hits: list[RetrieveHit] = []
            if self.retrieval_mode == "graph":
                global_hits = self._retrieve_graph(q, remaining, seen_ids)
            if not global_hits:
                # graph 模式失败/无结果 → 降级 hybrid-search（保底可用）
                hits = self.client().hybrid_search(
                    self.weknora_kb_id or "",
                    q,
                    match_count=max(1, remaining + len(seen_ids)),
                )
                global_hits = [
                    self._hit_from_weknora(h) for h in hits if h.knowledge_id not in seen_ids
                ][:remaining]
            return mission_hits + global_hits
        # local fallback (dev only)
        from sortie_deck.knowledge_pack import search_for_mission

        ranked = search_for_mission(self.store, q, limit=remaining + len(seen_ids))
        global_hits = [
            RetrieveHit(
                title=doc.title,
                content=doc.body or doc.summary,
                score=score,
                remote_id=doc.id,
                source="local",
                folder_path=doc.folder_path,
                meta={"category": doc.category, "tags": doc.tags},
            )
            for doc, score in ranked
            if doc.id not in seen_ids
        ][:remaining]
        return mission_hits + global_hits

    def _retrieve_graph(
        self, query: str, remaining: int, seen_ids: set[str | None]
    ) -> list[RetrieveHit]:
        """graph 模式全局池：走 knowledge-chat（图谱实体扩展）检索。

        WeKnora 的 GraphRAG 增强（查询实体识别 → Neo4j 图谱一跳扩展 →
        关联 chunk 补充召回）只在该管线生效；失败时返回空列表，
        由调用方降级到 hybrid-search，绝不让检索中断。
        """
        try:
            session_id = self.client().create_session()
        except WeKnoraError as exc:
            logger.warning("weknora graph retrieve: create_session failed: %s", exc)
            return []
        try:
            hits = self.client().knowledge_chat_references(
                session_id,
                query,
                [self.weknora_kb_id or ""],
                timeout=self.chat_timeout,
            )
        except WeKnoraError as exc:
            logger.warning(
                "weknora graph retrieve failed, fallback to hybrid: %s", exc
            )
            return []
        finally:
            self.client().delete_session(session_id)
        return [
            self._hit_from_weknora(h)
            for h in hits
            if h.knowledge_id not in seen_ids
        ][:max(1, remaining)]

    # --- mission namespace ---------------------------------------------------

    @staticmethod
    def mission_tag(initiative_id: str) -> str:
        return f"ini:{initiative_id}"

    def mission_folder(self, initiative_id: str) -> str:
        from sortie_deck.settings import settings

        prefix = (settings.kb_mission_folder_prefix or "missions").strip("/")
        return f"{prefix}/{initiative_id}"

    def _retrieve_mission(self, query: str, initiative_id: str, *, limit: int) -> list[RetrieveHit]:
        """Keyword search restricted to this mission's namespace (local catalog)."""
        from sortie_deck.knowledge_pack import score_doc

        tag = self.mission_tag(initiative_id)
        ranked: list[tuple[KnowledgeDoc, float]] = []
        for doc in self.store.list():
            if tag not in doc.tags:
                continue
            score = score_doc(doc, query)
            if score > 0:
                ranked.append((doc, score))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return [
            RetrieveHit(
                title=doc.title,
                content=doc.body or doc.summary,
                score=score + 10.0,  # namespace boost: this mission's knowledge first
                remote_id=doc.id,
                source="local",
                folder_path=doc.folder_path,
                meta={"category": doc.category, "tags": doc.tags, "mission": initiative_id},
            )
            for doc, score in ranked[: max(1, limit)]
        ]

    def index_mission_artifact(
        self,
        *,
        initiative_id: str,
        stage: str,
        kind: str,
        title: str,
        content: str,
        author_name: str = "Sortie",
    ) -> KnowledgeDoc | None:
        """Best-effort writeback of a stage artifact into the mission namespace.

        Local: catalog entry tagged ``ini:<id>``. WeKnora: manual doc moved to
        folder ``<prefix>/<initiative_id>``. Failures degrade silently (never
        break a stage); idempotent per (initiative, stage, kind) title.
        """
        from sortie_deck.settings import settings

        if not settings.kb_mission_index:
            return None
        body = (content or "").strip()
        if not body:
            return None
        body = body[: settings.kb_mission_index_max_chars]
        doc_title = f"{title}".strip() or f"{initiative_id} · {stage} · {kind}"
        try:
            existing = next(
                (
                    d
                    for d in self.store.list()
                    if d.title == doc_title and self.mission_tag(initiative_id) in d.tags
                ),
                None,
            )
            if existing is not None:
                return existing  # idempotent across stage retries / resumes
            folder = self.mission_folder(initiative_id)
            if self.uses_weknora:
                try:
                    remote = self.client().create_manual(
                        self.weknora_kb_id or "",
                        title=doc_title,
                        content=body,
                    )
                    with contextlib.suppress(WeKnoraError):
                        self.client().move_to_folder(
                            knowledge_ids=[remote.id],
                            folder_path=folder,
                            knowledge_base_id=self.weknora_kb_id or "",
                        )  # folder move is cosmetic; keep the doc either way
                    return self._upsert_catalog_from_remote(
                        remote,
                        tags=[self.mission_tag(initiative_id), "mission", stage],
                        category="reference",
                        author_id="system",
                        author_name=author_name,
                        title_override=doc_title,
                    )
                except WeKnoraError:
                    pass  # remote write failed → fall through to local catalog
            return self.store.create(
                CreateKnowledgeRequest(
                    title=doc_title,
                    summary=body[:300],
                    body=body,
                    tags=[self.mission_tag(initiative_id), "mission", stage],
                    category="reference",
                ),
                author_id="system",
                author_name=author_name,
            )
        except Exception:  # noqa: BLE001 — knowledge writeback must never break a stage
            return None

    def _upsert_catalog_from_remote(
        self,
        remote: WeKnoraKnowledge,
        *,
        tags: list[str],
        category: str,
        author_id: str,
        author_name: str,
        title_override: str | None,
        base_id: str = "",
    ) -> KnowledgeDoc:
        doc = KnowledgeDoc(
            id=new_id("kb_"),
            title=(title_override or remote.title or remote.file_name or remote.id).strip(),
            summary=remote.description,
            body="",
            tags=[t.strip() for t in tags if t.strip()],
            category=category,  # type: ignore[arg-type]
            base_id=base_id,
            author_id=author_id,
            author_name=author_name,
            backend="weknora",
            remote_id=remote.id,
            remote_kb_id=remote.knowledge_base_id or self.weknora_kb_id,
            folder_path=remote.folder_path,
            parse_status=remote.parse_status,
        )
        with self.store._lock:
            docs = self.store._read()
            docs.append(doc)
            self.store._write(docs)
        return doc

    @staticmethod
    def _hit_from_weknora(h: WeKnoraHit) -> RetrieveHit:
        return RetrieveHit(
            title=h.knowledge_title or h.knowledge_filename or h.knowledge_id,
            content=h.content,
            score=h.score,
            remote_id=h.knowledge_id,
            source="weknora",
            meta={
                "chunk_id": h.chunk_id,
                "match_type": h.match_type,
                "knowledge_base_id": h.knowledge_base_id,
                "filename": h.knowledge_filename,
            },
        )


def gateway_from_settings(settings: Any, store: KnowledgeStore | None = None) -> KnowledgeGateway:
    path = settings.knowledge_db
    store = store or KnowledgeStore(path)
    return KnowledgeGateway(
        store,
        backend=getattr(settings, "kb_backend", "local"),
        weknora_base_url=getattr(settings, "weknora_base_url", None),
        weknora_api_key=getattr(settings, "weknora_api_key", None),
        weknora_kb_id=getattr(settings, "weknora_kb_id", None),
        weknora_retrieval_mode=getattr(settings, "weknora_retrieval_mode", "hybrid"),
        weknora_chat_timeout=getattr(settings, "weknora_chat_timeout", 90.0),
    )
