"""DSH provider 单测：不打真实 SDK，用假 ``deepseek_harness`` 模块驱动。"""

from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

from sortie_deck.harness_providers import dsh as dsh_mod
from sortie_deck.harness_providers.dsh import DshProvider
from sortie_deck.settings import settings


class _FakeResult:
    def __init__(
        self,
        *,
        session_id: str = "dsh-session-1",
        text: str = "done",
        finish: str = "completed",
        events: list[dict] | None = None,
    ) -> None:
        self.session_id = session_id
        self.final_response = text
        self.finish_reason = finish
        self.events = events or []
        self.notifications: list = []


class _FakeSession:
    def __init__(self, harness: _FakeHarness, session_id: str) -> None:
        self.harness = harness
        self.id = session_id

    def run(self, prompt, on_notification=None):
        self.harness.prompts.append(prompt)
        if on_notification is not None and self.harness.notify_on_run:
            on_notification(_tool_call_notification())
        if self.harness.delay:
            time.sleep(self.harness.delay)
        if self.harness.raise_after_delay and self.harness.closed:
            raise RuntimeError("runtime closed")
        return self.harness.result


class _FakeHarness:
    """最小假实现：记录构造参数、暴露 start/resume/close 行为开关。"""

    delay = 0.0
    raise_after_delay = False
    notify_on_run = True

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.prompts: list[str] = []
        self.result = _FakeResult()
        type(self).instances.append(self)

    def start_session(self, session_id=None):
        if session_id in type(self).existing:
            raise RuntimeError(f'session "{session_id}" already exists')
        type(self).existing.add(session_id)
        return _FakeSession(self, session_id)

    def resume_session(self, session_id):
        if type(self).resume_fails:
            raise RuntimeError("session not found")
        return _FakeSession(self, session_id)

    def close(self) -> None:
        self.closed = True


_FakeHarness.instances = []
_FakeHarness.existing = set()
_FakeHarness.resume_fails = False


def _tool_call_notification() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        method="session.event",
        payload={
            "sessionId": "dsh-session-1",
            "event": {
                "type": "tool/call",
                "data": {
                    "name": "bash",
                    "arguments": '{"command": "npm test", "description": "run tests"}',
                },
            },
        },
    )


def _install_fake(monkeypatch, harness_cls=_FakeHarness) -> None:
    module = types.ModuleType("deepseek_harness")
    module.DeepSeekHarness = harness_cls
    monkeypatch.setitem(sys.modules, "deepseek_harness", module)
    monkeypatch.setattr(dsh_mod, "_sdk_available", lambda: True)


