"""Agent harness providers — system-wide "embed a harness" capability.

Public API::

    from sortie_deck.harness_providers import (
        get_harness_registry, build_harness_registry, is_valid_harness,
        HarnessStageExecutor, HARNESS_NAMES, HARNESS_LABELS,
    )
"""

from __future__ import annotations

from sortie_deck.harness_providers.base import (
    HARNESS_LABELS,
    HARNESS_NAMES,
    HarnessProvider,
    HarnessRegistry,
    HarnessReply,
    HarnessSession,
    ProbeStatus,
    build_harness_registry,
    get_harness_registry,
    is_valid_harness,
    reset_harness_registry,
)
from sortie_deck.harness_providers.executor import HarnessStageExecutor

__all__ = [
    "HARNESS_LABELS",
    "HARNESS_NAMES",
    "HarnessProvider",
    "HarnessRegistry",
    "HarnessReply",
    "HarnessSession",
    "HarnessStageExecutor",
    "ProbeStatus",
    "build_harness_registry",
    "get_harness_registry",
    "is_valid_harness",
    "reset_harness_registry",
]
