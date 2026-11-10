"""统一权限解析与事后失败归因。

回归 2026-11-09 任务 ini_88465249e006：HITL 提权只在旧 CLI 生效、
环境阻塞被当作代码缺陷，导致 8 轮开发 + 4 轮 QA 的空转。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from sortie_deck.harness_providers.executor import classify_stage_failure
from sortie_deck.models import StageFailureKind
from sortie_deck.permissions import (
    configured_deny_rules,
    parse_permission_override,
    permission_preflight,
    resolve_permission_mode,
)


def test_parse_permission_override_accepts_sortie_and_legacy_tags() -> None:
    assert parse_permission_override("go\n[sortie:coding_permission_mode=bypassPermissions]") == (
        "bypassPermissions"
    )
    assert parse_permission_override("[rally:coding_permission_mode=acceptEdits]") == "acceptEdits"


def test_parse_permission_override_rejects_unknown_mode() -> None:
    assert parse_permission_override(None) is None
    assert parse_permission_override("[sortie:coding_permission_mode=rm -rf /]") is None
    assert parse_permission_override("bypassPermissions") is None


def test_resolve_permission_mode_prefers_task_override() -> None:
    assert (
        resolve_permission_mode(
            permissions={"coding_permission_mode": "bypassPermissions"},
            human_instruction="[sortie:coding_permission_mode=acceptEdits]",
            default="acceptEdits",
        )
        == "bypassPermissions"
    )


def test_resolve_permission_mode_reads_instruction_tag_and_validates_default() -> None:
    assert (
        resolve_permission_mode(
            permissions={},
            human_instruction="retry [sortie:coding_permission_mode=dontAsk]",
            default="acceptEdits",
        )
        == "dontAsk"
    )
    assert resolve_permission_mode(default="not-a-mode") == "acceptEdits"
    assert resolve_permission_mode(default="") == "acceptEdits"


def test_configured_deny_rules_parses_and_ignores_empty() -> None:
    assert configured_deny_rules("") == []
    assert configured_deny_rules("Bash(rm -rf /*), Bash(curl *)") == [
        "Bash(rm -rf /*)",
        "Bash(curl *)",
    ]


def test_permission_preflight_reports_without_raising() -> None:
    assert asyncio.run(permission_preflight([sys.executable, "-c", "pass"])) is True
    assert (
        asyncio.run(permission_preflight([sys.executable, "-c", "import sys; sys.exit(1)"]))
        is False
    )
    assert asyncio.run(permission_preflight(["/definitely/not/a/binary"])) is False


def _classify(tmp_path: Path, *, reply_meta: dict | None = None):
    return classify_stage_failure(
        reply_meta=reply_meta,
        artifacts={},
        workspace=tmp_path,
        artifact_dir=None,
    )


def test_classify_selftest_blocked(tmp_path: Path) -> None:
    (tmp_path / "selftest_log.json").write_text(
        json.dumps([{"status": "blocked", "tail": "This command requires approval"}]),
        encoding="utf-8",
    )
    kind, reason = _classify(tmp_path)
    assert kind is StageFailureKind.ENVIRONMENT_BLOCKED
    assert "自测" in reason


def test_classify_qa_not_a_code_defect(tmp_path: Path) -> None:
    (tmp_path / ".upstream").mkdir()
    (tmp_path / ".upstream" / "test_report.json").write_text(
        json.dumps(
            {
                "verdict": {
                    "status": "blocked",
                    "not_a_code_defect": True,
                    "release_decision": "do_not_release",
                }
            }
        ),
        encoding="utf-8",
    )
    kind, reason = _classify(tmp_path)
    assert kind is StageFailureKind.ENVIRONMENT_BLOCKED
    assert "QA" in reason


def test_classify_real_code_defect_is_not_environment(tmp_path: Path) -> None:
    (tmp_path / "test_report.json").write_text(
        json.dumps({"verdict": {"status": "failed", "release_decision": "do_not_release"}}),
        encoding="utf-8",
    )
    assert _classify(tmp_path) == (None, "")


def test_classify_provider_markers_take_priority(tmp_path: Path) -> None:
    assert _classify(tmp_path, reply_meta={"permission_block": True})[0] is (
        StageFailureKind.ENVIRONMENT_BLOCKED
    )
    assert _classify(tmp_path, reply_meta={"auth_block": True})[0] is (
        StageFailureKind.AUTH_BLOCKED
    )


def test_classify_ignores_malformed_artifacts(tmp_path: Path) -> None:
    (tmp_path / "selftest_log.json").write_text("not json", encoding="utf-8")
    assert _classify(tmp_path) == (None, "")
