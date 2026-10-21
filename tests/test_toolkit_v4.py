"""V4 工具箱新功能测试：load 字段 / MCP 测试连接 / zip 导入 / 市场精选 / skill-install 优化与草稿确认。

连通性路径覆盖：
- stdio 会话用真实子进程 fake MCP server（同 test_mcp_pool 模式）
- 优化链路：LLM 不可用时走规则兜底；确认后草稿删除（未选版本清除）
"""

from __future__ import annotations

import asyncio
import base64
import sys
import textwrap
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from sortie_deck.skill_install import (
    MARKET_SKILLS,
    SkillDraft,
    SkillDraftStore,
    install_from_github,
    market_request,
    optimize_skill_md,
    parse_github_repo,
    rule_optimize,
)
from sortie_deck.toolkit import (
    CreateToolkitRequest,
    ToolkitStore,
    parse_mcp_zip_bytes,
)


# ---------------------------------------------------------------------------
# load 字段
# ---------------------------------------------------------------------------


def test_load_field_default_and_update(tmp_path: Path) -> None:
    store = ToolkitStore(tmp_path / "toolkit.json")
    item = store.create(
        CreateToolkitRequest(kind="tool", name="t1", runtime="cli", endpoint="ls"),
        author_id="u",
        author_name="U",
    )
    assert item.load == "defer"  # 默认按需
    updated = store.update(item.id, __import__("sortie_deck.toolkit", fromlist=["UpdateToolkitRequest"]).UpdateToolkitRequest(load="always"))
    assert updated.load == "always"


# ---------------------------------------------------------------------------
# GitHub 安装
# ---------------------------------------------------------------------------


def test_parse_github_repo_rejects_non_repo_urls() -> None:
    assert parse_github_repo("https://github.com/org/skill-repo") == ("org", "skill-repo")
    assert parse_github_repo("https://github.com/org/skill-repo.git") == ("org", "skill-repo")
    with pytest.raises(ValueError):
        parse_github_repo("https://example.com/x.md")  # MD 直链不支持
    with pytest.raises(ValueError):
        parse_github_repo("not-a-url")


def test_install_from_github_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """monkeypatch httpx：raw SKILL.md 200 时正常入库。"""

    class FakeResp:
        status_code = 200
        text = "---\nname: web-skill\ndescription: 来自 GitHub 的技能\n---\n## 用途\nx"

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            assert url.endswith("/main/SKILL.md")
            return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    req = asyncio.run(install_from_github("https://github.com/org/skill-repo"))
    assert req.name == "web-skill"
    assert req.source == "github:org/skill-repo"


