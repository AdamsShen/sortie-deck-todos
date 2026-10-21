"""元工具链路端到端测试：模型回复文本 → 意图解析 → pick 上浮 → mcp 真实调用。

覆盖此前断裂的两段：
1. 意图解析层：```toolkit_intent 围栏 → _parse_toolkit_intents → StageResult.meta；
2. pick 上浮的 mcp 回写 stage_toolkits → resolve_mcp_items 能找到 → 池化会话
   真实建连 → 工具调用成功。
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from sortie_deck.graph import merge_picked_toolkits, run_toolkit_meta
from sortie_deck.harness_providers.executor import _parse_toolkit_intents
from sortie_deck.mcp_runtime import McpToolInvoker, resolve_mcp_items
from sortie_deck.toolkit import CreateToolkitRequest, ToolkitStore

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
            result = {"content": [{"type": "text", "text": "pong"}]}
        write({"jsonrpc": "2.0", "id": msg["id"], "result": result})
    """
)


@pytest.fixture()
def store(tmp_path: Path) -> ToolkitStore:
    return ToolkitStore(tmp_path / "toolkit.json")


def _mcp_item(store: ToolkitStore, tmp_path: Path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_SERVER, encoding="utf-8")
    return store.create(
        CreateToolkitRequest(
            kind="mcp",
            name="pg-logs",
            summary="查询 postgres 慢查询日志",
            transport="stdio",
            command=sys.executable,
            args=[str(script)],
            config={"allow_all_tools": True},
        ),
        author_id="tester",
        author_name="Tester",
    )


# --- 1. 意图解析层 ------------------------------------------------------------


def test_parse_single_intent_block() -> None:
    reply = (
        "我需要查慢日志。\n```toolkit_intent\n"
        '{"toolkit_search": {"query": "postgres 慢日志"}}\n```\n其余正文。'
    )
    assert _parse_toolkit_intents(reply) == {
        "toolkit_search": {"query": "postgres 慢日志"}
    }


def test_parse_array_block_and_multiple_keys() -> None:
    reply = textwrap.dedent(
        """\
        ```toolkit_intent
        [
          {"toolkit_search": {"query": "postgres logs"}},
          {"mcp_calls": [{"tool": "ping", "arguments": {}}]}
        ]
        ```
        done.
        """
    )
    out = _parse_toolkit_intents(reply)
    assert out["toolkit_search"] == {"query": "postgres logs"}
    assert out["mcp_calls"] == [{"tool": "ping", "arguments": {}}]


def test_parse_ignores_garbage_and_unknown_keys() -> None:
    assert _parse_toolkit_intents("no block here") == {}
    # 坏 JSON 静默忽略
    assert _parse_toolkit_intents("```toolkit_intent\n{broken\n```\n") == {}
    # 白名单外的键不透传（防 prompt 注入任意 meta 键）
    assert _parse_toolkit_intents('```toolkit_intent\n{"evil": {"a": 1}}\n```\n') == {}
    # 无围栏时裸 JSON 不解析（必须走约定标记，避免误读正文）
    assert _parse_toolkit_intents('{"toolkit_search": {"query": "x"}}') == {}


# --- 2. 全链路：回复文本 → pick → 回写 → 真实调用 ------------------------------


@pytest.mark.asyncio()
async def test_full_chain_reply_text_to_real_mcp_call(
    store: ToolkitStore, tmp_path: Path
) -> None:
    item = _mcp_item(store, tmp_path)

    # ① 模型按协议输出含意图围栏的回复文本（第一回合：先 search 后 pick）
    reply_text = textwrap.dedent(
        f"""\
        需要查库。```toolkit_intent
        {{"toolkit_pick": {{"item_id": "{item.id}"}}}}
        ```"""
    )
    meta = _parse_toolkit_intents(reply_text)
    assert "toolkit_pick" in meta

    # ② 编排层兑现 pick：上浮进 brief，record 带 kind=mcp
    out = await run_toolkit_meta(store, stage="eng_backend", meta=meta)
    assert out.records[0]["floated"] is True
    assert out.records[0]["kind"] == "mcp"
    assert "pg-logs" in out.brief_append

    # ③ 阶段节点回写：pick 的 mcp 并入 stage_toolkits
    state: dict[str, Any] = {"stage_toolkits": {}}
    stage_toolkits = merge_picked_toolkits(state, "eng_backend", out.records)
    assert stage_toolkits["eng_backend"] == [item.id]

    # ④ 同回合的 mcp_calls：解析层拿到意图 → resolve 能找到新条目 → 池化会话真实调用
    reply_text2 = textwrap.dedent(
        """\
        ```toolkit_intent
        {"mcp_calls": [{"tool": "ping", "arguments": {}}]}
        ```"""
    )
    meta2 = _parse_toolkit_intents(reply_text2)
    assert meta2["mcp_calls"] == [{"tool": "ping", "arguments": {}}]
    ids = stage_toolkits["eng_backend"]
    items = resolve_mcp_items(store, ids)
    assert [i.id for i in items] == [item.id], "pick 上浮的 mcp 必须能被解析为会话目标"

    invoker = McpToolInvoker(enabled=True)
    try:
        await invoker.open(items, task_id="task-e2e")
        assert invoker.available_tools() == ["ping"]
        result = await invoker.call("ping", {})
        assert result["ok"] is True, f"上浮后的 mcp 工具应可真实调用: {result}"
    finally:
        await invoker.close()
        from sortie_deck.mcp_runtime import get_mcp_pool, reset_mcp_pool

        await get_mcp_pool().release_task("task-e2e")
        await reset_mcp_pool()


@pytest.mark.asyncio()
async def test_pick_tool_and_skill_do_not_write_stage_toolkits(
    store: ToolkitStore,
) -> None:
    """只有 mcp 回写 stage_toolkits；tool/skill 走各自的 brief / skill_run 路径。"""
    tool = store.create(
        CreateToolkitRequest(
            kind="tool", name="git-diff", runtime="cli", endpoint="git diff --stat"
        ),
        author_id="tester",
        author_name="Tester",
    )
    skill = store.create(
        CreateToolkitRequest(
            kind="skill",
            name="check",
            summary="check",
            executable=True,
            scripts=[{"path": "scripts/a.py", "language": "py", "content": "print(1)"}],
        ),
        author_id="tester",
        author_name="Tester",
    )
    state: dict[str, Any] = {"stage_toolkits": {"qa": ["tk_bound"]}}
    for picked in (tool, skill):
        out = await run_toolkit_meta(
            store, stage="qa", meta={"toolkit_pick": {"item_id": picked.id}}
        )
        stage_toolkits = merge_picked_toolkits(state, "qa", out.records)
        assert stage_toolkits["qa"] == ["tk_bound"], f"{picked.kind} 不应回写 stage_toolkits"
