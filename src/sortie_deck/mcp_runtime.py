from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sortie_deck.toolkit import ToolkitItem

logger = logging.getLogger(__name__)


@dataclass
class McpToolInfo:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


class McpSession:
    """Minimal MCP stdio JSON-RPC client (initialize + tools/list + tools/call)."""

    def __init__(self, item: ToolkitItem) -> None:
        self.item = item
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.tools: list[McpToolInfo] = []
        self.log: list[str] = []

    async def start(self) -> None:
        if self.item.transport and self.item.transport != "stdio":
            self.log.append(f"skip non-stdio MCP {self.item.name} ({self.item.transport})")
            return
        if not self.item.command:
            self.log.append(f"MCP {self.item.name}: missing command")
            return
        import os

        merged_env = {**os.environ, **(self.item.env or {})}
        self._proc = await asyncio.create_subprocess_exec(
            self.item.command,
            *list(self.item.args or []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        assert self._proc.stdin and self._proc.stdout
        self._writer = self._proc.stdin
        self._reader = self._proc.stdout
        await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sortie-deck", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized", {})
        listed = await self._request("tools/list", {})
        for raw in (listed or {}).get("tools") or []:
            self.tools.append(
                McpToolInfo(
                    name=str(raw.get("name") or ""),
                    description=str(raw.get("description") or ""),
                    input_schema=raw.get("inputSchema") or {},
                )
            )
        self.log.append(f"MCP {self.item.name}: {len(self.tools)} tools")

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        self.log.append(f"call {name}")
        return result

    @property
    def alive(self) -> bool:
        """池借出前的存活判据：子进程已 spawn 且未退出（与 http 会话的 alive 同名）。"""
        proc = self._proc
        return proc is not None and proc.returncode is None

    async def close(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except TimeoutError:
                self._proc.kill()
        self._proc = None

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        req_id = self._next_id
        self._next_id += 1
        await self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        while True:
            msg = await self._read()
            if msg is None:
                return None
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result")

    async def _write(self, payload: dict[str, Any]) -> None:
        if not self._writer:
            return
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode()
        self._writer.write(header + raw)
        await self._writer.drain()

    async def _read(self) -> dict[str, Any] | None:
        if not self._reader:
            return None
        # Content-Length framed
        headers = b""
        while b"\r\n\r\n" not in headers:
            chunk = await self._reader.readline()
            if not chunk:
                return None
            headers += chunk
        length = 0
        for line in headers.decode().split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        body = await self._reader.readexactly(length)
        return json.loads(body.decode("utf-8"))


class McpSessionManager:
    def __init__(self) -> None:
        self.sessions: list[McpSession] = []

    async def open_for_items(self, items: list[ToolkitItem]) -> list[McpSession]:
        opened: list[McpSession] = []
        for item in items:
            if item.kind != "mcp" or not item.enabled:
                continue
            session = McpSession(item)
            try:
                await session.start()
                opened.append(session)
                self.sessions.append(session)
            except Exception as exc:  # noqa: BLE001 — soft-fail per server
                logger.warning("MCP start failed for %s: %s", item.name, exc)
                session.log.append(f"start failed: {exc}")
                opened.append(session)
        return opened

    def tools_manifest(self) -> str:
        lines: list[str] = []
        for s in self.sessions:
            for t in s.tools:
                lines.append(f"- [{s.item.name}] {t.name}: {t.description}")
        return "\n".join(lines)

    def combined_log(self) -> str:
        return "\n".join(line for s in self.sessions for line in s.log)

    async def close_all(self) -> None:
        for s in self.sessions:
            await s.close()
        self.sessions.clear()


def resolve_mcp_items(store: Any, toolkit_ids: list[str]) -> list[ToolkitItem]:
    items: list[ToolkitItem] = []
    if store is None:
        return items
    for tid in toolkit_ids:
        try:
            item = store.get(tid)
        except Exception:
            continue
        if item and item.kind == "mcp" and item.enabled:
            items.append(item)
    return items


def config_fingerprint(item: ToolkitItem) -> str:
    """条目连接配置的稳定指纹（command/args/env/transport/url/认证头）。

    配置变了就必须起新实例，所以它参与池键而不是只看 item_id。
    env 与认证头参与指纹但从不落日志（认证头变化 = 连接身份变化）。
    """
    auth_headers = sorted(
        (str(k), str(v)) for k, v in ((item.config or {}).get("headers") or {}).items()
    )
    payload = json.dumps(
        [
            item.command,
            item.args,
            sorted((item.env or {}).items()),
            item.transport,
            item.url,
            auth_headers,
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _create_session(item: ToolkitItem) -> Any:
    """按 transport 分派会话实现：stdio 子进程 / Streamable HTTP / legacy SSE。

    http/sse 会话与 stdio 会话暴露同一套接口（start/call_tool/close/alive），
    池与 invoker 对传输层无感知。函数挂在模块级，测试可 monkeypatch。
    """
    transport = (item.transport or "").lower()
    if item.kind == "mcp" and transport in {"http", "sse"} and item.url:
        from sortie_deck.mcp_http import HttpMcpSession, SseMcpSession

        return HttpMcpSession(item) if transport == "http" else SseMcpSession(item)
    return McpSession(item)


@dataclass
class _PoolEntry:
    session: McpSession
    task_id: str
    stateful: bool = False
    in_flight: int = 0
    last_used: float = field(default_factory=time.monotonic)


class McpSessionPool:
    """MCP 会话池（方案文档 3.5 节：按 transport 分域隔离 + 空闲回收）。

    - 实例键 = (item_id, config_hash, 隔离域)：隔离域由 transport 与 stateful 决定——
      stdio 恒为真实 task_id（每任务独立实例，进程一对一物理上不可共享）；
      http/sse 且非 stateful 固定为 "shared"（共享桶：无进程成本、无 per-user 状态，
      跨任务直接复用同一会话）；stateful 的 http/sse 仍按 task_id 隔离防状态泄露；
    - 任务内复用：同键已存在且存活 → 直接借出，消灭反复冷启动；
    - 回收判据 = 无在途请求 AND（空闲超过 ttl_seconds OR 任务已结束）；
      "无在途请求"是硬前提——纯心跳计数会误杀分钟级长调用；
    - 容量：全局实例上限，满了先触发一轮回收，仍满则按 LRU 驱逐无在途实例；
    - 扫描是惰性的（借出时顺带执行），不引入后台线程，行为确定可测。
    回收失败/重建失败都只记日志并 soft-fail，不影响阶段主流程。
    """

    def __init__(
        self,
        *,
        max_instances: int = 50,
        ttl_seconds: float = 300.0,
    ) -> None:
        self.max_instances = max(1, int(max_instances))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._entries: dict[tuple[str, str, str], _PoolEntry] = {}
        self._lock = asyncio.Lock()
        self._ended_tasks: set[str] = set()

    def _entry_alive(self, entry: _PoolEntry) -> bool:
        # http/sse 会话自带 alive 语义（未关闭且无致命错误）；stdio 走进程存活
        alive = getattr(entry.session, "alive", None)
        if alive is not None:
            return bool(alive)
        proc = getattr(entry.session, "_proc", None)
        return proc is not None and proc.returncode is None

    def _mark_task_ended(self, task_id: str) -> None:
        """任务结束登记：其全部实例在下一轮惰性扫描中回收（即使仍空闲未超 TTL）。"""
        self._ended_tasks.add(task_id)

    def _close_sync_safe(self, entry: _PoolEntry) -> None:
        """非阻塞回收：请求 terminate，不等 wait（等待在后台任务里完成）。

        仅 stdio 会话有进程可 terminate；http/sse 会话的关闭走 _evict_entry 的
        await close() 路径，这里直接跳过。
        """
        proc = getattr(entry.session, "_proc", None)
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    async def _evict_entry(self, key: tuple[str, str, str], entry: _PoolEntry) -> None:
        self._entries.pop(key, None)
        try:
            await asyncio.wait_for(entry.session.close(), timeout=3)
        except Exception as exc:  # noqa: BLE001 — 回收失败只记日志
            logger.warning(
                "mcp pool: evict %s failed: %s", key[0], exc
            )

    async def _sweep(self) -> None:
        """惰性回收：无在途 AND（空闲超 TTL OR 任务已结束）→ 关闭并移除。"""
        now = time.monotonic()
        ended = self._ended_tasks
        for key, entry in list(self._entries.items()):
            if entry.in_flight > 0:
                continue
            if entry.stateful and key[2] not in ended:
                # 有状态实例不参与空闲回收（只在任务结束时回收），防会话被误杀
                continue
            idle = now - entry.last_used
            if key[2] in ended or idle >= self.ttl_seconds:
                logger.info(
                    "mcp pool: evicting %s (task=%s idle=%.0fs ended=%s)",
                    key[0],
                    key[2],
                    idle,
                    key[2] in ended,
                )
                await self._evict_entry(key, entry)
        self._ended_tasks = {t for t in ended if any(k[2] == t for k in self._entries)}

    async def _make_room(self, exclude_key: tuple[str, str, str]) -> bool:
        """容量不足时先全量回收，仍满则 LRU 驱逐无在途实例。返回是否腾出空位。"""
        await self._sweep()
        if len(self._entries) < self.max_instances:
            return True
        idle_entries = sorted(
            (
                (k, e)
                for k, e in self._entries.items()
                if e.in_flight == 0 and k != exclude_key and not e.stateful
            ),
            key=lambda pair: pair[1].last_used,
        )
        for key, entry in idle_entries:
            logger.info("mcp pool: LRU evicting %s (task=%s)", key[0], key[2])
            await self._evict_entry(key, entry)
            if len(self._entries) < self.max_instances:
                return True
        return False

    async def acquire(self, item: ToolkitItem, task_id: str) -> Any:
        """借出一个可用会话：命中则复用，未命中则新建（按 transport 分派实现）。

        返回的会话 in_flight 已 +1，调用方负责在结束后调用 release(entry)
        （或经 McpToolInvoker.close 统一释放），否则该实例永远不会被回收。
        """
        async with self._lock:
            await self._sweep()
            # 隔离域：无状态 http/sse 进共享桶跨任务复用；其余按任务隔离
            transport = (item.transport or "").lower()
            is_httpish = item.kind == "mcp" and transport in {"http", "sse"}
            bucket = "shared" if is_httpish and not item.stateful else task_id
            key = (item.id, config_fingerprint(item), bucket)
            entry = self._entries.get(key)
            if entry is not None and self._entry_alive(entry):
                entry.in_flight += 1
                entry.last_used = time.monotonic()
                return entry.session
            if entry is not None:
                # 死实例：先移除再重建
                await self._evict_entry(key, entry)
            if len(self._entries) >= self.max_instances and not await self._make_room(key):
                raise RuntimeError(
                    f"mcp pool exhausted ({self.max_instances} instances, all busy)"
                )
            session = _create_session(item)
            try:
                await session.start()
            except Exception as exc:
                logger.warning("mcp pool: start failed for %s (task=%s): %s", item.name, task_id, exc)
                raise
            entry = _PoolEntry(session=session, task_id=task_id, stateful=bool(item.stateful))
            entry.in_flight = 1
            self._entries[key] = entry
            return session

    async def release(self, session: McpSession) -> None:
        """归还借出的会话：仅递减在途计数，不关闭进程（留给回收判据决定）。"""
        async with self._lock:
            for entry in self._entries.values():
                if entry.session is session:
                    entry.in_flight = max(0, entry.in_flight - 1)
                    entry.last_used = time.monotonic()
                    return

    async def release_task(self, task_id: str) -> None:
        """任务结束：登记后立即回收该任务的全部空闲实例（有在途的留给下轮扫描）。"""
        async with self._lock:
            self._mark_task_ended(task_id)
            await self._sweep()

    def stats(self) -> dict[str, int]:
        return {"instances": len(self._entries), "ended_tasks": len(self._ended_tasks)}


_pool: McpSessionPool | None = None


def get_mcp_pool() -> McpSessionPool:
    """进程级单例池（懒创建，参数读 settings）。测试可直接替换 _pool。"""
    global _pool
    if _pool is None:
        from sortie_deck.settings import settings

        _pool = McpSessionPool(
            max_instances=settings.mcp_pool_max_instances,
            ttl_seconds=settings.mcp_pool_ttl_seconds,
        )
    return _pool


async def reset_mcp_pool() -> None:
    """测试辅助：清空并重置进程级单例。"""
    global _pool
    if _pool is not None:
        for key, entry in list(_pool._entries.items()):
            await _pool._evict_entry(key, entry)
        _pool = None


# Tool names that look like they mutate state (heuristic, documented as such).
_WRITE_TOOL_PREFIXES = ("write", "create", "update", "delete", "remove", "set", "put", "post", "move")


def tool_looks_like_write(tool_name: str) -> bool:
    lowered = (tool_name or "").strip().lower()
    return lowered.startswith(_WRITE_TOOL_PREFIXES)


class McpToolInvoker:
    """Call MCP tools with a per-server whitelist and a write-confirmation gate.

    Fails closed: a server that declares no whitelist exposes **no** callable
    tools, and write-looking tools require an explicit ``confirmed=True``.
    """

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self._manager = McpSessionManager()
        self._pooled_sessions: list[McpSession] = []
        self._allowed: dict[str, set[str]] = {}
        self._server_of: dict[str, str] = {}

    def _use_pool(self, task_id: str) -> bool:
        """池化开关判定：调用方给了 task_id 且池开关开启才走池，否则保持旧行为。"""
        if not task_id:
            return False
        from sortie_deck.settings import settings

        return bool(getattr(settings, "mcp_pool_enabled", False))

    def _register_whitelist(self, session: McpSession) -> None:
        allow_all = bool((session.item.config or {}).get("allow_all_tools"))
        declared = {
            str(t).strip()
            for t in ((session.item.config or {}).get("allowed_tools") or [])
            if str(t).strip()
        }
        for tool in session.tools:
            self._server_of[tool.name] = session.item.name
            if allow_all or tool.name in declared:
                self._allowed.setdefault(tool.name, set()).add(session.item.name)

    async def open(self, items: list[ToolkitItem], *, task_id: str = "") -> None:
        if not self.enabled:
            return
        if self._use_pool(task_id):
            # 池化路径：借出/复用实例，close() 只归还不关闭（回收判据在池里）
            pool = get_mcp_pool()
            for item in items:
                if item.kind != "mcp" or not item.enabled:
                    continue
                try:
                    session = await pool.acquire(item, task_id)
                except Exception as exc:  # noqa: BLE001 — 单 server 失败不拖垮整批
                    logger.warning("mcp pool: acquire failed for %s: %s", item.name, exc)
                    continue
                self._pooled_sessions.append(session)
                self._register_whitelist(session)
            return
        sessions = await self._manager.open_for_items(items)
        for session in sessions:
            self._register_whitelist(session)

    def available_tools(self) -> list[str]:
        return sorted(self._allowed)

    def is_allowed(self, tool_name: str) -> bool:
        return bool(self._allowed.get(tool_name))

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "blocked": True, "reason": "MCP calling is disabled"}
        if not self.is_allowed(tool_name):
            return {
                "ok": False,
                "blocked": True,
                "reason": f"tool `{tool_name}` is not on any server whitelist",
            }
        if tool_looks_like_write(tool_name) and not confirmed:
            return {
                "ok": False,
                "blocked": True,
                "reason": f"tool `{tool_name}` looks like a write — confirmation required",
            }
        session = next(
            (s for s in self._pooled_sessions if s.item.name in self._allowed[tool_name]),
            None,
        )
        if session is None:
            session = next(
                (s for s in self._manager.sessions if s.item.name in self._allowed[tool_name]),
                None,
            )
        if session is None:
            return {"ok": False, "reason": f"no live session for `{tool_name}`"}
        try:
            result = await session.call_tool(tool_name, arguments or {})
        except Exception as exc:  # noqa: BLE001 — a failing tool must not break the stage
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": result}

    async def close(self) -> None:
        # 池化借出的会话只归还（递减在途计数），进程回收由池的回收判据决定；
        # 非池化（旧路径）仍即用即关。
        for session in self._pooled_sessions:
            await get_mcp_pool().release(session)
        self._pooled_sessions.clear()
        await self._manager.close_all()
        self._allowed.clear()
        self._server_of.clear()
