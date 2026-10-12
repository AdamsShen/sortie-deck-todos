from __future__ import annotations

from pathlib import Path

from sortie_deck.harness_providers.executor import HarnessStageExecutor
from sortie_deck.plugins.coding import ClaudeCodeExecutor, CursorCliExecutor
from sortie_deck.plugins.deploy import DeployShellExecutor, LlmProductExecutor
from sortie_deck.plugins.entrypoints import load_entry_point_executors
from sortie_deck.plugins.mock import (
    MockCollaborativeExecutor,
    MockDeployExecutor,
    MockDesignExecutor,
    MockEngExecutor,
    MockHandoffExecutor,
    MockIntakeExecutor,
    MockIntegrateExecutor,
    MockProductExecutor,
    MockQaCasesExecutor,
    MockQaExecutor,
    MockSelfTestExecutor,
)
from sortie_deck.registry import ExecutorRegistry


def build_default_registry(
    repo_root: Path | None = None,
    worktrees_root: Path | None = None,
) -> ExecutorRegistry:
    root = repo_root or Path(__file__).resolve().parents[3]
    worktrees = worktrees_root or (root / "data" / "worktrees")
    registry = ExecutorRegistry()
    for executor in (
        MockIntakeExecutor(),
        MockProductExecutor(),
        MockDesignExecutor(),
        MockEngExecutor(),
        MockQaExecutor(),
        MockQaCasesExecutor(),
        MockSelfTestExecutor(),
        MockIntegrateExecutor(),
        MockHandoffExecutor(),
        MockDeployExecutor(),
        MockCollaborativeExecutor(),
        LlmProductExecutor(),
        ClaudeCodeExecutor(root, worktrees),
        CursorCliExecutor(root, worktrees),
        HarnessStageExecutor(),
        DeployShellExecutor(),
    ):
        registry.register(executor)
    load_entry_point_executors(registry)
    return registry
