"""记忆条目 → WeKnora 镜像与语义检索（混合检索 + rerank + graph）。

设计（docs/plans/2026-10-21-memory-weknora-retrieval.md）：

- 本地 ``MemoryStore`` 永远是数据源；WeKnora 只承担「召回 + 语义排序」；
- 镜像：条目 create/update/delete 时 best-effort 同步，文档 ``title="mem:<id>"``
  作为检索命中后回映射本地条目的锚点（WeKnora 的 knowledge_id/chunk_id 与
  记忆条目 id 是两套体系，靠 title 桥接）；
- 检索：``hybrid_search``（向量 + BM25 + rerank，租户未配 rerank 模型时
  WeKnora 自动降级 RRF）；graph 模式走知识图谱一跳扩展，失败降级 hybrid；
- 一切失败静默降级：记忆是增强不是依赖，本地打分路径永远兜底。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sortie_deck.weknora import WeKnoraClient, WeKnoraError

if TYPE_CHECKING:
    from sortie_deck.memory import MemoryEntry

logger = logging.getLogger(__name__)

_MEM_TITLE_PREFIX = "mem:"
# 回映射锚点必须能从 title 解析，校验条目 id 形态防注入
_MEM_ID_RE = re.compile(r"^mem_[A-Za-z0-9_]+$")
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _retrieval_enabled() -> bool:
    from sortie_deck.settings import settings

    return (
        (settings.memory_retrieval or "local").strip().lower() == "weknora"
        and bool(settings.weknora_base_url)
    )


class MemoryMirror:
    """本地记忆条目 → WeKnora 文档镜像（best-effort，失败静默）。"""

    def __init__(self, client: WeKnoraClient, kb_id: str, state_path: Path) -> None:
        self.client = client
        self.kb_id = kb_id
        self.state_path = state_path
        # mem_id → 远端文档 id 的本地映射；丢失时 remove 走 title 扫描兜底
        self._state: dict[str, str] = self._load_state()

    # --- state ---------------------------------------------------------------

    def _load_state(self) -> dict[str, str]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self._state, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # 状态文件是加速结构，写失败只影响 remove 的扫描兜底

    # --- mirror write ----------------------------------------------------------

    @staticmethod
    def folder_for(entry: MemoryEntry) -> str:
        """按作用域划分镜像 folder（仅作召回辅助，权限过滤仍以本地条目为准）。"""
        safe = _SAFE_SEGMENT_RE.sub("_", str(entry.scope_id or "global"))[:40] or "global"
        return f"memory/{entry.scope}/{safe}"

    @staticmethod
    def body_for(entry: MemoryEntry) -> str:
        """镜像正文 = 记忆全文 + 结构化元数据行（提升召回可解释性与过滤信息量）。"""
        meta = f"[mem-id={entry.id} scope={entry.scope}/{entry.scope_id} kind={entry.kind}"
        if entry.tags:
            meta += f" tags={','.join(entry.tags)}"
        meta += "]"
        return f"{entry.content}\n\n{meta}"

    def upsert(self, entry: MemoryEntry) -> None:
        """镜像一条 active 记忆：先删旧镜像再重建（WeKnoraClient 无更新接口）。"""
        remote_id = self._state.get(entry.id)
        if remote_id:
            with contextlib.suppress(WeKnoraError):
                self.client.delete_knowledge(remote_id)
        doc = self.client.create_manual(
            self.kb_id,
            title=f"{_MEM_TITLE_PREFIX}{entry.id}",
            content=self.body_for(entry),
        )
        with contextlib.suppress(WeKnoraError):
            self.client.move_to_folder(
                knowledge_ids=[doc.id],
                folder_path=self.folder_for(entry),
                knowledge_base_id=self.kb_id,
            )
        self._state[entry.id] = doc.id
        self._save_state()

    def remove(self, mem_id: str) -> None:
        """删除远端镜像：优先走 state 映射，丢失时按 title 扫描兜底（best-effort）。"""
        remote_id = self._state.pop(mem_id, None)
        if not remote_id:
            try:
                for row in self.client.list_knowledge(self.kb_id):
                    if row.title == f"{_MEM_TITLE_PREFIX}{mem_id}":
                        remote_id = row.id
                        break
            except WeKnoraError as exc:
                logger.warning("记忆镜像 remove 扫描失败(忽略): id=%s: %s", mem_id, exc)
                return
        if remote_id:
            with contextlib.suppress(WeKnoraError):
                self.client.delete_knowledge(remote_id)
        self._save_state()

    def backfill(self, entries: list[MemoryEntry]) -> dict[str, int]:
        """存量 active 条目一次性回填，返回 {ok, failed} 统计（单条失败不中断）。"""
        ok = failed = 0
        for e in entries:
            try:
                self.upsert(e)
                ok += 1
            except WeKnoraError as exc:
                failed += 1
                logger.warning("记忆镜像回填失败: id=%s: %s", e.id, exc)
        return {"ok": ok, "failed": failed}


# --- semantic retrieval ------------------------------------------------------


def retrieve_memory_ids(query: str, *, limit: int) -> list[str]:
    """WeKnora 语义召回记忆条目 id（graph → hybrid → 空列表 三级降级）。

    只做召回与排序；作用域/治理态等结构化过滤由调用方（MemoryStore）完成。
    未启用或失败返回空列表，调用方走本地打分兜底。
    """
    if not _retrieval_enabled() or not (query or "").strip():
        return []
    mirror = get_memory_mirror()
    client = mirror.client
    kb_id = mirror.kb_id
    hits: list[Any] = []
    from sortie_deck.settings import settings

    mode = (settings.weknora_retrieval_mode or "hybrid").strip().lower()
    if mode == "graph":
        hits = _graph_hits(client, kb_id, query, limit)
    if not hits:
        try:
            hits = client.hybrid_search(kb_id, query, match_count=max(1, limit))
        except WeKnoraError as exc:
            logger.warning("记忆 WeKnora hybrid 检索失败，降级本地打分: %s", exc)
            return []
    ids: list[str] = []
    for h in hits:
        title = (getattr(h, "knowledge_title", "") or "").strip()
        if not title.startswith(_MEM_TITLE_PREFIX):
            continue  # 非记忆镜像文档（同库混用其他知识时）直接跳过
        mid = title[len(_MEM_TITLE_PREFIX) :].strip()
        if _MEM_ID_RE.match(mid) and mid not in ids:
            ids.append(mid)
    return ids


def _graph_hits(client: WeKnoraClient, kb_id: str, query: str, limit: int) -> list[Any]:
    """graph 模式：知识图谱实体扩展检索（GraphRAG），失败返回空由调用方降级 hybrid。"""
    try:
        session_id = client.create_session()
    except WeKnoraError as exc:
        logger.warning("记忆 WeKnora graph 检索: create_session 失败: %s", exc)
        return []
    try:
        refs = client.knowledge_chat_references(session_id, query, [kb_id])
    except WeKnoraError as exc:
        logger.warning("记忆 WeKnora graph 检索失败，降级 hybrid: %s", exc)
        return []
    finally:
        with contextlib.suppress(WeKnoraError):
            client.delete_session(session_id)
    return list(refs or [])[: max(1, limit)]


# --- singleton ---------------------------------------------------------------

_MIRROR: MemoryMirror | None = None


def get_memory_mirror() -> MemoryMirror:
    global _MIRROR
    if _MIRROR is None:
        from sortie_deck.settings import settings

        kb_id = (settings.weknora_memory_kb_id or settings.weknora_kb_id or "").strip()
        if not kb_id:
            raise WeKnoraError("TDT_WEKNORA_MEMORY_KB_ID / TDT_WEKNORA_KB_ID 均未配置")
        _MIRROR = MemoryMirror(
            WeKnoraClient(settings.weknora_base_url or "", settings.weknora_api_key),
            kb_id,
            settings.data_dir / "memory_mirror.json",
        )
    return _MIRROR


def reset_memory_mirror() -> None:
    global _MIRROR
    _MIRROR = None
