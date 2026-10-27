"""MCP 按需挂载：ToolkitItem → SDK mcp_servers。"""

from __future__ import annotations

from typing import Any

from sortie_deck.harness_providers.mcp_binding import mcp_servers_from_toolkit
from sortie_deck.toolkit import ToolkitItem


class _FakeStore:
    def __init__(self, items: list[ToolkitItem]) -> None:
        self._items = {item.id: item for item in items}

    def get(self, item_id: str) -> Any:
        return self._items.get(item_id)


def _item(**kwargs: Any) -> ToolkitItem:
    payload = {"kind": "mcp", "name": "pg", **kwargs}
    return ToolkitItem(**payload)


def test_no_binding_returns_empty():
    assert mcp_servers_from_toolkit(_FakeStore([]), []) == ({}, [])


def test_stdio_transport_splits_command_and_merges_args():
    item = _item(
        name="pg-test",
        transport="stdio",
        command="npx -y @modelcontextprotocol/server-postgres",
        args=["postgresql://db"],
        env={"PGSSLMODE": "disable"},
    )
    servers, notes = mcp_servers_from_toolkit(_FakeStore([item]), [item.id])
    assert servers == {
        "pg-test": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-postgres", "postgresql://db"],
            "env": {"PGSSLMODE": "disable"},
        }
    }
    assert any("已挂载" in note for note in notes)


def test_sse_and_http_transports():
    sse = _item(name="browser", transport="sse", url="http://127.0.0.1:8931/sse")
    http = _item(name="gateway", transport="http", url="http://127.0.0.1:9000/mcp")
    servers, _notes = mcp_servers_from_toolkit(_FakeStore([sse, http]), [sse.id, http.id])
    assert servers["browser"] == {"type": "sse", "url": "http://127.0.0.1:8931/sse"}
    assert servers["gateway"] == {"type": "http", "url": "http://127.0.0.1:9000/mcp"}


def test_disabled_item_is_skipped():
    item = _item(name="off", transport="stdio", command="npx x", enabled=False)
    servers, notes = mcp_servers_from_toolkit(_FakeStore([item]), [item.id])
    assert servers == {}
    assert notes == []


def test_non_mcp_item_is_skipped():
    tool = ToolkitItem(kind="tool", name="curl", runtime="cli", command="curl")
    servers, notes = mcp_servers_from_toolkit(_FakeStore([tool]), [tool.id])
    assert servers == {}
    assert notes == []


def test_missing_command_or_url_records_note():
    bad_stdio = _item(name="broken", transport="stdio", command="")
    bad_http = _item(name="broken-http", transport="http", url="")
    servers, notes = mcp_servers_from_toolkit(
        _FakeStore([bad_stdio, bad_http]), [bad_stdio.id, bad_http.id]
    )
    assert servers == {}
    assert len(notes) == 2
    assert all("跳过" in note for note in notes)


def test_duplicate_names_are_disambiguated():
    first = _item(name="pg", transport="stdio", command="npx a")
    second = _item(name="pg", transport="stdio", command="npx b")
    servers, _notes = mcp_servers_from_toolkit(_FakeStore([first, second]), [first.id, second.id])
    assert set(servers) == {"pg", "pg_2"}


def test_name_is_sanitized_for_sdk():
    item = _item(name="my server/测试", transport="stdio", command="npx a")
    servers, _notes = mcp_servers_from_toolkit(_FakeStore([item]), [item.id])
    assert list(servers) == ["my_server"]


def test_credentials_never_leak_into_notes():
    item = _item(
        name="pg",
        transport="stdio",
        command="npx a",
        env={"PGPASSWORD": "secret-value"},
    )
    _servers, notes = mcp_servers_from_toolkit(_FakeStore([item]), [item.id])
    assert all("secret-value" not in note for note in notes)
