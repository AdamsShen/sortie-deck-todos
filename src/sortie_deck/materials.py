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

# Rough token estimate: CJK glyphs ≈ 1 token each, latin ≈ 1 token / 4 chars.
def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate for UI display and budgeting."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(0, other) // 4


def pinned_materials(materials: Iterable[ExternalMaterial]) -> list[ExternalMaterial]:
    """Materials that should ride along as persistent context."""
    return [m for m in materials if m.pinned and (m.text_excerpt or "").strip()]


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
