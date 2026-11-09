"""DSH patch 生成单测：工具映射、MCP 转换、base patch 安全默认。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from sortie_deck.harness_providers import dsh as dsh_mod
from sortie_deck.harness_providers.dsh import (
    _BASE_PATCH,
    _build_patches,
    _mcp_patch,
    _tool_off_patch,
)
from sortie_deck.settings import settings

# --- base patch ---------------------------------------------------------------


def test_base_patch_ships_safe_defaults():
    text = _BASE_PATCH.read_text(encoding="utf-8")
    # 会话日志默认上传必须被关掉
    assert "id: session-log-deepseek" in text
    assert "disabled: true" in text
    # 用户级技能根必须被切断
    assert "includeDefaultRoots: false" in text


def test_build_patches_default_posture(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dsh_session_log_upload", False)
    monkeypatch.setattr(settings, "dsh_isolate_skills", True)
    patches, meta = _build_patches(tmp_path, {"disallowed_tools": [], "mcp_servers": {}})

    names = [Path(p).name for p in patches]
    assert names == ["base.yml"]
    assert meta == {}


def test_build_patches_allows_explicit_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dsh_session_log_upload", True)
    monkeypatch.setattr(settings, "dsh_isolate_skills", False)
    patches, _ = _build_patches(tmp_path, {"disallowed_tools": [], "mcp_servers": {}})

    names = [Path(p).name for p in patches]
    # 覆盖层必须排在 base 之后，后置 patch 才能覆盖前面
    assert names[:3] == ["base.yml", "log-on.yml", "skills-on.yml"]
    assert "disabled: false" in (tmp_path / "log-on.yml").read_text(encoding="utf-8")


def test_build_patches_writes_dynamic_files_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dsh_session_log_upload", False)
    monkeypatch.setattr(settings, "dsh_isolate_skills", True)
    _build_patches(
        tmp_path,
        {"disallowed_tools": ["Bash"], "mcp_servers": {"poc": {"type": "stdio", "command": "x"}}},
    )
    for name in ("tools-off.yml", "mcp.yml"):
        mode = (tmp_path / name).stat().st_mode & 0o777
        assert mode == 0o600


# --- 工具禁用 -----------------------------------------------------------------


def test_tool_off_patch_maps_rows_and_dedupes():
    text, notes = _tool_off_patch(["Bash", "Grep", "Glob", "Task"])
    assert text.count("id: tool-bash") == 1
    assert text.count("id: tool-fs-search") == 1
    assert "id: tool-subagent" in text
    assert notes == []


def test_tool_off_patch_reports_unmapped_with_reason():
    text, notes = _tool_off_patch(["Read", "NotebookEdit", "UnknownTool"])
    assert text == ""
    assert any("Read" in n and "read/write/edit" in n for n in notes)
    assert any("NotebookEdit" in n for n in notes)
    assert any("UnknownTool" in n for n in notes)


def test_tool_off_patch_ignores_blank_names():
    text, notes = _tool_off_patch(["", "   "])
    assert text == "" and notes == []


# --- MCP ----------------------------------------------------------------------


def test_mcp_patch_stdio_and_http():
    text, skipped = _mcp_patch(
        {
            "poc": {"type": "stdio", "command": "python", "args": ["s.py"], "env": {"K": "v"}},
            "web": {"type": "http", "url": "http://x/mcp"},
        }
    )
    assert skipped == []
    assert "@deepseek-ai/dsh-mcp-client" in text
    assert "transport: stdio" in text
    assert "transport: streamable-http" in text
    assert 'serverName: "poc"' in text


def test_mcp_patch_skips_unsupported_and_incomplete():
    text, skipped = _mcp_patch(
        {
            "legacy": {"type": "sse", "url": "http://y/sse"},
            "broken": {"type": "stdio"},
            "nourl": {"type": "http"},
        }
    )
    assert text == ""
    assert any("sse" in s for s in skipped)
    assert any("command" in s for s in skipped)
    assert any("url" in s for s in skipped)


def test_mcp_patch_sanitizes_server_name():
    text, _ = _mcp_patch({"bad name/with:chars": {"type": "http", "url": "http://x"}})
    # DSH 只接受 [A-Za-z0-9_-]{1,32}
    assert 'serverName: "bad_name_with_chars"' in text


def test_mcp_patch_rejects_non_dict_config():
    text, skipped = _mcp_patch({"oops": "not-a-dict"})
    assert text == "" and skipped == []


# --- patch 文件清理 ------------------------------------------------------------


async def test_send_cleans_patch_dir_even_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "dsh_home_root", "")
    monkeypatch.setattr(settings, "dsh_api_key", "sk-test")

    class _Boom:
        def __init__(self, **kwargs) -> None:
            raise RuntimeError("spawn failed")

    module = types.ModuleType("deepseek_harness")
    module.DeepSeekHarness = _Boom
    monkeypatch.setitem(sys.modules, "deepseek_harness", module)
    monkeypatch.setattr(dsh_mod, "_sdk_available", lambda: True)

    provider = dsh_mod.DshProvider()
    session = await provider.start_session(initiative_id="ini_test", stage="build")
    reply = await provider.send(session, "go", cwd=str(tmp_path))

    assert reply.status == "fail"
    patch_root = tmp_path / "data" / "dsh_home" / "ini_test" / "_patches"
    assert list(patch_root.iterdir()) == []
