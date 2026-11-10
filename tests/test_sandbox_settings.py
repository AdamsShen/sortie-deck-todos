"""沙箱配置透传与随镜像分发的配置文件校验。

回归目标：`TDT_HARNESS_SETTINGS_FILE` 指向真实文件时才传给 SDK；
本地未部署配置时不改变行为；镜像内配置必须开启沙箱免审批。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

from sortie_deck.harness_providers.base import HarnessSession
from sortie_deck.harness_providers.claude_code import ClaudeCodeProvider

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SANDBOX_CONFIG = _REPO_ROOT / "docker" / "claude-sandbox.json"


def _fake_sdk(monkeypatch: Any, captured: dict[str, Any]) -> None:
    class _FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    class _FakeMessage:
        pass

    async def _empty_query(**_kwargs: Any):
        if False:  # pragma: no cover — 只为让它成为异步生成器
            yield _FakeMessage()

    fake_mod = types.ModuleType("claude_agent_sdk")
    fake_types = types.ModuleType("claude_agent_sdk.types")
    fake_mod.ClaudeAgentOptions = _FakeOptions  # type: ignore[attr-defined]
    fake_mod.query = _empty_query  # type: ignore[attr-defined]
    fake_types.AssistantMessage = _FakeMessage  # type: ignore[attr-defined]
    fake_types.ResultMessage = _FakeMessage  # type: ignore[attr-defined]
    fake_types.SystemMessage = _FakeMessage  # type: ignore[attr-defined]
    fake_mod.types = fake_types  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_mod)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types)


def _capture(monkeypatch: Any, tmp_path: Path, settings_file: str) -> dict[str, Any]:
    monkeypatch.setattr("sortie_deck.settings.settings.harness_settings_file", settings_file)
    captured: dict[str, Any] = {}
    _fake_sdk(monkeypatch, captured)
    provider = ClaudeCodeProvider()
    session = HarnessSession(id="hs_test", provider="claude_code")
    asyncio.run(provider._send_via_sdk(session, "x", cwd=str(tmp_path), context_pack={}))
    return captured


def test_settings_file_is_passed_when_exists(monkeypatch: Any, tmp_path: Path) -> None:
    config = tmp_path / "sandbox.json"
    config.write_text("{}", encoding="utf-8")
    assert _capture(monkeypatch, tmp_path, str(config))["settings"] == str(config)


def test_empty_settings_file_keeps_existing_behavior(monkeypatch: Any, tmp_path: Path) -> None:
    assert _capture(monkeypatch, tmp_path, "")["settings"] is None


def test_missing_settings_file_is_ignored(monkeypatch: Any, tmp_path: Path) -> None:
    assert _capture(monkeypatch, tmp_path, str(tmp_path / "missing.json"))["settings"] is None


def test_shipped_sandbox_config_enables_sandbox_without_prompts() -> None:
    data = json.loads(_SANDBOX_CONFIG.read_text(encoding="utf-8"))
    sandbox = data["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["autoAllowBashIfSandboxed"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert "registry.npmjs.org" in sandbox["network"]["allowedDomains"]
    credential_names = {item["name"] for item in sandbox["credentials"]["envVars"]}
    assert "ANTHROPIC_API_KEY" in credential_names
    assert all(item["mode"] == "deny" for item in sandbox["credentials"]["envVars"])
    assert "/usr/local" in sandbox["filesystem"]["denyWrite"]


def test_sandbox_preflight_reports_missing_binary(monkeypatch: Any) -> None:
    from sortie_deck import permissions

    monkeypatch.setattr(
        permissions.shutil,
        "which",
        lambda name: None if name == "socat" else f"/usr/bin/{name}",
    )
    assert permissions.sandbox_preflight() == {"available": False, "missing": ["socat"]}


def test_sandbox_preflight_ok(monkeypatch: Any) -> None:
    from sortie_deck import permissions

    monkeypatch.setattr(permissions.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert permissions.sandbox_preflight() == {"available": True, "missing": []}


def test_sandbox_health_reflects_config_and_binaries(monkeypatch: Any, tmp_path: Path) -> None:
    import importlib

    # `from sortie_deck.api import app` 拿到的是 FastAPI 实例，必须取模块本身
    api_module = importlib.import_module("sortie_deck.api.app")

    config = tmp_path / "sandbox.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("sortie_deck.settings.settings.harness_settings_file", str(config))
    monkeypatch.setattr(
        "sortie_deck.permissions.sandbox_preflight",
        lambda: {"available": False, "missing": ["socat"]},
    )
    state = api_module._sandbox_health()
    assert state["config_present"] is True
    assert state["bwrap"] is True
    assert state["socat"] is False


def test_resolve_sandbox_mode_maps_permission_modes() -> None:
    from sortie_deck.permissions import resolve_sandbox_mode

    assert (
        resolve_sandbox_mode(permissions={"coding_permission_mode": "acceptEdits"})
        == "workspace_write"
    )
    assert (
        resolve_sandbox_mode(permissions={"coding_permission_mode": "bypassPermissions"})
        == "full_access"
    )
    assert resolve_sandbox_mode(permissions={"coding_permission_mode": "default"}) == "read_only"


def test_resolve_sandbox_mode_prefers_explicit_value() -> None:
    from sortie_deck.permissions import resolve_sandbox_mode

    assert resolve_sandbox_mode(permissions={"sandbox_mode": "full_access"}) == "full_access"
    assert resolve_sandbox_mode(permissions={"sandbox_mode": "bogus"}) == "workspace_write"


def test_dsh_maps_public_mode_to_its_vocabulary() -> None:
    from sortie_deck.harness_providers import dsh

    assert dsh._sandbox_mode_from_pack({"sandbox": {"mode": "read_only"}}) == "read-only"
    assert dsh._sandbox_mode_from_pack({"sandbox": {"mode": "full_access"}}) == "danger-full-access"
    assert dsh._sandbox_mode_from_pack({}) is None
    assert dsh._permission_mode("read-only") == "read-only"
    assert dsh._permission_mode("bogus") == "workspace-write"


def test_codex_first_turn_args_include_sandbox_approval_and_network() -> None:
    from sortie_deck.harness_providers import codex

    class _Settings:
        codex_sandbox = "workspace-write"
        codex_ask_for_approval = "never"
        codex_network_access = True

    args = codex._first_turn_args(
        "/usr/local/bin/codex",
        _Settings(),
        {"sandbox": {"mode": "read_only"}},
        ["/tmp/artifacts"],
    )
    assert args[1:5] == ["exec", "--json", "--skip-git-repo-check", "--sandbox"]
    assert args[5] == "read-only"
    assert "--ask-for-approval" in args and "never" in args
    assert "sandbox_workspace_write.network_access=true" in args
    assert "--add-dir" in args


def test_codex_first_turn_args_can_disable_approval_and_network() -> None:
    from sortie_deck.harness_providers import codex

    class _Settings:
        codex_sandbox = "workspace-write"
        codex_ask_for_approval = ""
        codex_network_access = False

    args = codex._first_turn_args("/bin/codex", _Settings(), None, None)
    assert args[5] == "workspace-write"
    assert "--ask-for-approval" not in args
    assert "sandbox_workspace_write.network_access=true" not in args


def _stage_ctx(**overrides: Any) -> Any:
    from sortie_deck.models import RoleId, StageContext

    data: dict[str, Any] = {
        "initiative_id": "ini",
        "thread_id": "t",
        "stage": "eng_implement",
        "role": RoleId.ENG,
        "brief": "x",
        "artifact_dir": "/tmp/arts",
    }
    data.update(overrides)
    return StageContext(**data)


def test_build_prompt_declares_dependency_boundary(tmp_path: Path) -> None:
    from sortie_deck.harness_providers.executor import HarnessStageExecutor

    prompt = HarnessStageExecutor()._build_prompt(_stage_ctx(), tmp_path, {"briefs": {}}, "")
    assert "Dependency boundary" in prompt
    assert "node_modules" in prompt
    assert "/usr/local" in prompt


def test_doc_stage_prompt_skips_dependency_boundary(tmp_path: Path) -> None:
    from sortie_deck.harness_providers.executor import HarnessStageExecutor

    prompt = HarnessStageExecutor()._build_prompt(
        _stage_ctx(stage="product_prd"), tmp_path, {"briefs": {}}, "", doc_stage=True
    )
    assert "Dependency boundary" not in prompt


def test_workspace_listing_excludes_task_local_dependencies(tmp_path: Path) -> None:
    from sortie_deck.plugins.coding import _workspace_file_listing

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.js").write_text("x", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "x.py").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")

    listing = _workspace_file_listing(tmp_path)
    assert "src/app.js" in listing
    assert ".venv" not in listing
    assert "node_modules" not in listing
