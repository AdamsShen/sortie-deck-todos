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
    MockTechDesignExecutor,
)
from sortie_deck.registry import ExecutorRegistry


def build_default_registry(
    repo_root: Path | None = None,
    worktrees_root: Path | None = None,
) -> ExecutorRegistry:
    root = repo_root or Path(__file__).resolve().parents[3]
    # worktrees_root 实为 missions 根（data/missions），任务沙盒在 <id>/workspace
    worktrees = worktrees_root or (root / "data" / "missions")
    registry = ExecutorRegistry()
    for executor in (
        MockIntakeExecutor(),
        MockProductExecutor(),
        MockTechDesignExecutor(),
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
