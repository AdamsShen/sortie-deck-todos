"""工具箱条目 ↔ WeKnora 镜像（方案 A：混合检索 + rerank 复用 WeKnora 现成能力）。

设计（docs/plans/2026-10-20-tool-search-meta-tool.md）：
- **写入**：ToolkitStore 增删改时把条目镜像为 WeKnora manual knowledge
    * title 锚点 = ``toolkit/{kind}/{name}#{item_id}`` —— 检索命中后映射回条目
    * content = name/summary/tags/capabilities/category + endpoint/command/body
      （``env`` 永不写入镜像，密钥不进知识库）
- **检索**：``WeKnoraClient.hybrid_search``（向量 + BM25 → RRF 融合 →
  bge-reranker-v2-m3 重排 → 复合评分），复合分数直接作为 recall 得分；
  租户未配 rerank 模型时 WeKnora 自动降级为 RRF 检索序
- **降级**：backend 未开 / WeKnora 不可用 / 超时 → 调用方回退本地三层加权打分
  （loadout._score），任何镜像异常都不影响条目主流程

全量重建走 ``sync_all``（管理端点 POST /api/toolkit/reindex）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sortie_deck.weknora import WeKnoraClient

if TYPE_CHECKING:  # 仅类型引用，避免循环导入
    from sortie_deck.toolkit import ToolkitItem, ToolkitStore

logger = logging.getLogger(__name__)

# 镜像 title 的固定前缀：检索命中时据此识别工具箱条目
TITLE_PREFIX = "toolkit"
# skill 正文镜像上限（与 loadout.SKILL_BODY_FLOAT_LIMIT 同量级，防超大正文）
_CONTENT_BODY_LIMIT = 4000
# 镜像同步的网络超时（检索走 hybrid_search 自身的 60s，这里只管写入）
_MIRROR_TIMEOUT = 10.0


def item_title(item: ToolkitItem) -> str:
    """条目的镜像 title 锚点：检索命中 → 截取 ``#`` 后的 item_id → 回查条目。"""
    return f"{TITLE_PREFIX}/{item.kind}/{item.name}#{item.id}"


def item_content(item: ToolkitItem) -> str:
    """条目的镜像正文：供向量/BM25/重排三层匹配的全部可检索文本。

    注意：``env``（可能含密钥）与脚本源码永不写入镜像。
    """
    lines = [f"# {item.kind}/{item.name}"]
    if item.summary:
        lines += ["", item.summary]
    meta: list[str] = []
    if item.category:
        meta.append(f"分类: {item.category}")
    if item.tags:
        meta.append(f"标签: {' '.join(item.tags)}")
    if item.capabilities:
        meta.append(f"能力: {' '.join(item.capabilities)}")
    if item.kind == "tool" and item.endpoint:
        meta.append(f"端点: {item.endpoint}")
    if item.kind == "mcp" and (item.command or item.url):
        meta.append(f"连接: {item.url or item.command}")
    if item.kind == "skill" and item.scripts:
        meta.append(f"脚本: {', '.join(s.path for s in item.scripts)}")
    if meta:
        lines += ["", "\n".join(meta)]
    body = (item.body or "").strip()
    if body:
        if len(body) > _CONTENT_BODY_LIMIT:
            body = body[:_CONTENT_BODY_LIMIT] + " …[截断]"
        lines += ["", body]
    return "\n".join(lines)


def mirror_available() -> bool:
    """镜像检索是否可用：backend=weknora 且 KB id 已配置。"""
    from sortie_deck.settings import settings

    backend = (getattr(settings, "toolkit_recall_backend", "") or "").strip().lower()
    return backend == "weknora" and bool(getattr(settings, "toolkit_weknora_kb_id", None))


def build_client() -> WeKnoraClient | None:
    """按 settings 构建镜像客户端；backend 未开 / 缺 base_url 时返回 None。"""
    from sortie_deck.settings import settings

    if not mirror_available():
        return None
    base_url = getattr(settings, "weknora_base_url", None)
    if not base_url:
        return None
    return WeKnoraClient(
        base_url,
        getattr(settings, "weknora_api_key", None),
        timeout=_MIRROR_TIMEOUT,
    )


def _kb_id() -> str:
    from sortie_deck.settings import settings

    return str(getattr(settings, "toolkit_weknora_kb_id", "") or "")