@pytest.fixture
def dsh_settings(tmp_path, monkeypatch):
    """隔离数据目录与 DSH 配置，避免任何测试写到真实 data/。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "dsh_home_root", "")
    monkeypatch.setattr(settings, "dsh_api_key", "sk-test")
    monkeypatch.setattr(settings, "dsh_session_log_upload", False)
    monkeypatch.setattr(settings, "dsh_isolate_skills", True)
    monkeypatch.setattr(settings, "dsh_isolate_home", False)
    monkeypatch.setattr(settings, "dsh_request_timeout_seconds", 0.0)
    monkeypatch.setattr(settings, "coding_timeout_seconds", 30.0)
    monkeypatch.setattr(settings, "dsh_max_tokens", 0)
    monkeypatch.setattr(settings, "dsh_reasoning_effort", "")
    monkeypatch.setattr(settings, "dsh_model", "deepseek-v4-flash")
    monkeypatch.setattr(settings, "dsh_llm_provider", "deepseek-official")
    monkeypatch.setattr(settings, "dsh_profile", "sdk")
    monkeypatch.setattr(settings, "dsh_base_url", None)
    _FakeHarness.instances = []
    _FakeHarness.existing = set()
    _FakeHarness.resume_fails = False
    _FakeHarness.delay = 0.0
    _FakeHarness.raise_after_delay = False
    _FakeHarness.notify_on_run = True
    yield settings


# --- probe --------------------------------------------------------------------


def test_probe_missing_without_sdk(monkeypatch, dsh_settings):
    monkeypatch.setattr(dsh_mod, "_sdk_available", lambda: False)
    assert DshProvider().probe().status == "missing"


def test_probe_requires_key(monkeypatch, dsh_settings):
    monkeypatch.setattr(dsh_mod, "_sdk_available", lambda: True)
    monkeypatch.setattr(settings, "dsh_api_key", "")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    probe = DshProvider().probe()
    assert probe.status == "auth_required"
    assert "DEEPSEEK_API_KEY" in probe.detail


def test_probe_ok(monkeypatch, dsh_settings):
    monkeypatch.setattr(dsh_mod, "_sdk_available", lambda: True)
    probe = DshProvider().probe()
    assert probe.status == "ok"
    assert "deepseek-v4-flash" in probe.detail


def test_key_falls_back_to_env(monkeypatch, dsh_settings):
    monkeypatch.setattr(dsh_mod, "_sdk_available", lambda: True)
    monkeypatch.setattr(settings, "dsh_api_key", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    assert DshProvider().probe().status == "ok"


# --- send ---------------------------------------------------------------------


async def test_send_ok_passes_config_and_cleans_patches(monkeypatch, dsh_settings):
    _install_fake(monkeypatch)
    provider = DshProvider()
    session = await provider.start_session(initiative_id="ini_test", stage="build")
    reply = await provider.send(
        session,
        "do it",
        cwd="/tmp/worktree",
        context_pack={"disallowed_tools": ["Bash"], "mcp_servers": {}},
    )

    assert reply.status == "ok"
    assert reply.text == "done"
    assert reply.native_id == "dsh-session-1"
    assert reply.meta["provider"] == "dsh"
    # 不提供 token 口径：context 留空但键存在，下游读得到
    assert reply.meta["context"] == {}

    harness = _FakeHarness.instances[-1]
    assert harness.closed is True
    assert harness.kwargs["cwd"] == "/tmp/worktree"
    assert harness.kwargs["api_key"] == "sk-test"
    assert harness.kwargs["profile"] == "sdk"
    # 默认安全姿态：base patch 必须在，且带工具禁用 patch
    patches = [str(p) for p in harness.kwargs["patches"]]
    assert any(p.endswith("base.yml") for p in patches)
    assert any(p.endswith("tools-off.yml") for p in patches)
    # 隔离用户级技能根
    assert harness.kwargs["env"]["DSH_AGENTS_HOME"].endswith("/agents")
    # 临时 patch 目录跑完即清
    patch_root = dsh_settings.data_dir / "dsh_home" / "ini_test" / "_patches"
    assert list(patch_root.iterdir()) == []


async def test_send_resumes_existing_native_id(monkeypatch, dsh_settings):
    _install_fake(monkeypatch)
    provider = DshProvider()
    session = await provider.start_session(initiative_id="ini_test", stage="build")
    session.native_id = "dsh-existing"

    reply = await provider.send(session, "second turn", cwd="/tmp/worktree")

    assert reply.status == "ok"
    assert _FakeHarness.instances[-1].prompts == ["second turn"]


async def test_send_falls_back_when_resume_fails(monkeypatch, dsh_settings):
    _install_fake(monkeypatch)
    _FakeHarness.resume_fails = True
    provider = DshProvider()
    session = await provider.start_session(initiative_id="ini_test", stage="build")
    session.native_id = "lost-session"

    reply = await provider.send(session, "go", cwd="/tmp/worktree")

    assert reply.status == "ok"
    # 回退新会话：native_id 由新一轮结果刷新，不再沿用丢失的 id
    assert reply.native_id == "dsh-session-1"


async def test_send_maps_auth_error_to_needs_auth(monkeypatch, dsh_settings):
    _install_fake(monkeypatch)

    class _AuthHarness(_FakeHarness):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.result = _FakeResult(
                text="",
                finish="error",
                events=[
                    {
                        "type": "turn/end",
                        "data": {
                            "turn": 1,
                            "reason": {
                                "kind": "error",
                                "error": {
                                    "message": "Authentication Fails",
                                    "code": "AUTH",
                                    "status": 401,
                                },
                            },
                        },
                    }
                ],
            )

    _install_fake(monkeypatch, _AuthHarness)
    provider = DshProvider()
    session = await provider.start_session(initiative_id="ini_test", stage="build")
    reply = await provider.send(session, "go", cwd="/tmp/worktree")

    assert reply.status == "needs_auth"
    assert reply.meta["auth"] is True
    assert reply.meta["error_code"] == "AUTH"


async def test_send_maps_other_error_to_fail(monkeypatch, dsh_settings):
    _install_fake(monkeypatch)

    class _ErrHarness(_FakeHarness):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.result = _FakeResult(
                text="partial",
                finish="error",
                events=[
                    {
                        "type": "turn/end",
                        "data": {
                            "turn": 1,
                            "reason": {
                                "kind": "error",
                                "error": {"message": "quota", "code": "RATE_LIMIT"},
                            },
                        },
                    }
                ],
            )

    _install_fake(monkeypatch, _ErrHarness)
    provider = DshProvider()
    session = await provider.start_session(initiative_id="ini_test", stage="build")
    reply = await provider.send(session, "go", cwd="/tmp/worktree")

    assert reply.status == "fail"
    assert reply.meta["error_code"] == "RATE_LIMIT"


async def test_send_timeout_kills_runtime(monkeypatch, dsh_settings):
    _install_fake(monkeypatch)
    monkeypatch.setattr(settings, "dsh_request_timeout_seconds", 0.05)
    _FakeHarness.delay = 0.3
    _FakeHarness.raise_after_delay = True

    provider = DshProvider()
    session = await provider.start_session(initiative_id="ini_test", stage="build")
    reply = await provider.send(session, "slow", cwd="/tmp/worktree")

    assert reply.status == "fail"
    assert reply.meta.get("timeout") is True
    assert _FakeHarness.instances[-1].closed is True


async def test_activity_bridge_forwards_tool_calls(monkeypatch, dsh_settings):
    _install_fake(monkeypatch)
    seen: list[tuple[str, str]] = []

    async def on_activity(tool: str, detail: str) -> None:
        seen.append((tool, detail))

    provider = DshProvider()
    session = await provider.start_session(initiative_id="ini_test", stage="build")
    await provider.send(session, "go", cwd="/tmp/worktree", on_activity=on_activity)
    # 回调经 run_coroutine_threadsafe 投递，等事件循环跑一轮
    await asyncio.sleep(0.05)

    assert seen and seen[0][0] == "bash"
    assert seen[0][1] == "npm test"


async def test_activity_callback_failure_does_not_break_turn(monkeypatch, dsh_settings):
    _install_fake(monkeypatch)

    async def on_activity(tool: str, detail: str) -> None:
        raise RuntimeError("sse down")

    provider = DshProvider()
    session = await provider.start_session(initiative_id="ini_test", stage="build")
    reply = await provider.send(session, "go", cwd="/tmp/worktree", on_activity=on_activity)
    await asyncio.sleep(0.05)

    assert reply.status == "ok"


def test_provider_cannot_widen_writable_dirs():
    # 执行器据此不传 extra_dirs（DSH 一个会话只有一个可写根）
    assert DshProvider().supports_extra_write_dirs is False
