"""MCP 会话池（每任务独立实例 + 空闲 TTL 回收 + LRU 上限）的行为测试。

测试用一个常驻的 fake MCP server（帧化 JSON-RPC，响应 initialize / tools/list），
保证 spawn 真实子进程路径被覆盖。
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from sortie_deck.mcp_runtime import McpSessionPool, config_fingerprint
from sortie_deck.toolkit import CreateToolkitRequest, ToolkitItem, ToolkitStore

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
            result = {"protocolVersion": "2024-11-05", "capabilities": {},
                      "serverInfo": {"name": "fake"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "ping", "description": "pong",
                                  "inputSchema": {"type": "object"}}]}
        else:
            result = {"ok": True}
        write({"jsonrpc": "2.0", "id": msg["id"], "result": result})
    """
)


@pytest.fixture()
def mcp_item(tmp_path: Path) -> ToolkitItem:
    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_SERVER, encoding="utf-8")
    store = ToolkitStore(tmp_path / "toolkit.json")
    item = store.create(
        CreateToolkitRequest(
            kind="mcp",
            name="fake-mcp",
            summary="test server",
            transport="stdio",
            command=sys.executable,
            args=[str(script)],
        ),
        author_id="tester",
        author_name="Tester",
    )
    return item


@pytest.fixture()
def pool() -> McpSessionPool:
    return McpSessionPool(max_instances=10, ttl_seconds=300.0)


@pytest.mark.asyncio()
async def test_config_fingerprint_changes_with_config(mcp_item: ToolkitItem) -> None:
    same = config_fingerprint(mcp_item)
    changed = mcp_item.model_copy(update={"args": ["other"]})
    assert same == config_fingerprint(mcp_item)
    assert same != config_fingerprint(changed)


@pytest.mark.asyncio()
async def test_same_task_reuses_instance(mcp_item: ToolkitItem, pool: McpSessionPool) -> None:
    s1 = await pool.acquire(mcp_item, "task-a")
    s2 = await pool.acquire(mcp_item, "task-a")
    assert s1 is s2, "同任务同条目必须复用同一实例（消灭反复冷启动）"
    assert s1.tools, "握手后 tools/list 应已拉取"
    await pool.release(s1)
    await pool.release(s2)
    await pool.release_task("task-a")


@pytest.mark.asyncio()
async def test_different_tasks_get_isolated_instances(
    mcp_item: ToolkitItem, pool: McpSessionPool
) -> None:
    s1 = await pool.acquire(mcp_item, "task-a")
    s2 = await pool.acquire(mcp_item, "task-b")
    assert s1 is not s2, "不同任务即使同一 server 也必须各持独立实例"
    assert s1._proc is not None and s2._proc is not None
    assert s1._proc.pid != s2._proc.pid
    await pool.release(s1)
    await pool.release(s2)
    await pool.release_task("task-a")
    await pool.release_task("task-b")


@pytest.mark.asyncio()
async def test_task_end_releases_instances(mcp_item: ToolkitItem, pool: McpSessionPool) -> None:
    s1 = await pool.acquire(mcp_item, "task-a")
    await pool.release(s1)
    await pool.release_task("task-a")
    assert pool.stats()["instances"] == 0, "任务结束后空闲实例应立即回收"
    # 已结束任务重复登记是安全的
    await pool.release_task("task-a")
    assert pool.stats()["instances"] == 0


@pytest.mark.asyncio()
async def test_in_flight_protected_from_recycle(mcp_item: ToolkitItem) -> None:
    pool = McpSessionPool(max_instances=10, ttl_seconds=0.0)
    s1 = await pool.acquire(mcp_item, "task-a")
    await pool.release_task("task-a")
    # 在途（未 release）→ 即使 TTL=0 且任务已结束也不能回收
    assert pool.stats()["instances"] == 1
    await pool.release(s1)
    s_other = await pool.acquire(mcp_item, "task-zz")  # 触发一轮惰性扫描
    assert pool.stats()["instances"] == 1, "已结束任务的空闲实例应被扫描回收"
    await pool.release(s_other)
    await pool.release_task("task-zz")


@pytest.mark.asyncio()
async def test_idle_ttl_eviction(mcp_item: ToolkitItem) -> None:
    pool = McpSessionPool(max_instances=10, ttl_seconds=0.0)
    s1 = await pool.acquire(mcp_item, "task-a")
    await pool.release(s1)
    # TTL=0：下次 acquire 的惰性扫描应回收空闲实例
    s2 = await pool.acquire(mcp_item, "task-b")
    assert s2 is not s1
    assert pool.stats()["instances"] == 1
    await pool.release(s2)
    await pool.release_task("task-b")


@pytest.mark.asyncio()
async def test_lru_cap_and_busy_protection(
    tmp_path: Path, mcp_item: ToolkitItem
) -> None:
    pool = McpSessionPool(max_instances=1, ttl_seconds=300.0)
    s1 = await pool.acquire(mcp_item, "task-a")

    # 已有实例在途 → 无可驱逐 → 池耗尽
    with pytest.raises(RuntimeError, match="exhausted"):
        await pool.acquire(mcp_item, "task-b")

    await pool.release(s1)
    # 归还后 LRU 可驱逐 → 新任务借出成功
    s2 = await pool.acquire(mcp_item, "task-b")
    assert s2 is not s1
    await pool.release(s2)
    await pool.release_task("task-b")


@pytest.mark.asyncio()
async def test_stateful_exempts_idle_recycle(tmp_path: Path) -> None:
    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_SERVER, encoding="utf-8")
    store = ToolkitStore(tmp_path / "toolkit.json")
    item = store.create(
        CreateToolkitRequest(
            kind="mcp",
            name="stateful-mcp",
            summary="browser session",
            transport="stdio",
            command=sys.executable,
            args=[str(script)],
            stateful=True,
        ),
        author_id="tester",
        author_name="Tester",
    )
    pool = McpSessionPool(max_instances=10, ttl_seconds=0.0)
    s1 = await pool.acquire(item, "task-a")
    await pool.release(s1)
    s_other = await pool.acquire(item, "task-zz")  # 触发扫描：stateful 不参与空闲回收
    assert pool.stats()["instances"] == 2, "stateful 实例只随任务结束回收"
    await pool.release(s_other)
    await pool.release_task("task-a")
    await pool.release_task("task-zz")
    assert pool.stats()["instances"] == 0
