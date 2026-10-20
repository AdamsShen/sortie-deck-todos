"""MCP HTTP 传输层：Streamable HTTP 与 legacy HTTP+SSE 两种会话实现。

与 stdio 版 ``McpSession``（mcp_runtime.py）暴露相同的鸭子接口
（``item`` / ``tools`` / ``log`` / ``start()`` / ``call_tool()`` / ``close()`` / ``alive``），
因此 ``McpSessionPool`` 可以无感知分派（见 ``mcp_runtime._create_session``）。

协议参照：
- transport="http" → Streamable HTTP（2025-03-26 规范，现行标准）：
  单一 MCP 端点，每个 JSON-RPC 请求各发一个 POST；响应为 application/json
  或 text/event-stream 二选一（两种都要解析）；会话经 Mcp-Session-Id 响应头
  维护（initialize 返回则带上，结束尽力 DELETE）。
  https://modelcontextprotocol.io/specification/latest/basic/transports/streamable-http
- transport="sse" → legacy HTTP+SSE（2024-11-05 规范）：
  先 GET 建立 SSE 事件流，服务端经 endpoint 事件告知 POST 地址；
  请求 POST 到该地址，响应经事件流按 id 匹配异步返回。
  https://modelcontextprotocol.io/specification/2024-11-05/basic/transports

认证：读 ``item.config["headers"]``（原样注入，不落日志）与
``item.config["bearer_token"]``（转 Authorization: Bearer）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Iterable, Iterator
from urllib.parse import urljoin

import httpx

from sortie_deck.mcp_runtime import McpToolInfo
from sortie_deck.toolkit import ToolkitItem

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5.0
CALL_TIMEOUT_SECONDS = 30.0

_STREAMABLE_HTTP_ACCEPT = "application/json, text/event-stream"


class _SseParser:
    """SSE 事件状态机：逐行 feed，遇到空行产出完整 (event, data) 事件。

    兼容多行 data（按规范以换行连接）；`:` 开头的注释行跳过；
    未标注 event 名的事件按 "message" 处理。
    """

    def __init__(self) -> None:
        self.event = "message"
        self._data_lines: list[str] = []

    def feed(self, raw_line: str) -> tuple[str, str] | None:
        line = raw_line.rstrip("\r")
        if not line:
            if self._data_lines:
                event = (self.event, "\n".join(self._data_lines))
                self.event, self._data_lines = "message", []
                return event
            return None
        if line.startswith(":"):
            return None
        if line.startswith("event:"):
            self.event = line[6:].strip()
        elif line.startswith("data:"):
            self._data_lines.append(line[5:].strip())
        return None

    def flush(self) -> tuple[str, str] | None:
        """流结束时若还有未提交的 data，产出一个事件。"""
        if self._data_lines:
            event = (self.event, "\n".join(self._data_lines))
            self.event, self._data_lines = "message", []
            return event
        return None


def iter_sse_events_sync(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    """同步版本：解析已缓冲的 SSE 文本行（list[str] / 字符串拆行）。"""
    parser = _SseParser()
    for line in lines:
        event = parser.feed(line)
        if event is not None:
            yield event
    event = parser.flush()
    if event is not None:
        yield event


async def iter_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """异步版本：解析 aiter_lines() 行流。"""
    parser = _SseParser()
    async for line in lines:
        event = parser.feed(line)
        if event is not None:
            yield event
    event = parser.flush()
    if event is not None:
        yield event


def _auth_headers(item: ToolkitItem) -> dict[str, str]:
    """从条目 config 提取认证头；值不落任何日志。"""
    cfg = item.config or {}
    headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
    token = str(cfg.get("bearer_token") or "").strip()
    if token and "authorization" not in {k.lower() for k in headers}:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _rpc_result_or_raise(obj: dict[str, Any]) -> Any:
    """JSON-RPC 响应 → result；error 显式抛出（池据此标记 dead 并重建）。"""
    if isinstance(obj, dict) and obj.get("error"):
        raise RuntimeError(f"mcp rpc error: {obj['error']}")
    return obj.get("result") if isinstance(obj, dict) else None


class _HttpSessionBase:
    """两种 HTTP 会话的公共部分：client 生命周期、认证头、alive 语义。"""

    def __init__(
        self,
        item: ToolkitItem,
        *,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        call_timeout: float = CALL_TIMEOUT_SECONDS,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.item = item
        self.tools: list[McpToolInfo] = []
        self.log: list[str] = []
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        # 测试注入点（httpx.ASGITransport），生产恒为 None（直连）
        self._http_transport = http_transport
        self._client: httpx.AsyncClient | None = None
        self._next_id = 1
        self._closed = False
        self._error = ""

    @property
    def alive(self) -> bool:
        """池借出前的存活判据：未关闭且未发生过致命错误。"""
        return not self._closed and not self._error

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._http_transport,
            timeout=httpx.Timeout(self._call_timeout, connect=self._connect_timeout),
        )

    def _fail(self, message: str) -> None:
        # 首个致命错误即标记 dead，下一次借出时池会逐出并重建
        if not self._error:
            self._error = message
        self.log.append(message)
        logger.warning("mcp http session %s: %s", self.item.name, message)


class HttpMcpSession(_HttpSessionBase):
    """Streamable HTTP 会话（transport="http"，现行 MCP 标准）。"""

    def __init__(self, item: ToolkitItem, **kwargs: Any) -> None:
        super().__init__(item, **kwargs)
        self._session_id = ""

    def _headers(self, *, with_session: bool = True) -> dict[str, str]:
        headers = _auth_headers(self.item)
        headers["Accept"] = _STREAMABLE_HTTP_ACCEPT
        headers["Content-Type"] = "application/json"
        if with_session and self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(
        self, payload: dict[str, Any], *, expect_response: bool
    ) -> dict[str, Any] | None:
        """POST 一个 JSON-RPC 消息；响应可能是 JSON 或 SSE 流，统一归一。"""
        assert self._client is not None
        resp = await self._client.post(self.item.url, json=payload, headers=self._headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"mcp http: POST returned {resp.status_code}")
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            # 服务端选择以 SSE 流返回：逐事件找本次请求 id 的 JSON-RPC 响应
            if not expect_response or payload.get("id") is None:
                return None
            for _event, data in iter_sse_events_sync(resp.text.splitlines()):
                try:
                    obj = json.loads(data)
                except (TypeError, ValueError):
                    continue
                if isinstance(obj, dict) and obj.get("id") == payload.get("id"):
                    return _rpc_result_or_raise(obj)  # type: ignore[return-value]
            raise RuntimeError("mcp http: SSE stream ended without a matching response")
        if not expect_response or payload.get("id") is None:
            return None
        return _rpc_result_or_raise(resp.json())  # type: ignore[return-value]

    async def start(self) -> None:
        self._client = self._new_client()
        # initialize：响应头里的 Mcp-Session-Id 决定后续请求是否带会话
        resp = await self._client.post(
            self.item.url,
            json={
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "sortie-deck", "version": "0.1.0"},
                },
            },
            headers=self._headers(with_session=False),
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"mcp http: initialize returned {resp.status_code}")
        session_id = resp.headers.get("mcp-session-id") or ""
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            for _event, data in iter_sse_events_sync(resp.text.splitlines()):
                try:
                    obj = json.loads(data)
                except (TypeError, ValueError):
                    continue
                if isinstance(obj, dict) and obj.get("id") == self._next_id:
                    _rpc_result_or_raise(obj)
                    break
        else:
            _rpc_result_or_raise(resp.json())
        # 2026-07-28 修订移除了协议级会话：服务端不给 session id 就按无会话用
        self._session_id = session_id
        self._next_id += 1
        await self._client.post(
            self.item.url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self._headers(),
        )
        listed = await self._post(
            {"jsonrpc": "2.0", "id": self._next_id, "method": "tools/list", "params": {}},
            expect_response=True,
        )
        self._next_id += 1
        self._load_tools(listed or {})
        self.log.append(f"MCP {self.item.name} (http): {len(self.tools)} tools")

    def _load_tools(self, listed: dict[str, Any]) -> None:
        for raw in listed.get("tools") or []:
            self.tools.append(
                McpToolInfo(
                    name=str(raw.get("name") or ""),
                    description=str(raw.get("description") or ""),
                    input_schema=raw.get("inputSchema") or {},
                )
            )

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
            expect_response=True,
        )
        self._next_id += 1
        self.log.append(f"call {name}")
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client is not None and self._session_id:
            try:  # 尽力终止服务端会话，失败不影响回收
                await self._client.delete(
                    self.item.url, headers=self._headers(), timeout=self._connect_timeout
                )
            except Exception:  # noqa: BLE001 — best effort
                pass
        if self._client is not None:
            await self._client.aclose()
        self._client = None


class SseMcpSession(_HttpSessionBase):
    """legacy HTTP+SSE 会话（transport="sse"，2024-11-05 规范，兼容存量 server）。

    生命周期：GET 常驻读事件流 → endpoint 事件给出 POST 地址 →
    请求 POST 上去、响应经事件流按 id 匹配（Future 等待）。GET 流意外断开
    即标记 dead，由池在下次借出时逐出重建（不做自动重连，保持行为可测）。
    """

    def __init__(self, item: ToolkitItem, **kwargs: Any) -> None:
        super().__init__(item, **kwargs)
        self._post_url = ""
        self._endpoint_ready = asyncio.Event()
        self._responses: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._client = self._new_client()
        self._reader_task = asyncio.create_task(self._read_stream(), name="mcp-sse-reader")
        try:
            await asyncio.wait_for(self._endpoint_ready.wait(), timeout=self._connect_timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"mcp sse: endpoint event not received within {self._connect_timeout}s"
            ) from exc
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sortie-deck", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized")
        listed = await self._rpc("tools/list", {})
        self._load_tools(listed or {})
        self.log.append(f"MCP {self.item.name} (sse): {len(self.tools)} tools")

    def _load_tools(self, listed: dict[str, Any]) -> None:
        for raw in listed.get("tools") or []:
            self.tools.append(
                McpToolInfo(
                    name=str(raw.get("name") or ""),
                    description=str(raw.get("description") or ""),
                    input_schema=raw.get("inputSchema") or {},
                )
            )

    async def _read_stream(self) -> None:
        """常驻读 GET 事件流：endpoint 事件 → 记 POST 地址；message 事件 → 按 id 唤醒。"""
        assert self._client is not None
        try:
            async with self._client.stream(
                "GET", self.item.url, headers={**_auth_headers(self.item), "Accept": "text/event-stream"}
            ) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"mcp sse: GET stream returned {resp.status_code}")
                async for event, data in iter_sse_events(resp.aiter_lines()):
                    if self._closed:
                        return
                    if event == "endpoint" and data:
                        self._post_url = urljoin(self.item.url, data)
                        self._endpoint_ready.set()
                    elif data:
                        try:
                            obj = json.loads(data)
                        except (TypeError, ValueError):
                            continue
                        rid = obj.get("id") if isinstance(obj, dict) else None
                        fut = self._responses.pop(rid, None) if isinstance(rid, int) else None
                        if fut is not None and not fut.done():
                            fut.set_result(obj)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 流断开即 dead，池负责重建
            if not self._closed:
                self._fail(f"sse stream ended: {exc}")

    async def _post(self, payload: dict[str, Any]) -> None:
        assert self._client is not None
        if not self._post_url:
            raise RuntimeError("mcp sse: POST endpoint not known yet")
        resp = await self._client.post(
            self._post_url, json=payload, headers=_auth_headers(self.item)
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"mcp sse: POST returned {resp.status_code}")

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._responses[rid] = fut
        await self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        obj = await asyncio.wait_for(fut, timeout=self._call_timeout)
        return _rpc_result_or_raise(obj)  # type: ignore[return-value]

    async def _notify(self, method: str) -> None:
        await self._post({"jsonrpc": "2.0", "method": method})

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        self.log.append(f"call {name}")
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — 回收路径吞异常
                pass
            self._reader_task = None
        if self._client is not None and self._post_url:
            try:  # 尽力通知服务端关闭，失败不影响回收
                await self._client.delete(self._post_url, timeout=self._connect_timeout)
            except Exception:  # noqa: BLE001 — best effort
                pass
        if self._client is not None:
            await self._client.aclose()
        self._client = None
