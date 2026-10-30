from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from sortie_deck.knowledge import KnowledgeDoc, KnowledgeStore

logger = logging.getLogger(__name__)

# Categories suited for enterprise KB (product / API / design / …)
ENTERPRISE_HINT_TAGS = (
    "product",
    "api",
    "openapi",
    "design",
    "architecture",
    "tech",
    "spec",
    "interface",
)


def _tokenize(text: str) -> set[str]:
    parts = re.findall(r"[\w\u4e00-\u9fff]{2,}", (text or "").lower())
    return set(parts)


def score_doc(doc: KnowledgeDoc, query: str) -> float:
    """Lightweight relevance score for local-catalog fallback only."""
    q = (query or "").strip().lower()
    if not q:
        return 0.0
    tokens = _tokenize(q)
    hay = f"{doc.title}\n{doc.summary}\n{' '.join(doc.tags)}\n{doc.body}".lower()
    score = 0.0
    if q in hay:
        score += 8.0
    for t in tokens:
        if t in doc.title.lower():
            score += 3.0
        if t in doc.summary.lower():
            score += 2.0
        if any(t in tag.lower() for tag in doc.tags):
            score += 2.5
        if t in doc.body.lower():
            score += 1.0
    if any(h in hay for h in ENTERPRISE_HINT_TAGS):
        score += 0.5
    if doc.category in {"reference", "playbook"}:
        score += 0.25
    return score


def search_for_mission(
    store: KnowledgeStore,
    query: str,
    *,
    limit: int = 6,
    min_score: float = 0.8,
) -> list[tuple[KnowledgeDoc, float]]:
    """Local keyword ranking — used only when WeKnora is not configured."""
    ranked: list[tuple[KnowledgeDoc, float]] = []
    for doc in store.list():
        s = score_doc(doc, query)
        if s >= min_score:
            ranked.append((doc, s))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[: max(1, limit)]


def render_knowledge_pack(
    hits: list[tuple[KnowledgeDoc, float]] | list[Any],
    *,
    query: str,
    target: str = "cursor",
) -> str:
    lines = [
        f"# Sortie Deck knowledge pack ({target})",
        "",
        "Auto-selected for this mission. Prefer these sources over guessing.",
        "",
        f"**Query:** {query.strip()[:500] or '(empty)'}",
        "",
    ]
    if not hits:
        lines.append(
            "_No matching knowledge. Import docs into the enterprise KB (WeKnora) "
            "or local catalog, then retry._"
        )
        return "\n".join(lines) + "\n"

    # Support both (KnowledgeDoc, score) and RetrieveHit-like objects
    normalized: list[tuple[str, str, float, str]] = []
    for item in hits:
        if isinstance(item, tuple) and len(item) == 2 and hasattr(item[0], "title"):
            doc, score = item
            body = getattr(doc, "body", "") or getattr(doc, "summary", "") or ""
            normalized.append((doc.title, body, float(score), getattr(doc, "id", "")))
        else:
            title = getattr(item, "title", "") or "Untitled"
            content = getattr(item, "content", "") or ""
            score = float(getattr(item, "score", 0.0) or 0.0)
            rid = getattr(item, "remote_id", "") or ""
            normalized.append((title, content, score, rid))

    lines.append("## Selected passages")
    lines.append("")
    for title, _body, score, _rid in normalized:
        lines.append(f"- **{title}** (score={score:.4f})")
    lines.append("")
    for title, body, score, rid in normalized:
        lines.append("---")
        lines.append("")
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"_id=`{rid or '—'}` · score={score:.4f}_")
        lines.append("")
        lines.append((body or "").strip() or "_(empty)_")
        lines.append("")
    return "\n".join(lines)


def load_pack_for_cursor(
    store: KnowledgeStore | None,
    workspace: Path,
    query: str,
    *,
    limit: int = 6,
    gateway: Any | None = None,
    mission_root: Path | None = None,
    stage: str = "",
) -> dict[str, str]:
    """
    Retrieve KB passages for this mission and write Cursor-consumable context.

    When `gateway` uses WeKnora, retrieval is WeKnora hybrid-search.
    Otherwise falls back to local catalog keyword ranking.

    ``mission_root`` 非空时，额外把同一份内容归档到
    ``<mission_root>/logs/knowledge-pack/<stage>.md``：workspace 会被回收，
    任务目录里的副本才是「这次 agent 拿到了什么知识」的审计依据。
    """
    if gateway is not None:
        hits = gateway.retrieve(query, limit=limit)
        pack = render_knowledge_pack(hits, query=query, target="cursor")
        hit_count = len(hits)
        source = "weknora" if getattr(gateway, "uses_weknora", False) else "local"
    else:
        assert store is not None
        ranked = search_for_mission(store, query, limit=limit)
        pack = render_knowledge_pack(ranked, query=query, target="cursor")
        hit_count = len(ranked)
        source = "local"

    rules_dir = workspace / ".cursor" / "rules"
    sortie_dir = workspace / ".sortie-deck"
    rules_dir.mkdir(parents=True, exist_ok=True)
    sortie_dir.mkdir(parents=True, exist_ok=True)

    mdc = (
        "---\n"
        "description: Sortie Deck mission knowledge pack (auto-loaded)\n"
        "alwaysApply: true\n"
        "---\n\n"
        "When implementing this mission, treat the following knowledge as authoritative "
        "product / API / design context. Cite doc titles when making design choices.\n\n"
        f"_Retrieval source: {source}_\n\n"
        + pack
    )
    rule_path = rules_dir / "sortie-knowledge.mdc"
    pack_path = sortie_dir / "knowledge-pack.md"
    rule_path.write_text(mdc, encoding="utf-8")
    pack_path.write_text(pack, encoding="utf-8")

    archive_rel = ""
    if mission_root is not None:
        try:
            target_dir = Path(mission_root) / "logs" / "knowledge-pack"
            target_dir.mkdir(parents=True, exist_ok=True)
            safe_stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", stage or "pack")[:60] or "pack"
            archive = target_dir / f"{safe_stage}.md"
            archive.write_text(
                f"<!-- mission knowledge pack | stage={stage or '-'} | "
                f"source={source} | hits={hit_count} -->\n\n{mdc}",
                encoding="utf-8",
            )
            archive_rel = str(archive)
        except OSError as exc:
            # 归档是审计增强项，写失败不能影响本阶段执行
            logger.warning("知识包归档失败: %s: %s", mission_root, exc)

    return {
        "rule": str(rule_path.relative_to(workspace)),
        "pack": str(pack_path.relative_to(workspace)),
        "archive": archive_rel,
        "hits": str(hit_count),
        "source": source,
    }
