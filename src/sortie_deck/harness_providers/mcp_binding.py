"""把工具箱里「本阶段绑定/召回」的 MCP 条目转成 SDK 的 ``mcp_servers``。

背景：claude-agent-sdk 之前未显式传 ``mcp_servers``，客户端会按 ``setting_sources``
加载本机 ``~/.claude.json`` 的**全部** mcpServers，导致每个阶段都背着 5 个常驻 server
的工具 schema（实测 4 个 postgres + rea-agents）。业界的共识是工具 schema 应按需加载
（progressive tool discovery / tool search），本项目已有「阶段绑定 + 阶段主动召回 +
toolkit_pick 上浮」链路，这里只需把它接到 SDK 上，并用 ``strict_mcp_config`` 切断

- 输入: ``ctx.toolkit_ids``（已含阶段主动召回结果）→ ``resolve_mcp_items`` → ToolkitItem
- 输出: ``(servers, notes)``，``servers`` 直接给 ``ClaudeAgentOptions.mcp_servers``

注意：harness 是「一次 query 跑完整个阶段」，所以 MCP 集合必须在会话启动前确定；
阶段中途 ``toolkit_pick`` 上浮的 MCP 只能从下一阶段开始生效（该约束写进 toolkit 协议）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _clean_name(name: str, used: set[str]) -> str:
    """清洗成 SDK 可接受的 server 名（[A-Za-z0-9_-]），重名追加序号。"""
    base = _NAME_SAFE_RE.sub("_", (name or "").strip()).strip("_") or "mcp"
    candidate = base
    idx = 2
    while candidate in used:
        candidate = f"{base}_{idx}"
        idx += 1
    used.add(candidate)
    return candidate


def _server_config(item: Any) -> dict[str, Any] | None:
    """按 transport 生成 SDK 配置；缺关键字段返回 None（调用方记 skipped）。"""
    transport = (item.transport or "stdio").lower()
    if transport == "stdio":
        command = (item.command or "").strip()
        if not command:
            return None
        # command 可能被导入成「命令 + 参数」的一整行，这里拆开以免 exec 找不到可执行文件
        parts = command.split()
        cfg: dict[str, Any] = {"type": "stdio", "command": parts[0]}
        args = [*parts[1:], *[str(a) for a in (item.args or [])]]
        if args:
            cfg["args"] = args
        if item.env:
            cfg["env"] = {str(k): str(v) for k, v in item.env.items()}
        return cfg
    url = (item.url or "").strip()
    if not url:
        return None
    # sse 与 http 的配置结构一致，仅 type 不同
    return {"type": "sse" if transport == "sse" else "http", "url": url}


def mcp_servers_from_toolkit(
    store: Any,
    toolkit_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """把本阶段绑定/召回的 MCP 条目转成 SDK ``mcp_servers``。

    输入: store=工具箱存储, toolkit_ids=["tk_xxx", ...]（可含非 MCP 条目，自动跳过）
    输出: (servers, notes)；未绑定时返回 ({}, [])，配合 strict_mcp_config=True
          即「本阶段不加载任何 MCP」。
    notes 只记录条目名与跳过原因，**不包含 env / url 中的凭据**。
    """
    if not toolkit_ids:
        return {}, []
    try:
        from sortie_deck.mcp_runtime import resolve_mcp_items
    except Exception:  # noqa: BLE001 — 可选依赖缺失时按「无 MCP」处理
        logger.warning("MCP 绑定跳过：mcp_runtime 不可用", exc_info=True)
        return {}, []

    servers: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    used: set[str] = set()
    for item in resolve_mcp_items(store, list(toolkit_ids)):
        name = _clean_name(getattr(item, "name", "") or getattr(item, "id", ""), used)
        cfg = _server_config(item)
        if cfg is None:
            notes.append(f"{name}: 跳过（transport={item.transport} 缺 command/url）")
            continue
        servers[name] = cfg
        notes.append(f"{name}: 已挂载（{cfg['type']}）")

    if servers:
        logger.info(
            "mcp bound: count=%d names=%s", len(servers), ",".join(sorted(servers))
        )
    return servers, notes
