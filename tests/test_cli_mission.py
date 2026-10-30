"""P1：sortie mission CLI 的纯逻辑（不打网络、不起编排器）。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from sortie_deck import cli_mission as cli


def test_fail_uses_stderr_and_semantic_exit_code(capsys):
    code = cli._fail("not_found", "任务不存在")
    captured = capsys.readouterr()
    assert code == cli.EXIT_NOT_FOUND
    assert captured.out == ""  # --json 场景下 stdout 必须干净
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "not_found"


def test_printer_keeps_stdout_pure(capsys):
    printer = cli.Printer(as_json=True)
    printer.note("解析来源: 本地指针")
    printer.data({"id": "ini_1"})

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"id": "ini_1"}
    assert "解析来源" in captured.err


def test_pointer_roundtrip_and_server_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cli, "POINTER_PATH", tmp_path / "last_mission.json")
    monkeypatch.setenv("SORTIE_URL", "http://host-a:8787")

    cli.remember_mission("ini_abc")
    assert cli._pointer_mission_id() == "ini_abc"

    # 换了服务端，旧指针不能复用（否则会操作到另一台机器上的同名任务）
    monkeypatch.setenv("SORTIE_URL", "http://host-b:8787")
    assert cli._pointer_mission_id() == ""


def test_hitl_action_mapping():
    base = {"stop": False, "reject": False, "instruction": None}
    assert cli._hitl_action(SimpleNamespace(**base)) == ("approve", None)
    assert cli._hitl_action(SimpleNamespace(**{**base, "reject": True}))[0] == "reject"
    assert cli._hitl_action(SimpleNamespace(**{**base, "instruction": "补一条验收"})) == (
        "edit_instruction",
        "补一条验收",
    )
    assert cli._hitl_action(SimpleNamespace(**{**base, "stop": True}))[0] == "stop"


def test_mission_brief_renders_structured_gate():
    payload = {
        "id": "ini_1",
        "title": "通知中心",
        "status": "waiting_hitl",
        "current_stage": "product_prd",
        "updated_at": "2026-10-30T10:00:00Z",
        "pending_hitl": {
            "stage": "product_prd",
            "prompt": "Review outputs",
            "allowed_actions": ["approve", "reject"],
            "risk": "只读评审",
            "irreversible": False,
            "artifact_summary": "prd.md（12 个验收点）",
        },
        "artifacts": [{"path": "a"}, {"path": "b"}],
    }
    brief = cli._mission_brief(payload)
    assert brief["artifacts"] == 2
    text = cli._render_status(brief)
    assert "product_prd" in text
    assert "只读评审" in text
    assert "prd.md（12 个验收点）" in text
    assert "approve / reject" in text


def test_mission_brief_without_gate():
    brief = cli._mission_brief(
        {"id": "ini_2", "title": "t", "status": "running", "current_stage": "build"}
    )
    assert brief["hitl"] is None
    assert "running" in cli._render_status(brief)
