"""Role registry — opens the previously closed role set.

Before this module the set of roles was a compile-time closed set (``RoleId`` in
``models.py`` plus the ``AgentSlot`` Literal in ``agents.py``). This registry
gives a single lookup point for:

* built-in roles (derived from :class:`~sortie_deck.models.RoleId`),
* persona-backed roles (``packages/templates/personas/*.md``),
* custom / forge-created roles persisted to ``data/roles.json``.

Callers (``graph``, ``pipeline_llm``, ``mission_briefing``, ``agent_forge``, the
API) should ask the registry instead of hard-coding role names.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from sortie_deck.models import CODING_ROLE_VALUES, RoleId, utc_now
from sortie_deck.templates import list_persona_roles, load_agent_contract

RoleSource = Literal["builtin", "persona", "custom", "forge"]

# Tokens ignored by the semantic-similarity heuristic
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "with",
        "role",
        "agent",
        "specialist",
        "engineer",
        "manager",
    }
)

_DEFAULT_TITLES: dict[str, str] = {
    "product": "Product Manager",
    "design": "Product Designer",
    "eng": "Software Engineer",
    "eng_ios": "iOS Engineer",
    "eng_android": "Android Engineer",
    "eng_web": "Web Frontend Engineer",
    "eng_backend": "Backend Engineer",
    "eng_agent": "Agent Systems Engineer",
    "qa": "QA Engineer",
    "deploy": "Release Engineer",
    "ops": "Operations",
}


def normalize_role_key(text: str) -> str:
    """Filesystem-safe role key derived from free text.

    ASCII text is slugified to ``snake_case``; non-ASCII input (e.g. ``育儿``)
    falls back to a stable short hash so the key stays usable as a filename.
    """
    raw = (text or "").strip().lower()
    ascii_part = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if ascii_part:
        return ascii_part[:64]
    if not raw:
        return ""
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"role_{digest}"


def _tokens(text: str) -> set[str]:
    parts = re.split(r"[^0-9a-z\u4e00-\u9fff]+", (text or "").lower())
    return {p for p in parts if p and p not in _STOPWORDS}


def _similarity(a: RoleSpec, b: RoleSpec) -> float:
    """Cheap Jaccard over normalized tokens plus an exact-key shortcut."""
    if a.role_key == b.role_key:
        return 1.0
    ta = _tokens(f"{a.role_key} {a.title} {a.summary} {' '.join(a.tags)}")
    tb = _tokens(f"{b.role_key} {b.title} {b.summary} {' '.join(b.tags)}")
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class RoleSpec(BaseModel):
    """One registrable role."""

    role_key: str
    title: str = ""
    title_en: str = ""
    summary: str = ""
    source: RoleSource = "custom"
    # Runs a coding executor (Claude Code / Cursor) rather than a mock/LLM agent
    coding: bool = False
    # Optional explicit executor name; None → resolved by graph defaults
    executor: str | None = None
    handoff_targets: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


def _builtin_specs() -> dict[str, RoleSpec]:
    out: dict[str, RoleSpec] = {}
    for member in RoleId:
        key = member.value
        contract = load_agent_contract(key)
        out[key] = RoleSpec(
            role_key=key,
            title=(contract.title if contract else "") or _DEFAULT_TITLES.get(key, key),
            summary="",
            source="builtin",
            coding=key in CODING_ROLE_VALUES,
            handoff_targets=list(contract.handoff_targets) if contract else [],
            tags=[key],
        )
    return out


def _persona_specs() -> dict[str, RoleSpec]:
    out: dict[str, RoleSpec] = {}
    for key in list_persona_roles():
        contract = load_agent_contract(key)
        out[key] = RoleSpec(
            role_key=key,
            title=(contract.title if contract else "") or _DEFAULT_TITLES.get(key, key),
            source="persona",
            coding=key in CODING_ROLE_VALUES,
            handoff_targets=list(contract.handoff_targets) if contract else [],
            tags=[key],
        )
    return out


class RoleRegistry:
    """Merged read view (builtin ∪ persona ∪ custom) with JSON persistence."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Merged-view cache: building it touches ~20 persona files, too costly to
        # repeat on every stage execution.
        self._cache: dict[str, RoleSpec] | None = None
        if not self.path.exists():
            self._write([])

    def _invalidate(self) -> None:
        self._cache = None

    # --- persistence ---------------------------------------------------

    def _read(self) -> list[RoleSpec]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [RoleSpec.model_validate(x) for x in raw]

    def _write(self, rows: list[RoleSpec]) -> None:
        self.path.write_text(
            json.dumps([r.model_dump(mode="json") for r in rows], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # --- merged view ---------------------------------------------------

    def _merged(self) -> dict[str, RoleSpec]:
        if self._cache is None:
            merged = _builtin_specs()
            for key, spec in _persona_specs().items():
                if key not in merged:
                    merged[key] = spec
            for spec in self._read():
                merged[spec.role_key] = spec
            self._cache = merged
        return self._cache

    def list(self, *, q: str | None = None, source: str | None = None) -> list[RoleSpec]:
        specs = list(self._merged().values())
        if source:
            specs = [s for s in specs if s.source == source]
        if q:
            needle = q.strip().lower()
            specs = [
                s
                for s in specs
                if needle in f"{s.role_key} {s.title} {s.summary} {' '.join(s.tags)}".lower()
            ]
        return sorted(specs, key=lambda s: (s.source != "builtin", s.role_key))

    def keys(self) -> set[str]:
        return set(self._merged())

    def maybe(self, role_key: str) -> RoleSpec | None:
        return self._merged().get(role_key)

    def get(self, role_key: str) -> RoleSpec:
        spec = self.maybe(role_key)
        if spec is None:
            raise KeyError(role_key)
        return spec

    def is_known(self, role_key: str) -> bool:
        return role_key in self._merged()

    def known_role_keys(self) -> list[str]:
        return sorted(self._merged())

    def is_coding(self, role_key: str) -> bool:
        spec = self.maybe(role_key)
        if spec is not None:
            return spec.coding
        return role_key in CODING_ROLE_VALUES

    def executor_hint(self, role_key: str) -> str | None:
        spec = self.maybe(role_key)
        return spec.executor if spec else None

    def handoff_targets(self, role_key: str) -> list[str]:
        contract = load_agent_contract(role_key)
        if contract is not None:
            return list(contract.handoff_targets)
        spec = self.maybe(role_key)
        return list(spec.handoff_targets) if spec else []

    def find_similar(
        self,
        *,
        title: str,
        summary: str = "",
        tags: list[str] | None = None,
        threshold: float = 0.34,
    ) -> list[tuple[RoleSpec, float]]:
        """Rank existing roles by semantic similarity (forge de-duplication)."""
        probe = RoleSpec(role_key=normalize_role_key(title), title=title, summary=summary, tags=tags or [])
        scored = [(_similarity(probe, spec), spec) for spec in self._merged().values()]
        hits = [(spec, score) for score, spec in scored if score >= threshold]
        return sorted(hits, key=lambda pair: pair[1], reverse=True)

    # --- writes --------------------------------------------------------

    def register(self, spec: RoleSpec, *, overwrite: bool = False) -> RoleSpec:
        """Persist a custom/forge role. Built-in keys are protected unless overwriting."""
        key = normalize_role_key(spec.role_key) or spec.role_key
        if not key:
            raise ValueError("role_key is required")
        spec = spec.model_copy(update={"role_key": key, "updated_at": utc_now().isoformat()})
        with self._lock:
            rows = self._read()
            builtin = _builtin_specs()
            builtin.update(_persona_specs())
            for i, row in enumerate(rows):
                if row.role_key == key:
                    rows[i] = spec
                    self._write(rows)
                    self._invalidate()
                    return spec
            if key in builtin and not overwrite:
                raise ValueError(f"role `{key}` is built in — pass overwrite=True to shadow it")
            rows.append(spec)
            self._write(rows)
            self._invalidate()
            return spec

    def delete(self, role_key: str) -> None:
        with self._lock:
            rows = self._read()
            keep = [r for r in rows if r.role_key != role_key]
            if len(keep) == len(rows):
                raise KeyError(role_key)
            self._write(keep)
            self._invalidate()


_REGISTRY: RoleRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_role_registry() -> RoleRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                from sortie_deck.settings import settings

                _REGISTRY = RoleRegistry(settings.data_dir / "roles.json")
    return _REGISTRY


def reset_role_registry() -> None:
    global _REGISTRY
    _REGISTRY = None
