"""External materials attached to a mission and their prompt context block.

A mission can carry any number of input files (PRD / plan doc / design mock /
data export …). Each material may be *pinned*: pinned materials are the ones the
squad keeps carrying into every downstream prompt — this module owns the single
place where that context block is composed, so the HTTP layer, the orchestrator
and any future consumer agree on the format.
"""

from __future__ import annotations

from collections.abc import Iterable

from sortie_deck.models import ExternalMaterial

# Marker heading that opens the persistent-context section of a brief. Kept in
# one place so re-composition can strip an old section before appending anew.
MATERIALS_HEADER = "【外部资料 · 持续上下文】"

# Per-material excerpt cap folded into the brief (characters).
MAX_EXCERPT_CHARS = 8000

# 超过该长度的资料不再内联，只在 brief 里留「去读哪个文件」的指针。
# 依据：pinned 资料会被带进每个阶段的 prompt，4000 字符约 2-3k tokens，
# 再多就挤占上游产物与代码上下文的预算（大文件由 agent 按需 Read）。
MAX_INLINE_CHARS = 4000

# Rough token estimate: CJK glyphs ≈ 1 token each, latin ≈ 1 token / 4 chars.
def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate for UI display and budgeting."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(0, other) // 4


def pinned_materials(materials: Iterable[ExternalMaterial]) -> list[ExternalMaterial]:
    """Materials that should ride along as persistent context.

    除了「有摘录的 pinned」，也包含「只有原件、没有可读文本的 pinned」——后者
    在 brief 里表现为路径指针，让 agent 知道该去读哪个文件（图片/大文件场景）。
    """
    return [
        m
        for m in materials
        if m.pinned and ((m.text_excerpt or "").strip() or m.stored_path)
    ]


def compose_brief(brief_base: str, materials: Iterable[ExternalMaterial]) -> str:
    """Return the mission brief with pinned materials appended as context.

    ``brief_base`` must be the *raw* user brief (without a materials section),
    so calling this repeatedly stays idempotent.
    """
    base = strip_materials_section(brief_base)
    pinned = pinned_materials(materials)
    if not pinned:
        return base.strip()

    parts = [base.strip(), "", MATERIALS_HEADER]
    for m in pinned:
        excerpt = (m.text_excerpt or "").strip()[:MAX_EXCERPT_CHARS]
        # 长文本 / 无文本可读的原件只给路径：动态加载，避免把上下文预算吃光
        if m.stored_path and (len(excerpt) > MAX_INLINE_CHARS or not excerpt):
            target = m.text_path or m.stored_path
            parts.append(
                f"- {m.name}（{m.kind}）— 内容较长或为二进制，按需读取 `input/{target}`"
            )
            continue
        parts.append(f"- {m.name}（{m.kind}）")
        parts.append(f"```\n{excerpt}\n```")
    return "\n".join(parts).strip()


def strip_materials_section(brief: str) -> str:
    """Drop a previously composed materials section, keeping the raw brief."""
    if MATERIALS_HEADER not in brief:
        return brief
    return brief.split(MATERIALS_HEADER, 1)[0].rstrip()


def pinned_token_estimate(materials: Iterable[ExternalMaterial]) -> int:
    """Total estimated tokens of all pinned material excerpts."""
    return sum(estimate_tokens(m.text_excerpt) for m in pinned_materials(materials))