def _iter_knowledge(client: WeKnoraClient, kb_id: str) -> list[Any]:
    """分页拉全 KB 的 knowledge 列表（镜像规模小，分页到尽头即可）。"""
    out: list[Any] = []
    page = 1
    while True:
        rows = client.list_knowledge(kb_id, page=page, page_size=100)
        out.extend(rows)
        if len(rows) < 100:
            return out
        page += 1


def _find_mirror_id(client: WeKnoraClient, kb_id: str, title: str) -> str | None:
    for row in _iter_knowledge(client, kb_id):
        if row.title == title:
            return row.id
    return None


def mirror_upsert(item: ToolkitItem) -> str:
    """单个条目镜像 upsert：同 title 存在则删旧建新（WeKnora 无更新接口）。

    返回 created / updated / skipped（backend 未开）。抛 WeKnoraError 由调用方兜底。
    """
    client = build_client()
    kb = _kb_id()
    if client is None or not kb:
        return "skipped"
    title = item_title(item)
    old_id = _find_mirror_id(client, kb, title)
    if old_id:
        client.delete_knowledge(old_id)
    client.create_manual(kb, title=title, content=item_content(item))
    return "updated" if old_id else "created"


def mirror_remove(item_id: str) -> str:
    """按 item_id 后缀定位并删除镜像（条目已删，无法拼完整 title，扫后缀匹配）。"""
    client = build_client()
    kb = _kb_id()
    if client is None or not kb:
        return "skipped"
    suffix = f"#{item_id}"
    removed = 0
    for row in _iter_knowledge(client, kb):
        if row.title.endswith(suffix):
            client.delete_knowledge(row.id)
            removed += 1
    return f"removed:{removed}"


def sync_all(store: ToolkitStore) -> dict[str, int]:
    """全量重建镜像：本地为准，多退少补。管理端点 POST /api/toolkit/reindex 使用。"""
    client = build_client()
    kb = _kb_id()
    if client is None or not kb:
        return {"skipped": 1, "created": 0, "updated": 0, "removed": 0}
    by_title = {
        row.title: row.id
        for row in _iter_knowledge(client, kb)
        if row.title.startswith(TITLE_PREFIX + "/")
    }
    stats = {"skipped": 0, "created": 0, "updated": 0, "removed": 0}
    for item in store.list():
        title = item_title(item)
        old_id = by_title.pop(title, None)
        if old_id is not None:
            client.delete_knowledge(old_id)
            stats["updated"] += 1
        else:
            stats["created"] += 1
        client.create_manual(kb, title=title, content=item_content(item))
    for old_id in by_title.values():
        # 库里有、本地已删的残留镜像
        client.delete_knowledge(old_id)
        stats["removed"] += 1
    return stats


def weknora_recall(
    store: ToolkitStore,
    query: str,
    *,
    kinds: list[str] | None = None,
    allowed_ids: set[str] | None = None,
    limit: int = 5,
) -> list[tuple[ToolkitItem, float]] | None:
    """混合检索 + rerank 召回：WeKnora hits → title 锚点映射回条目。

    返回 None 表示后端未配置（调用方应回退本地打分）；WeKnora 网络/协议异常
    直接抛出（WeKnoraError），由 recall() 统一捕获降级。
    """
    client = build_client()
    kb = _kb_id()
    if client is None or not kb:
        return None
    # 多取一倍留出过滤损耗（停用/类型/白名单过滤后仍够 limit 条）
    hits = client.hybrid_search(kb, (query or "").strip(), match_count=max(limit * 2, limit))
    rows: list[tuple[ToolkitItem, float]] = []
    for hit in hits:
        title = hit.knowledge_title or ""
        if not title.startswith(TITLE_PREFIX + "/") or "#" not in title:
            continue
        item_id = title.rsplit("#", 1)[-1]
        if not item_id:
            continue
        try:
            item = store.get(item_id)
        except KeyError:
            continue  # 镜像滞后：条目已删
        if not item.enabled:
            continue
        if kinds and item.kind not in kinds:
            continue
        if allowed_ids is not None and item.id not in allowed_ids:
            continue
        # WeKnora 复合评分（rerank 模型分 + 检索分加权）已归一到 0-1，直接沿用
        rows.append((item, min(1.0, max(0.0, hit.score))))
        if len(rows) >= limit:
            break
    return rows
