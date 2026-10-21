from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
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
    # 记忆治理状态：active=生效；superseded=被新条目取代；expired=过期。
    # bi-temporal 思路：失效不删除，superseded_by 指向取代者 id，支持追溯演变链。
    status: Literal["active", "superseded", "expired"] = "active"
    superseded_by: str = ""
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


def _recency_bonus(entry: MemoryEntry, days: float) -> float:
    """新近度加分：最近 N 天内更新的记忆线性衰减加分（最高 3 分），旧记忆沉底不消失。

    业界共识（Mem0/Zep）：语义记忆的更新路径比写入路径更重要，检索需新近度
    加权防止高频记忆在事实变化后"自信地错误"。解析失败返回 0（不打分惩罚）。
    """
    if days <= 0:
        return 0.0
    try:
        updated = datetime.fromisoformat(entry.updated_at)
        age_days = (utc_now() - updated).total_seconds() / 86400
    except (ValueError, TypeError):
        return 0.0
    if age_days < 0 or age_days >= days:
        return 0.0
    return round((1.0 - age_days / days) * 3.0, 3)


def merge_stage_conclusions(
    entries: list[MemoryEntry],
) -> tuple[list[MemoryEntry], list[MemoryEntry]]:
    """并行分支结论合并仲裁：同主题多分支按 Provenance 权重选"采纳"，其余作参照。

    业界规则（多智能体记忆综述）：并行分支冲突禁止后写覆盖，用角色权重/新近度
    仲裁并保留全部结论。权重 = 人工写入(+2) > 反思沉淀(+1) > 其他，叠加新近度。

    返回 (merged, references)：merged 为排序结果——不带 stage 标签的条目在前、
    采纳分支居中、落选分支（追加 branch:reference 标签的副本）在末尾；
    references 为落选副本列表。落选用 model_copy 追加标签，render_block
    据此缩进标注"另一分支结论"，信息不丢失。
    """
    from sortie_deck.settings import settings

    threshold = float(settings.memory_conflict_threshold)
    tagged = [e for e in entries if stage_tag(e) is not None]
    untagged = [e for e in entries if stage_tag(e) is None]
    if len(tagged) <= 1:
        return [*untagged, *tagged], []

    def _weight(e: MemoryEntry) -> float:
        w = _recency_bonus(e, 7.0)
        if e.source == "manual":
            w += 2.0  # 人工写入权重最高
        elif e.source == "reflection":
            w += 1.0  # 反思沉淀次之
        return w

    merged: list[MemoryEntry] = []
    references: list[MemoryEntry] = []
    for e in sorted(tagged, key=_weight, reverse=True):
        terms_e = _terms(e.content)
        is_ref = False
        for kept in merged:
            kt = _terms(kept.content)
            if (
                terms_e
                and kt
                and len(terms_e & kt) / min(len(terms_e), len(kt)) >= threshold
            ):
                is_ref = True  # 与已采纳结论同主题 → 落选为参照分支
                break
        if is_ref:
            copy = e.model_copy(deep=True)
            copy.tags = [*copy.tags, "branch:reference"]
            references.append(copy)
        else:
            merged.append(e)
    return [*untagged, *merged, *references], references


