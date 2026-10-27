from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from sortie_deck.graph import _coding_perm_from_instruction
from sortie_deck.models import RoleId, StageContext
from sortie_deck.plugins.coding import (
    ClaudeCodeExecutor,
    CursorCliExecutor,
    _looks_like_permission_block,
    _permission_mode,
)
from sortie_deck.plugins.factory import build_default_registry


def test_registry_lists_expected_executors():
    registry = build_default_registry()
    names = registry.names()
    expected = {
        "mock_intake",
        "mock_product",
        "mock_eng",
        "mock_qa",
        "mock_qa_cases",
        "mock_selftest",
        "mock_integrate",
        "mock_handoff",
        "mock_deploy",
        "llm_product",
        "claude_code",
        "cursor_cli",
        "deploy_shell",
    }
    assert expected <= set(names)


@pytest.mark.parametrize(
    "executor_cls",
    [ClaudeCodeExecutor, CursorCliExecutor],
)
def test_coding_executor_soft_fails_without_cli(tmp_path, executor_cls):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    worktrees = tmp_path / "worktrees"
    executor = executor_cls(root, worktrees)
    ctx = StageContext(
        initiative_id="ini_test",
        thread_id="thread_test",
        stage="eng_implement",
        role=RoleId.ENG,
        brief="Build feature X",
        persona="You are eng.",
        artifact_dir=str(tmp_path / "arts"),
    )

    async def _run():
        with patch("sortie_deck.plugins.coding._which", return_value=None):
            return await executor.run(ctx)

    result = asyncio.run(_run())
    assert result.status == "fail"
    assert "not found" in result.message.lower()
    assert "implementation.md" in result.artifacts
    assert "not found" in result.artifacts["implementation.md"].lower()


def test_permission_block_detection():
    assert _looks_like_permission_block(
        "Claude requested permissions to write to /tmp/README.md, but you haven't granted it yet."
    )
    assert _looks_like_permission_block("I've hit a permissions blocker: I can't create any files")
    assert not _looks_like_permission_block("Wrote index.html successfully")


def test_coding_perm_from_instruction_tag():
    assert _coding_perm_from_instruction(None) == {}
    assert _coding_perm_from_instruction("ok") == {}
    assert _coding_perm_from_instruction(
        "go\n[sortie:coding_permission_mode=bypassPermissions]"
    ) == {"coding_permission_mode": "bypassPermissions"}


def test_permission_mode_honors_hitl_bypass(monkeypatch):
    monkeypatch.setattr(
        "sortie_deck.settings.settings.coding_permission_mode",
        "acceptEdits",
    )
    ctx = StageContext(
        initiative_id="ini",
        thread_id="t",
        stage="eng_implement",
        role=RoleId.ENG,
        brief="x",
        artifact_dir="/tmp/arts",
        permissions={"coding_permission_mode": "bypassPermissions"},
    )
    assert _permission_mode(ctx) == "bypassPermissions"


def test_claude_code_returns_needs_hitl_on_permission_block(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    workspace = worktrees / "ini_test"
    workspace.mkdir()
    (workspace / "README.md").write_text("# empty\n", encoding="utf-8")

    executor = ClaudeCodeExecutor(root, worktrees)
    ctx = StageContext(
        initiative_id="ini_test",
        thread_id="thread_test",
        stage="eng_implement",
        role=RoleId.ENG,
        brief="Build a snake game",
        persona="You are eng.",
        artifact_dir=str(tmp_path / "arts"),
        workspace_dir=str(workspace),
        upstream_artifacts={"prd.md": "# PRD\n"},
    )

    async def fake_run(args, cwd, timeout=None, **_kwargs):
        return (
            0,
            "Claude requested permissions to write, but you haven't granted it yet.",
            "",
        )

    async def fake_diff(*_a, **_k):
        return ""

    monkeypatch.setattr(
        "sortie_deck.plugins.coding._which",
        lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
    )
    monkeypatch.setattr("sortie_deck.plugins.coding._run_cmd", fake_run)
    monkeypatch.setattr("sortie_deck.plugins.coding._git_diff", fake_diff)

    result = asyncio.run(executor.run(ctx))
    assert result.status == "needs_hitl"
    assert result.meta.get("permission_block") is True
    assert result.meta.get("retry_permission_mode") == "bypassPermissions"