def test_install_from_github_404(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResp:
        status_code = 404
        text = ""

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    with pytest.raises(RuntimeError):
        asyncio.run(install_from_github("https://github.com/org/empty"))


# ---------------------------------------------------------------------------
# 市场精选
# ---------------------------------------------------------------------------


def test_market_catalog_and_request() -> None:
    assert len(MARKET_SKILLS) >= 3
    req = market_request("data-viz")
    assert req.kind == "skill"
    assert req.body.startswith("---")
    with pytest.raises(KeyError):
        market_request("nope")


def test_market_install_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    app_mod = importlib.import_module("sortie_deck.api.app")
    from sortie_deck.toolkit import ToolkitStore

    store = ToolkitStore(tmp_path / "toolkit.json")
    monkeypatch.setattr(app_mod, "get_toolkit", lambda: store)
    from sortie_deck.api.app import MarketInstallRequest, install_market_skill

    req = MarketInstallRequest(key="api-tester")
    first = asyncio.run(install_market_skill(req, _test_user()))
    again = asyncio.run(install_market_skill(req, _test_user()))
    assert first["id"] == again["id"]  # 已存在时幂等返回既有条目
    assert len([i for i in store.list(kind="skill") if i.name == "api-tester"]) == 1


# ---------------------------------------------------------------------------
# skill-install 优化与草稿确认
# ---------------------------------------------------------------------------


def test_rule_optimize_preserves_body() -> None:
    body = "## 用途\n查日志"
    out = rule_optimize("log-tool", body)
    assert "## 原始正文" in out and "查日志" in out
    assert out.startswith("---\nname: log-tool")


def test_optimize_skill_md_fallback_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    from sortie_deck.llm import LlmClient

    monkeypatch.setattr(LlmClient, "enabled", property(lambda self: False))
    optimized, backend = asyncio.run(optimize_skill_md("my-skill", "正文"))
    assert backend == "rule"
    assert "my-skill" in optimized


def test_draft_confirm_deletes_unkept_version(tmp_path: Path) -> None:
    """确认选版后：条目创建、草稿删除（未选版本从本地存储清除）。"""
    store = SkillDraftStore(tmp_path / "drafts.json")
    draft = store.save(
        SkillDraft(name="s1", original="原版", optimized="优化版", backend="rule")
    )
    assert store.get(draft.draft_id).optimized == "优化版"
    store.delete(draft.draft_id)
    with pytest.raises(KeyError):
        store.get(draft.draft_id)


# ---------------------------------------------------------------------------
# MCP zip 导入（stdio 仅支持压缩包方式）
# ---------------------------------------------------------------------------


def test_parse_mcp_zip_extracts_and_prefills_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sortie_deck import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "data_dir", tmp_path)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("server.js", "console.log('mcp')")
        zf.writestr("package.json", "{}")
    req = parse_mcp_zip_bytes(buf.getvalue(), filename="server-filesystem-1.0.zip")
    assert req.kind == "mcp" and req.transport == "stdio"
    # slug 化：点号转连字符
    assert req.command == "npx -y @modelcontextprotocol/server-server-filesystem-1-0"
    pkg_dir = tmp_path / "mcp_packages" / "server-filesystem-1-0"
    assert (pkg_dir / "server.js").exists()


# ---------------------------------------------------------------------------
# /api/toolkit/mcp/test：stdio 真实子进程连通
# ---------------------------------------------------------------------------

_FAKE_SERVER = textwrap.dedent(
    """
    import sys, json

    def read():
        headers = b""
        while b"\\r\\n\\r\\n" not in headers:
            chunk = sys.stdin.buffer.readline()
            if not chunk:
                return None
            headers += chunk
        length = 0
        for line in headers.decode().split("\\r\\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1])
        return json.loads(sys.stdin.buffer.read(length))

    def write(obj):
        raw = json.dumps(obj).encode()
        sys.stdout.buffer.write(f"Content-Length: {len(raw)}\\r\\n\\r\\n".encode() + raw)
        sys.stdout.buffer.flush()

    while True:
        msg = read()
        if msg is None:
            break
        if msg.get("id") is None:
            continue
        method = msg.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": [{"name": "ping", "description": "pong", "inputSchema": {"type": "object"}}]}
        else:
            result = {"ok": True}
        write({"jsonrpc": "2.0", "id": msg["id"], "result": result})
    """
)


def _test_user():
    from sortie_deck.auth import PublicUser

    return PublicUser(id="u1", username="tester", display_name="Tester", role="admin", status="active")


def test_mcp_test_endpoint_stdio_ok(tmp_path: Path) -> None:
    from sortie_deck.api.app import McpTestRequest, test_mcp_connector

    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_SERVER, encoding="utf-8")
    req = McpTestRequest(transport="stdio", command=sys.executable, args=[str(script)])
    result = asyncio.run(test_mcp_connector(req, _test_user()))
    assert result["ok"] is True
    assert result["tools"] == ["ping"]


def test_mcp_test_endpoint_failure_is_structured() -> None:
    from sortie_deck.api.app import McpTestRequest, test_mcp_connector

    req = McpTestRequest(transport="stdio", command="/nonexistent-binary-xyz")
    result = asyncio.run(test_mcp_connector(req, _test_user()))
    assert result["ok"] is False
    assert result["error"]


def test_mcp_test_endpoint_missing_url() -> None:
    from sortie_deck.api.app import McpTestRequest, test_mcp_connector

    req = McpTestRequest(transport="http", url="")
    result = asyncio.run(test_mcp_connector(req, _test_user()))
    assert result["ok"] is False
