"""DSH 沙箱可用性探测 / 自动回退单测：全程打桩 SDK，不启动真实运行时。"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from sortie_deck.harness_providers import dsh as dsh_mod
from sortie_deck.harness_providers.dsh import DshProvider
from sortie_deck.settings import settings


class _Result:
    def __init__(self, *, text="ok", events=None, session_id="probe-session", finish="completed"):
        self.final_response = text
        self.events = events or []
        self.session_id = session_id
        self.finish_reason = finish
        self.notifications: list = []


class _Session:
    def __init__(self, harness: _Harness, session_id: str) -> None:
        self.harness = harness
        self.id = session_id

    def run(self, prompt, on_notification=None):
        self.harness.prompts.append(prompt)
        if self.harness.is_probe:
            if type(self.harness).probe_error is not None:
                raise type(self.harness).probe_error
            return _Result(
                text=type(self.harness).probe_text,
                events=list(type(self.harness).probe_events),
                session_id=self.id,
            )
        return _Result(text="business done", session_id="business-session")


class _Harness:
    """按 dsh_home 是否含 _probe 区分探测轮与业务轮，记录全部构造参数。"""

    instances: list[_Harness] = []
    probe_text = "ok"
    probe_events: list[dict] = []
    probe_error: BaseException | None = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.is_probe = "/_probe/" in str(kwargs.get("dsh_home") or "")
        self.closed = False
        self.prompts: list[str] = []
        self.session_ids: list[str] = []
        type(self).instances.append(self)

    def start_session(self, session_id=None):
        self.session_ids.append(session_id)
        return _Session(self, session_id)

    def resume_session(self, session_id):
        return _Session(self, session_id)

    def close(self) -> None:
        self.closed = True


_UNAVAILABLE_EVENTS = [
    {
        "type": "tool/result",
        "data": {"stderr": "SANDBOX_UNAVAILABLE: no usable backend (runner missing)"},
    }
]


def _install(monkeypatch) -> None:
    module = types.ModuleType("deepseek_harness")
    module.DeepSeekHarness = _Harness
    monkeypatch.setitem(sys.modules, "deepseek_harness", module)
    monkeypatch.setattr(dsh_mod, "_sdk_available", lambda: True)


@pytest.fixture
def sandbox_settings(tmp_path, monkeypatch):
    """隔离数据目录、沙箱配置与全局探测缓存。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "dsh_home_root", "")
    monkeypatch.setattr(settings, "dsh_api_key", "sk-test")
    monkeypatch.setattr(settings, "dsh_permission_mode", "workspace-write")
    monkeypatch.setattr(settings, "dsh_request_timeout_seconds", 0.0)
    monkeypatch.setattr(settings, "coding_timeout_seconds", 30.0)
    monkeypatch.setattr(settings, "dsh_sandbox_auto_fallback", True)
    monkeypatch.setattr(settings, "dsh_sandbox_probe_ttl_seconds", 600.0)
    monkeypatch.setattr(settings, "dsh_sandbox_probe_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "dsh_sandbox_unavailable_hints", "")
    monkeypatch.setattr(settings, "dsh_isolate_home", False)
    monkeypatch.setattr(settings, "dsh_session_log_upload", False)
    monkeypatch.setattr(settings, "dsh_isolate_skills", True)
    _Harness.instances = []
    _Harness.probe_text = "ok"
    _Harness.probe_events = []
    _Harness.probe_error = None
    dsh_mod.reset_sandbox_probe_cache()
    yield settings
    dsh_mod.reset_sandbox_probe_cache()


async def _send(context_pack=None, prompt="business prompt"):
    provider = DshProvider()
    session = await provider.start_session(initiative_id="ini_sandbox", stage="qa_verify")
    return await provider.send(
        session,
        prompt,
        cwd="/tmp",
        context_pack=context_pack or {},
    )


def _business() -> _Harness:
    return next(h for h in _Harness.instances if not h.is_probe)


def _probes() -> list[_Harness]:
    return [h for h in _Harness.instances if h.is_probe]


# --- 开关与零探测语义 ----------------------------------------------------------


async def test_switch_off_skips_probe(monkeypatch, sandbox_settings):
    _install(monkeypatch)
    monkeypatch.setattr(settings, "dsh_sandbox_auto_fallback", False)
    _Harness.probe_events = _UNAVAILABLE_EVENTS

    reply = await _send()

    assert reply.status == "ok"
    assert _probes() == []
    assert _business().kwargs["env"]["DSH_PERMISSION_MODE"] == "workspace-write"
    assert "permission_probe" not in reply.meta
    assert not reply.meta.get("permission_fallback")


async def test_explicit_full_access_skips_probe(monkeypatch, sandbox_settings):
    _install(monkeypatch)
    _Harness.probe_events = _UNAVAILABLE_EVENTS

    reply = await _send({"sandbox": {"mode": "full_access"}})

    assert reply.status == "ok"
    assert _probes() == []
    assert _business().kwargs["env"]["DSH_PERMISSION_MODE"] == "danger-full-access"
    assert reply.meta["permission_probe"] == "skipped"


# --- 探测结果 ------------------------------------------------------------------


async def test_sandbox_available_keeps_mode(monkeypatch, sandbox_settings):
    _install(monkeypatch)

    reply = await _send()

    assert reply.status == "ok"
    assert len(_probes()) == 1
    assert _business().kwargs["env"]["DSH_PERMISSION_MODE"] == "workspace-write"
    assert reply.meta["permission_probe"] == "run"
    assert reply.meta["effective_mode"] == "workspace-write"
    assert not reply.meta.get("permission_fallback")


async def test_sandbox_unavailable_falls_back(monkeypatch, sandbox_settings, caplog):
    _install(monkeypatch)
    _Harness.probe_events = _UNAVAILABLE_EVENTS

    with caplog.at_level(logging.WARNING, logger="sortie_deck.harness_providers.dsh"):
        reply = await _send()

    assert reply.status == "ok"
    assert _business().kwargs["env"]["DSH_PERMISSION_MODE"] == "danger-full-access"
    assert reply.meta["permission_fallback"] is True
    assert reply.meta["effective_mode"] == "danger-full-access"
    assert reply.meta["probe_reason"] == "sandbox_unavailable"
    assert any("自动回退到 danger-full-access" in r.message for r in caplog.records)
    assert _probes()[0].closed is True


async def test_custom_hints(monkeypatch, sandbox_settings):
    _install(monkeypatch)
    monkeypatch.setattr(settings, "dsh_sandbox_unavailable_hints", "自定义阻断,another-hint")
    _Harness.probe_text = "命令失败：自定义阻断"

    reply = await _send()

    assert reply.meta["permission_fallback"] is True
    assert reply.meta["probe_reason"] == "自定义阻断"


async def test_probe_exception_is_conservative(monkeypatch, sandbox_settings, caplog):
    _install(monkeypatch)
    _Harness.probe_error = RuntimeError("spawn failed")

    with caplog.at_level(logging.WARNING, logger="sortie_deck.harness_providers.dsh"):
        reply = await _send()

    assert reply.status == "ok"
    assert _business().kwargs["env"]["DSH_PERMISSION_MODE"] == "workspace-write"
    assert not reply.meta.get("permission_fallback")
    assert any("保守保持原档位" in r.message for r in caplog.records)


async def test_probe_link_error_is_conservative(monkeypatch, sandbox_settings, caplog):
    _install(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("cache key failed")

    monkeypatch.setattr(dsh_mod, "_probe_key", _boom)
    with caplog.at_level(logging.WARNING, logger="sortie_deck.harness_providers.dsh"):
        reply = await _send()

    assert reply.status == "ok"
    assert _probes() == []
    assert reply.meta["permission_probe"] == "error"
    assert _business().kwargs["env"]["DSH_PERMISSION_MODE"] == "workspace-write"


# --- 缓存 ----------------------------------------------------------------------


async def test_probe_result_cached(monkeypatch, sandbox_settings):
    _install(monkeypatch)
    _Harness.probe_events = _UNAVAILABLE_EVENTS

    first = await _send()
    second = await _send()

    assert len(_probes()) == 1
    assert first.meta["permission_probe"] == "run"
    assert second.meta["permission_probe"] == "cached"
    assert second.meta["probe_cached"] is True
    assert second.meta["permission_fallback"] is True


async def test_ttl_zero_reprobes(monkeypatch, sandbox_settings):
    _install(monkeypatch)
    monkeypatch.setattr(settings, "dsh_sandbox_probe_ttl_seconds", 0.0)
    _Harness.probe_events = _UNAVAILABLE_EVENTS

    await _send()
    await _send()

    assert len(_probes()) == 2


async def test_cache_isolated_by_mode(monkeypatch, sandbox_settings):
    _install(monkeypatch)
    _Harness.probe_events = _UNAVAILABLE_EVENTS

    await _send()
    await _send({"sandbox": {"mode": "read_only"}})

    assert len({p.kwargs["env"]["DSH_PERMISSION_MODE"] for p in _probes()}) == 2


# --- 探测轮隔离与清理 ----------------------------------------------------------


async def test_probe_is_isolated_and_cleaned(monkeypatch, sandbox_settings, tmp_path):
    _install(monkeypatch)
    _Harness.probe_events = _UNAVAILABLE_EVENTS

    await _send({"mcp_servers": {"poc": {"type": "stdio", "command": "python"}}})

    probe = _probes()[0]
    business = _business()
    assert "_probe" in probe.kwargs["dsh_home"]
    assert probe.kwargs["patches"] != business.kwargs["patches"]
    assert all(p.endswith("base.yml") for p in probe.kwargs["patches"])
    assert probe.kwargs["request_timeout_seconds"] == 5.0
    assert probe.session_ids[0].startswith("dsh-probe-")
    assert probe.prompts[0] == settings.dsh_sandbox_probe_prompt
    assert probe.closed is True
    # 探测目录跑完即删，不在任务 home 下累积会话
    assert list((tmp_path / "data" / "dsh_home" / "ini_sandbox" / "_probe").iterdir()) == []


async def test_fallback_meta_present_on_failure(monkeypatch, sandbox_settings):
    """业务轮失败时 meta 仍带审计字段，便于从产物里判断当时是否提权。"""
    _install(monkeypatch)
    _Harness.probe_events = _UNAVAILABLE_EVENTS

    class _FailSession(_Session):
        def run(self, prompt, on_notification=None):
            if not self.harness.is_probe:
                raise RuntimeError("runtime closed")
            return super().run(prompt, on_notification)

    def _start(self, session_id=None):
        self.session_ids.append(session_id)
        return _FailSession(self, session_id)

    monkeypatch.setattr(_Harness, "start_session", _start)
    monkeypatch.setattr(_Harness, "resume_session", _start)
    reply = await _send()

    assert reply.status == "fail"
    assert reply.meta["permission_fallback"] is True


# --- 纯函数 --------------------------------------------------------------------


def test_unavailable_reason_scans_events():
    result = _Result(events=_UNAVAILABLE_EVENTS)
    assert dsh_mod._probe_unavailable_reason(result) == "sandbox_unavailable"


def test_unavailable_reason_empty_for_healthy_result():
    assert dsh_mod._probe_unavailable_reason(_Result(text="ok")) == ""


def test_probe_success_short_circuits_policy_text():
    """bash 已成功执行时，系统提示里的 'file access denied' 不能算后端不可用。"""
    result = _Result(
        text="ok",
        events=[
            {"type": "system/message", "data": {"text": "sandbox: file access denied"}},
            {
                "type": "tool/result",
                "data": {
                    "message": {
                        "role": "tool",
                        "isError": False,
                        "content": [{"type": "text", "text": "__dsh_probe__\n"}],
                    }
                },
            },
        ],
    )
    assert dsh_mod._probe_unavailable_reason(result) == ""


def test_probe_cache_helpers(monkeypatch, sandbox_settings):
    key = ("host", "0.2.1", "workspace-write", "/tmp")
    assert dsh_mod._probe_cache_get(key, 10.0) is None
    dsh_mod._probe_cache_put(key, False, "no usable backend")
    assert dsh_mod._probe_cache_get(key, 10.0) == (False, "no usable backend")
    assert dsh_mod._probe_cache_get(key, 0.0) is None
    dsh_mod.reset_sandbox_probe_cache()
    assert dsh_mod._probe_cache_get(key, 10.0) is None
