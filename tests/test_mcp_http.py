"""MCP http/sse 传输层的行为测试（真实 uvicorn 线程服务器，覆盖协议全路径）。

- transport="http"：Streamable HTTP——initialize 拿 Mcp-Session-Id、JSON 响应、
  SSE 响应、JSON-RPC error → dead 标记；
- transport="sse"：legacy HTTP+SSE——endpoint 事件、响应按 id 经事件流匹配。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from sortie_deck.mcp_http import HttpMcpSession, SseMcpSession, iter_sse_events_sync
from sortie_deck.toolkit import CreateToolkitRequest, ToolkitItem, ToolkitStore

# --- fake servers ------------------------------------------------------------


def _streamable_app(state: dict[str, Any]) -> Starlette:
    """Streamable HTTP fake：单端点 /mcp，可用 state 开关切换响应形态。"""

    async def mcp_endpoint(request):  # type: ignore[no-untyped-def]
        if request.method == "DELETE":  # 会话终止（尽力而为，恒成功）
            return Response(status_code=200)
        body = await request.json()
        method = body.get("method")
        rid = body.get("id")
        if method == "initialize":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "fake-http"},
                    },
                },
                headers={"Mcp-Session-Id": "sid-123"},
            )
        if rid is None:
            return Response(status_code=202)
        if method == "tools/list":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "tools": [
                            {"name": "ping", "description": "pong", "inputSchema": {"type": "object"}}
                        ]
                    },
                }
            )
        # tools/call
        state.setdefault("calls", []).append(
            {
                "tool": body["params"]["name"],
                "arguments": body["params"].get("arguments") or {},
                "session_id": request.headers.get("mcp-session-id"),
            }
        )
        if state.get("rpc_error"):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "boom"}}
            )
        if state.get("sse_response"):
            # 服务端选择以 SSE 流返回本请求的响应（progress + 最终响应）
            async def gen() -> AsyncIterator[bytes]:
                yield b": keepalive\n\n"
                payload = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"content": [{"type": "text", "text": "echo-sse"}]},
                    }
                )
                yield f"event: message\ndata: {payload}\n\n".encode()

            return StreamingResponse(gen(), media_type="text/event-stream")
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "echo"}]}}
        )

    return Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["POST", "DELETE"])])


def _sse_app(state: dict[str, Any]) -> Starlette:
    """legacy HTTP+SSE fake：GET /sse 发 endpoint 事件 + 恰好 3 个响应后收流。"""
    tools = [{"name": "ping", "description": "pong", "inputSchema": {"type": "object"}}]

    async def sse(request):  # type: ignore[no-untyped-def]
        # ASGI 流式响应在 uvicorn 里真实分块下发；GET 流服务满 3 个响应后结束
        async def gen() -> AsyncIterator[bytes]:
            queue: asyncio.Queue = state.setdefault("queue", asyncio.Queue())
            yield f"event: endpoint\ndata: /messages?sid={state['sid']}\n\n".encode()
            for _ in range(3):
                item = await asyncio.wait_for(queue.get(), timeout=5)
                yield f"event: message\ndata: {json.dumps(item)}\n\n".encode()

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def messages(request):  # type: ignore[no-untyped-def]
        queue: asyncio.Queue = state.setdefault("queue", asyncio.Queue())
        body = await request.json()
        if body.get("id") is None:  # notification：无响应，202 即可
            return Response(status_code=202)
        method = body.get("method")
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fake-sse"},
            }
        elif method == "tools/list":
            result = {"tools": tools}
        else:
            state.setdefault("calls", []).append(body["params"]["name"])
            result = {"content": [{"type": "text", "text": "echo"}]}
        await queue.put({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
        return Response(status_code=202)

    async def stop(request):  # type: ignore[no-untyped-def]
        return Response(status_code=200)

    return Starlette(
        routes=[
            Route("/sse", sse),
            Route("/messages", messages, methods=["POST"]),
            Route("/messages", stop, methods=["DELETE"]),
        ]
    )


@asynccontextmanager
async def _running_server(app: Starlette) -> Iterator[str]:
    """在后台线程跑 uvicorn（port=0 取实际端口），yield base url。"""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn did not start in time"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _mcp_item(base: str, path: str, transport: str) -> ToolkitItem:
    store = ToolkitStore(Path("/tmp") / "mcp-http-test-toolkit.json")
    return store.create(
        CreateToolkitRequest(kind="mcp", name="http-mcp", transport=transport, url=f"{base}{path}"),
        author_id="tester",
        author_name="Tester",
    )


# --- streamable http ---------------------------------------------------------


@pytest.mark.asyncio()
async def test_http_json_roundtrip() -> None:
    state: dict[str, Any] = {}
    async with _running_server(_streamable_app(state)) as base:
        session = HttpMcpSession(_mcp_item(base, "/mcp", "http"))
        await session.start()
        assert [t.name for t in session.tools] == ["ping"]
        result = await session.call_tool("ping", {"x": 1})
        assert result["content"][0]["text"] == "echo"
        # initialize 返回的 session id 被保留并回传给后续请求
        assert state["calls"][0]["session_id"] == "sid-123"
        assert session.alive
        await session.close()


@pytest.mark.asyncio()
async def test_http_sse_response_mode() -> None:
    state: dict[str, Any] = {"sse_response": True}
    async with _running_server(_streamable_app(state)) as base:
        session = HttpMcpSession(_mcp_item(base, "/mcp", "http"))
        await session.start()
        result = await session.call_tool("ping", {})
        # 响应以 SSE 流到达：解析 data 行，命中本请求 id 的最终响应
        assert result["content"][0]["text"] == "echo-sse"
        await session.close()


@pytest.mark.asyncio()
async def test_http_rpc_error_marks_dead() -> None:
    state: dict[str, Any] = {"rpc_error": True}
    async with _running_server(_streamable_app(state)) as base:
        session = HttpMcpSession(_mcp_item(base, "/mcp", "http"))
        await session.start()
        with pytest.raises(RuntimeError, match="rpc error"):
            await session.call_tool("ping", {})
        # 失败即 dead：池下次借出会逐出重建，而不是复用坏会话
        assert not session.alive
        await session.close()


# --- legacy sse --------------------------------------------------------------


@pytest.mark.asyncio()
async def test_sse_legacy_roundtrip() -> None:
    state: dict[str, Any] = {"sid": "sse-1"}
    async with _running_server(_sse_app(state)) as base:
        session = SseMcpSession(_mcp_item(base, "/sse", "sse"))
        await session.start()
        assert [t.name for t in session.tools] == ["ping"]
        result = await session.call_tool("ping", {})
        assert result["content"][0]["text"] == "echo"
        assert state["calls"] == ["ping"]
        await session.close()


# --- sse parser --------------------------------------------------------------


def test_sse_parser_multi_line_and_comments() -> None:
    raw = ": comment\nevent: endpoint\ndata: /messages?sid=1\n\n\ndata: a\ndata: b\n\n"
    events = list(iter_sse_events_sync(raw.splitlines()))
    assert events == [("endpoint", "/messages?sid=1"), ("message", "a\nb")]
