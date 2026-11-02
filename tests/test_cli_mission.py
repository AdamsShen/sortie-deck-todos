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


def test_split_repo_url_handles_scp_and_branch():
    # scp 写法里的 @ 不是分支：整串当 URL
    assert cli._split_repo_url("git@github.com:org/web.git") == ("git@github.com:org/web.git", "")
    # # 是最稳妥的分支写法，支持 release/1.0 这类带斜杠的分支
    assert cli._split_repo_url("git@github.com:org/web.git#release/1.0") == (
        "git@github.com:org/web.git",
        "release/1.0",
    )
    assert cli._split_repo_url("https://h/org/web.git@dev") == ("https://h/org/web.git", "dev")
    # https 里的 user@host 不会被误判成分支
    assert cli._split_repo_url("https://user@host/org/web.git") == (
        "https://user@host/org/web.git",
        "",
    )


def test_parse_repo_spec_from_flags_and_file(tmp_path):
    payload = {"role_repos": {"product": ["git@example.invalid:doc.git#main"]}}
    path = tmp_path / "repos.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    repos = cli._parse_repo_spec(
        ["eng=git@example.invalid:web.git#dev", "Eng=https://h/qa.git@test"], str(path)
    )
    assert repos["product"] == [{"url": "git@example.invalid:doc.git", "branch": "main"}]
    assert repos["eng"] == [
        {"url": "git@example.invalid:web.git", "branch": "dev"},
        {"url": "https://h/qa.git", "branch": "test"},
    ]


def test_parse_repo_spec_rejects_bad_slot():
    import pytest

    with pytest.raises(ValueError, match="未知槽位"):
        cli._parse_repo_spec(["ops=git@x/y.git"], None)
    with pytest.raises(ValueError, match="SLOT=URL"):
        cli._parse_repo_spec(["git@x/y.git"], None)


def test_collect_materials_reads_file_and_inline(tmp_path, capsys):
    path = tmp_path / "prd.md"
    path.write_text("# 需求\n导出 JSON", encoding="utf-8")
    args = SimpleNamespace(
        file=[str(path)],
        content="# 设计\n右上角",
        name="design.md",
        kind="prd",
        pinned=True,
        max_bytes=1_000_000,
    )
    rows = cli._collect_materials(args)
    assert [r["name"] for r in rows] == ["prd.md", "design.md"]
    assert rows[0]["text_excerpt"].startswith("# 需求")
    assert all(r["pinned"] is True and r["kind"] == "prd" for r in rows)


def test_collect_materials_rejects_binary_and_truncates(tmp_path, capsys):
    import pytest

    binary = tmp_path / "a.bin"
    binary.write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(ValueError, match="不是文本文件"):
        cli._collect_materials(
            SimpleNamespace(file=[str(binary)], content=None, kind="other", pinned=False, max_bytes=0)
        )

    big = tmp_path / "big.md"
    big.write_text("x" * 50, encoding="utf-8")
    rows = cli._collect_materials(
        SimpleNamespace(file=[str(big)], content=None, kind="other", pinned=False, max_bytes=10)
    )
    assert len(rows[0]["text_excerpt"]) == 10
    assert "已截断" in capsys.readouterr().err


def test_merge_materials_overrides_by_name():
    merged = cli._merge_materials(
        [{"name": "prd.md", "kind": "prd"}, {"name": "design.md", "kind": "design"}],
        [{"name": "prd.md", "kind": "plan"}],
    )
    by_name = {m["name"]: m["kind"] for m in merged}
    assert by_name == {"prd.md": "plan", "design.md": "design"}


def test_create_mission_posts_draft_body(monkeypatch):
    """create 必须 auto_confirm 且不 auto_start，否则 start 还得再确认一次。"""
    client = object.__new__(cli.RemoteClient)
    sent: dict = {}

    def fake_request(method, path, **kwargs):
        sent.update({"method": method, "path": path, **kwargs})
        return {"id": "ini_new"}

    monkeypatch.setattr(client, "request", fake_request)
    args = SimpleNamespace(title=None, brief="给设置页加导出按钮", track="standard", harness="mock")
    client.create_mission(args, {"eng": [{"url": "git@x/y.git", "branch": "dev"}]}, [])

    assert sent["method"] == "POST" and sent["path"] == "/api/initiatives"
    body = sent["json"]
    assert body["auto_start"] is False
    assert body["auto_confirm_pipeline"] is True
    assert body["title"] == "给设置页加导出按钮"
    assert body["role_repos"]["eng"][0]["url"] == "git@x/y.git"
    assert "materials" not in body  # 空材料不污染请求体
