"""Codex provider：真实 JSONL 协议解析与凭据探测。"""

from __future__ import annotations

import json
from pathlib import Path

from sortie_deck.harness_providers import codex as codex_module
from sortie_deck.harness_providers.codex import CodexProvider, _parse_jsonl


def _line(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# --- 协议解析 ---------------------------------------------------------------


def test_parse_new_item_protocol_keeps_only_agent_message() -> None:
    stdout = "\n".join(
        [
            _line({"type": "thread.started", "thread_id": "th_123"}),
            _line({"type": "turn.started"}),
            _line({"type": "item.completed", "item": {"type": "reasoning", "text": "thinking..."}}),
            _line({"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}),
            _line({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}}),
        ]
    )
    text, native_id, errored, usage = _parse_jsonl(stdout)
    assert text == "OK"
    assert native_id == "th_123"
    assert errored is False
    assert usage == {"input_tokens": 12, "output_tokens": 3}


def test_parse_legacy_protocol_still_supported() -> None:
    stdout = "\n".join(
        [
            _line({"type": "session.started", "session_id": "s_9"}),
            _line({"type": "agent_message", "message": "legacy reply"}),
        ]
    )
    text, native_id, errored, usage = _parse_jsonl(stdout)
    assert text == "legacy reply"
    assert native_id == "s_9"
    assert errored is False
    assert usage == {}


def test_parse_item_error_is_a_warning_not_a_failure() -> None:
    # 实测 codex 会把配置告警也发成 item.type=error；正文仍可用时不应判失败
    stdout = "\n".join(
        [
            _line({"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}),
            _line({"type": "item.completed", "item": {"type": "error", "message": "config typo"}}),
        ]
    )
    text, _, errored, _ = _parse_jsonl(stdout)
    assert text == "partial"
    assert errored is False


def test_parse_stream_error_marks_failure() -> None:
    stdout = _line({"type": "error", "message": "stream broke"})
    text, _, errored, _ = _parse_jsonl(stdout)
    assert text == ""
    assert errored is True


# --- 凭据探测 ---------------------------------------------------------------


def test_probe_accepts_codex_login_file(tmp_path: Path, monkeypatch) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text('{"OPENAI_API_KEY": "sk-test"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(codex_module, "_binary", lambda: "/usr/local/bin/codex")

    status = CodexProvider().probe()
    assert status.status == "ok"


def test_probe_reports_auth_required_without_env_or_login(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # 空目录：无 auth.json
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(codex_module, "_binary", lambda: "/usr/local/bin/codex")

    status = CodexProvider().probe()
    assert status.status == "auth_required"
    assert "auth.json" in status.detail


def test_probe_reports_missing_binary(monkeypatch) -> None:
    monkeypatch.setattr(codex_module, "_binary", lambda: None)
    status = CodexProvider().probe()
    assert status.status == "missing"