def enforce_agent_lesson_cap(store: Any, cap: int) -> int:
    """agent 作用域 lesson 数量上限：超限按 updated_at 淘汰最旧，返回淘汰数。

    反思沉淀每 stage 一条，需上限防无限堆积。过程性经验没有追溯价值，
    直接 delete（区别于语义记忆的 bi-temporal 失效保留策略）。
    """
    if cap <= 0:
        return 0
    try:
        lessons = sorted(
            (e for e in store.list(scope="agent") if e.kind == "lesson"),
            key=lambda e: e.updated_at,
        )
    except Exception:  # noqa: BLE001 — 淘汰是治理增强，失败不影响写入方
        return 0
    removed = 0
    for e in lessons[: max(0, len(lessons) - cap)]:
        try:
            store.delete(e.id)
            removed += 1
        except Exception:  # noqa: BLE001 — 单条失败继续，不中断淘汰
            continue
    return removed


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
            status_mark = "" if e.status == "active" else f" [已失效:{e.status}]"
            lines.append(
                f"- {e.id} | {label}{target} / {e.kind}{status_mark} | {e.display_summary()}"
            )
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
        include_inactive: bool = False,
    ) -> list[MemoryEntry]:
        with self._lock:
            entries = self._read()
        out: list[MemoryEntry] = []
        for e in entries:
            if not include_inactive and e.status != "active":
                # 治理态条目（superseded/expired）默认不可见，仅显式查询（演变追溯）时返回
                continue
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
        # 按 id 精确取含治理态条目（superseded 演变链追溯需要能读到被取代者）
        for e in self.list(include_inactive=True):
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
        marked: list[MemoryEntry] = []
        with self._lock:
            entries = self._read()
            # 冲突检测（本地规则兜底）：同域同类型且内容高度重合 → 旧条目标记
            # superseded（失效不删除，superseded_by 保留演变链），零 LLM 成本；
            # mem0 开启时由 CompositeMemory 依赖其 LLM 决策做更准确的判定。
            # mission 作用域豁免：并行分支的阶段结论本就相似，需保留作参照分支
            # （由 merge_stage_conclusions 仲裁），不能被冲突检测提前吞掉
            if entry.scope != "mission":
                from sortie_deck.settings import settings

                marked = self._supersede_conflicts_locked(
                    entries,
                    scope=entry.scope,
                    scope_id=entry.scope_id,
                    kind=entry.kind,
                    content=entry.content,
                    superseded_by=entry.id,
                    threshold=settings.memory_conflict_threshold,
                )
            entries.append(entry)
            self._write(entries)
        # WeKnora 镜像（best-effort）：新条目上镜像，被取代条目撤镜像
        self._mirror(entry)
        for m in marked:
            self._mirror(m, removed=True)
        return entry

    def _supersede_conflicts_locked(
        self,
        entries: list[MemoryEntry],
        *,
        scope: str,
        scope_id: str,
        kind: str,
        content: str,
        superseded_by: str,
        threshold: float,
    ) -> list[MemoryEntry]:
        """就地把与 content 高度重合的 active 条目标记为 superseded（调用方需持锁）。

        返回被标记的条目列表（供调用方做镜像清理等后续动作）。
        """
        if threshold <= 0:
            return []
        new_terms = _terms(content)
        if not new_terms:
            return []
        marked: list[MemoryEntry] = []
        for i, e in enumerate(entries):
            if e.id == superseded_by or e.status != "active":
                continue
            if e.scope != scope or e.scope_id != scope_id or e.kind != kind:
                continue
            old_terms = _terms(e.content)
            if not old_terms:
                continue
            overlap = len(new_terms & old_terms) / min(len(new_terms), len(old_terms))
            if overlap >= threshold:
                data = e.model_dump()
                data["status"] = "superseded"
                data["superseded_by"] = superseded_by
                data["updated_at"] = utc_now().isoformat()
                entries[i] = MemoryEntry.model_validate(data)
                marked.append(entries[i])
        return marked

    def mark_superseded_by_content(
        self,
        *,
        scope: str,
        scope_id: str,
        kind: str,
        content: str,
        superseded_by: str,
        threshold: float,
    ) -> int:
        """公开版冲突标记：mem0 判定 UPDATE/DELETE 后由 CompositeMemory 调用跟随。

        以调用方给的阈值就地标记被取代条目并持久化，返回标记数（0 表示无冲突）。
        """
        with self._lock:
            entries = self._read()
            marked = self._supersede_conflicts_locked(
                entries,
                scope=scope,
                scope_id=scope_id,
                kind=kind,
                content=content,
                superseded_by=superseded_by,
                threshold=threshold,
            )
            if marked:
                self._write(entries)
            return len(marked)

    def update(self, mem_id: str, req: UpdateMemoryRequest) -> MemoryEntry:
        updated: MemoryEntry | None = None
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
                updated = entries[i]
                break
        if updated is None:
            raise KeyError(mem_id)
        self._mirror(updated)
        return updated

    def delete(self, mem_id: str) -> None:
        removed: MemoryEntry | None = None
        with self._lock:
            entries = self._read()
            removed = next((e for e in entries if e.id == mem_id), None)
            next_entries = [e for e in entries if e.id != mem_id]
            if len(next_entries) == len(entries):
                raise KeyError(mem_id)
            self._write(next_entries)
        self._mirror(removed, removed=True)

    def _mirror(self, entry: MemoryEntry | None, *, removed: bool = False) -> None:
        """best-effort 镜像到 WeKnora（TDT_MEMORY_RETRIEVAL=weknora 时生效）。

        active 条目上镜像，治理态（superseded/expired）与删除撤镜像；
        任何失败只打 warning——镜像检索是增强，本地打分路径永远兜底。
        """
        if entry is None:
            return
        try:
            from sortie_deck.settings import settings

            if (settings.memory_retrieval or "local").strip().lower() != "weknora":
                return
            from sortie_deck.memory_mirror import get_memory_mirror

            mirror = get_memory_mirror()
            if removed or entry.status != "active":
                mirror.remove(entry.id)
            else:
                mirror.upsert(entry)
        except Exception:  # noqa: BLE001 — 镜像是增强，失败绝不阻塞记忆写入
            logging.getLogger(__name__).warning(
                "记忆 WeKnora 镜像失败(忽略): id=%s removed=%s", entry.id, removed
            )

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
        from sortie_deck.settings import settings

        recency_days = float(settings.memory_recency_bonus_days)
        buckets: list[list[MemoryEntry]] = []
        buckets.append(self.list(scope="workspace", scope_id="global"))
        if user_id:
            buckets.append(self.list(scope="user", scope_id=user_id, user_id=user_id))
        if mission_id:
            stage_entries = [
                e
                for e in self.list(scope="mission", scope_id=mission_id)
                if stage_applies(e, stage)
            ]
            # 并行分支结论合并仲裁：同主题多分支按 Provenance 权重选采纳，其余作参照
            merged, _ = merge_stage_conclusions(stage_entries)
            buckets.append(merged)
        for aid in agent_ids or []:
            buckets.append(self.list(scope="agent", scope_id=aid))
        # 本地打分（兜底路径 / WeKnora 不足时的补足）：关键词相关度 + 新近度加权，
        # 同分保持 updated_at 倒序（list 已排序）
        scored = [
            (e, _score_entry(e, terms) + _recency_bonus(e, recency_days))
            for b in buckets
            for e in b
        ]
        # WeKnora 语义召回路径：混合检索 + rerank（graph 模式走图谱扩展），
        # 召回序优先，本地打分只补足召回不足 limit 的部分
        if (settings.memory_retrieval or "local").strip().lower() == "weknora":
            picked = self._weknora_pick(buckets=buckets, scored=scored, query=query, limit=limit)
            if picked is not None:
                return picked
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [e for e, _ in scored[:limit]]

    def _weknora_pick(
        self,
        *,
        buckets: list[list[MemoryEntry]],
        scored: list[tuple[MemoryEntry, float]],
        query: str | None,
        limit: int,
    ) -> list[MemoryEntry] | None:
        """WeKnora 语义召回 → 回映射本地条目 → 可见性过滤 → 本地打分补足。

        返回 None 表示 WeKnora 路径不可用（未启用 / 失败 / 空召回），调用方走本地打分。
        可见性以 buckets（已含作用域 / stage / 治理态过滤）为准：WeKnora 全库召回中
        不在 buckets 里的条目（其他任务 / 他人隐私 / 已失效）一律丢弃——WeKnora
        只负责召回与语义排序，结构化权限过滤始终在本地。
        """
        if not (query or "").strip():
            return None
        try:
            from sortie_deck.memory_mirror import retrieve_memory_ids

            recalled = retrieve_memory_ids(query.strip(), limit=limit * 3)
        except Exception:  # noqa: BLE001 — 语义召回失败走本地打分
            return None
        if not recalled:
            return None
        visible = {e.id: e for b in buckets for e in b}
        picked = [visible[mid] for mid in recalled if mid in visible]
        seen = {e.id for e in picked}
        if len(picked) < limit:
            for e, _s in scored:
                if e.id not in seen:
                    picked.append(e)
                    seen.add(e.id)
                    if len(picked) >= limit:
                        break
        return picked[:limit]

    def render_block(self, entries: list[MemoryEntry]) -> str:
        """候选集 → 提示词简介块（只注入 summary 行，全文由 Agent 按需取）。"""
        chunks: list[str] = []
        for e in entries:
            line = f"- [{e.kind}|{e.id}] {e.display_summary()}"
            if "branch:reference" in e.tags:
                # 并行分支仲裁的落选结论：缩进标注供模型参考，避免信息丢失
                line = f"  ↳ {line}（另一分支结论）"
            chunks.append(line)
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

    def add(
        self,
        content: str,
        *,
        initiative_id: str | None,
        tags: list[str] | None = None,
        kind: str | None = None,
    ) -> str:
        """写入一条远端记忆，返回 mem0 冲突决策事件串（如 "ADD" / "UPDATE,DELETE"）。

        mem0 写入侧原生执行 LLM 冲突决策（新增/更新覆盖/删除冗余），即 Azure
        Foundry Consolidation 的等价能力；调用方据此在本地跟随标记演变链。
        metadata 增强：携带 kind 便于远端按记忆类型过滤。失败返回空串（静默降级）。
        """
        try:
            import httpx

            metadata: dict[str, Any] = {"tags": tags or []}
            if kind:
                metadata["kind"] = kind
            resp = httpx.post(
                f"{self.base_url}/v2/memories",
                json={
                    "memory": content.strip(),
                    "user_id": self._user_id(initiative_id),
                    "metadata": metadata,
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            if resp.status_code >= 500:
                return ""
            try:
                rows = resp.json()
            except ValueError:
                return "ADD"
            events = [
                str(row.get("event") or "ADD")
                for row in (rows or [])
                if isinstance(row, dict)
            ]
            return ",".join(events) or "ADD"
        except Exception:  # noqa: BLE001 — mem0 is an enhancement, never a dependency
            return ""

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
            raw = self.mem0.add(
                entry.content, initiative_id=entry.scope_id, tags=entry.tags, kind=entry.kind
            )
            # 兼容自定义后端返回 bool（旧签名）：True 视为 ADD，False 视为失败
            events = "ADD" if raw is True else str(raw or "")
            # mem0 判定 UPDATE/DELETE → 本地跟随：以更宽松阈值把被取代的旧条目
            # 标记为 superseded，保持两端演变链一致（mem0 判冲突、本地跟随）
            if any(ev in ("UPDATE", "DELETE") for ev in events.split(",")):
                try:
                    from sortie_deck.settings import settings

                    self.local.mark_superseded_by_content(
                        scope=entry.scope,
                        scope_id=entry.scope_id,
                        kind=entry.kind,
                        content=entry.content,
                        superseded_by=entry.id,
                        threshold=min(settings.memory_conflict_threshold, 0.5),
                    )
                except Exception:  # noqa: BLE001 — 治理跟随是增强项，失败不阻断写入
                    pass
        return entry

    def update(self, mem_id: str, req: UpdateMemoryRequest) -> MemoryEntry:
        return self.local.update(mem_id, req)

    def delete(self, mem_id: str) -> None:
        self.local.delete(mem_id)

    def list_for_stage(self, **kwargs: Any) -> list[MemoryEntry]:
        return self.local.list_for_stage(**kwargs)

    def context_candidates(self, **kwargs: Any) -> list[MemoryEntry]:
        """候选集：本地检索 + mem0 检索命中前置（Plan-time / Run-time 共用）。

        mem0 检索返回文本（可能被 mem0 改写），按内容互含匹配回映射本地条目，
        命中者排前（语义相关性优先），其余保持本地打分序；映射不上的命中丢弃
        ——mem0 是增强不是数据源，本地候选集始终是可见性边界。
        """
        local = self.local.context_candidates(**kwargs)
        if self.mem0 is None:
            return local
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return local
        try:
            hits = self.mem0.search(query, initiative_id=kwargs.get("mission_id"), limit=6)
        except Exception:  # noqa: BLE001 — mem0 是增强，失败走纯本地序
            return local
        if not hits:
            return local

        def _boosted(e: MemoryEntry) -> bool:
            content = e.content.strip()
            return any(t and (t in content or content in t) for t in hits)

        boosted = [e for e in local if _boosted(e)]
        rest = [e for e in local if not _boosted(e)]
        limit = int(kwargs.get("limit") or 8)
        return (boosted + rest)[:limit]

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
