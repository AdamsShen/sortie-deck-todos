"""远程 MCP 服务端：把任务能力以 Streamable HTTP 暴露给外部 agent。

设计取舍（对齐 2026-07-28 规范与业界实践）：
- **无状态**：不依赖协议级会话，任意请求可打到任意副本；跨调用状态用
  ``mission_id`` 这个 server-minted handle 传递。
- **工具面收敛**：12 个 task-shaped 工具，不镜像 117 个 REST 路由。
- **身份只来自 Header**：工具参数里没有 user_id，避免冒充；无状态模式下
  每个请求解析一次 API Key。
- **长任务不阻塞**：``start_mission`` 立即返回，轮询交给 ``await_mission``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from sortie_deck.service import (
    Principal,
    ServiceError,
    get_mission_service,
    resolve_principal,
)

logger = logging.getLogger(__name__)

# 身份只从本次请求的 Authorization 头解析：无状态模式下每个请求独立，
# 不依赖任何进程级或上下文变量（SDK 的 SessionManager 可能在新任务里处理请求）。

MAX_ARTIFACT_CHARS = 200_000

server = MCPServer(
    "sortie-deck",
    instructions=(
        "Sortie Deck 是 AI 原生的办公迭代控制平面。用 start_mission 提交需求，"
        "用 await_mission 等待阶段推进；任务会在关键节点停下等你确认（门禁），"
        "此时用 get_mission 看清在审批什么（summary / risk / irreversible），"
        "再用 submit_hitl 批准、驳回或补充指令。同一个任务也可以在工作台网页上查看和操作。"
    ),
)


def _principal_from(ctx: Context) -> Principal:
    """从本次 MCP 请求的 HTTP 头解析调用者。

    工具参数里没有 user_id，身份只能来自 Key——这是防止冒充的唯一正确做法。
    """
    headers = {str(k).lower(): str(v) for k, v in (ctx.headers or {}).items()}
    token = headers.get("authorization") or ""
    if not token and headers.get("x-api-key"):
        token = f"Bearer {headers['x-api-key']}"
    try:
        return resolve_principal(token, source="mcp")
    except ServiceError as exc:
        raise ValueError(f"{exc.code}: {exc.message}") from exc


def _shape(mission: Any, response_format: str = "concise") -> dict[str, Any]:
    """把任务压成 agent 友好结构；concise 只给决策必需字段。"""
    hitl = mission.pending_hitl
    gate = None
    if hitl is not None:
        gate = {
            "stage": hitl.stage,
            "prompt": hitl.prompt,
            "summary": hitl.artifact_summary,
            "risk": hitl.risk,
            "irreversible": hitl.irreversible,
            "options": [a.value for a in hitl.allowed_actions],
            "action_help": hitl.action_help,
        }
    payload: dict[str, Any] = {
        "mission_id": mission.id,
        "title": mission.title,
        "status": mission.status.value,
        "stage": mission.current_stage,
        "updated_at": mission.updated_at.isoformat(),
        "gate": gate,
        "artifact_count": len(mission.artifacts),
    }
    if response_format == "detailed":
        payload["brief"] = mission.brief
        payload["artifacts"] = [
            {"stage": a.stage, "kind": a.kind, "path": a.path} for a in mission.artifacts[-20:]
        ]
        payload["timeline"] = [
            {"stage": e.stage, "kind": e.kind, "message": e.message}
            for e in mission.timeline[-10:]
        ]
    return payload


# ---------------------------------------------------------------------------
# 工具：写
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "提交一个新任务并立即开始执行。返回 mission_id；任务会在第一个门禁处停下，"
        "用 await_mission 等待，不要假设它已经完成。"
    )
)
async def start_mission(
    brief: str,
    title: str | None = None,
    track: str | None = None,
    harness: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    principal = _principal_from(ctx)
    try:
        ini = await get_mission_service().start_mission(
            principal, brief=brief, title=title, track=track, harness=harness
        )
    except ServiceError as exc:
        raise ValueError(f"{exc.code}: {exc.message}") from exc
    # 队列深度：让调用方知道是排队还是立刻执行（取不到就算了，不能因此失败）
    queue = getattr(getattr(get_mission_service(), "orch", None), "run_queue", None)
    try:
        queue_depth = queue.pending_count() if queue is not None else 0
    except Exception:  # noqa: BLE001 — 统计失败不影响建任务
        queue_depth = 0
    return {
        "mission_id": ini.id,
        "status": ini.status.value,
        "stage": ini.current_stage,
        "queue_depth": queue_depth,
        "next": "await_mission 等待门禁；或 get_mission 查看状态",
    }


@server.tool(
    description=(
        "处理当前门禁。action 取 approve / reject / edit_instruction / stop；"
        "edit_instruction 必须带 instruction。批准前先用 get_mission 看清 gate 的 summary 与 risk。"
    )
)
async def submit_hitl(
    mission_id: str, action: str, instruction: str | None = None, ctx: Context = None  # type: ignore[assignment]
) -> dict[str, Any]:
    principal = _principal_from(ctx)
    try:
        ini = await get_mission_service().hitl(
            principal, mission_id, action=action, instruction=instruction
        )
    except ServiceError as exc:
        raise ValueError(f"{exc.code}: {exc.message}") from exc
    return _shape(ini)


@server.tool(description="停止任务。已结束的任务原样返回。")
async def stop_mission(mission_id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
    principal = _principal_from(ctx)
    try:
        ini = await get_mission_service().stop(principal, mission_id)
    except ServiceError as exc:
        raise ValueError(f"{exc.code}: {exc.message}") from exc
    return _shape(ini)


# ---------------------------------------------------------------------------
# 工具：读
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "读取单个任务的当前状态与门禁详情。response_format=concise 只给决策必需字段，"
        "detailed 额外带 brief / 产物列表 / 最近时间线（消耗更多上下文）。"
    )
)
async def get_mission(
    mission_id: str, response_format: str = "concise", ctx: Context = None  # type: ignore[assignment]
) -> dict[str, Any]:
    principal = _principal_from(ctx)
    try:
        ini = get_mission_service().get(principal, mission_id)
    except ServiceError as exc:
        raise ValueError(f"{exc.code}: {exc.message}") from exc
    return _shape(ini, response_format)


@server.tool(description="列出我创建或被共享的任务（只返回本人可见）。")
async def list_missions(
    status: str | None = None, limit: int = 10, ctx: Context = None  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    principal = _principal_from(ctx)
    items = get_mission_service().list_mine(principal, status=status, limit=limit)
    return [_shape(ini) for ini in items]


@server.tool(
    description=(
        "等待任务到达门禁或终态，命中即返回；超时返回 still_running=true。"
        "这是长任务的推荐用法，不要用轮询 get_mission 打满上下文。"
    )
)
async def await_mission(
    mission_id: str, timeout_seconds: int = 120, ctx: Context = None  # type: ignore[assignment]
) -> dict[str, Any]:
    principal = _principal_from(ctx)
    svc = get_mission_service()
    deadline = asyncio.get_running_loop().time() + max(5, min(timeout_seconds, 900))
    while True:
        try:
            ini = svc.get(principal, mission_id)
        except ServiceError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc
        if ini.status.value in {"waiting_hitl", "done", "failed", "stopped"}:
            payload = _shape(ini)
            payload["still_running"] = False
            return payload
        if asyncio.get_running_loop().time() >= deadline:
            payload = _shape(ini)
            payload["still_running"] = True
            payload["hint"] = "任务仍在跑；可再次 await_mission 或 get_mission"
            return payload
        await asyncio.sleep(1.5)


@server.tool(description="列出任务的产物清单（阶段 / 类型 / 路径）。")
async def list_artifacts(
    mission_id: str, ctx: Context = None  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    principal = _principal_from(ctx)
    try:
        refs = get_mission_service().artifacts(principal, mission_id)
    except ServiceError as exc:
        raise ValueError(f"{exc.code}: {exc.message}") from exc
    return [
        {"stage": r.stage, "kind": r.kind, "path": r.path, "created_at": r.created_at.isoformat()}
        for r in refs
    ]


@server.tool(
    description=(
        "读取单个产物文本（超过 200k 字符会截断）。path 用 list_artifacts 返回的相对路径。"
    )
)
async def read_artifact(
    mission_id: str, path: str, ctx: Context = None  # type: ignore[assignment]
) -> dict[str, Any]:
    principal = _principal_from(ctx)
    try:
        content = get_mission_service().read_artifact(principal, mission_id, path)
    except ServiceError as exc:
        raise ValueError(f"{exc.code}: {exc.message}") from exc
    truncated = len(content) > MAX_ARTIFACT_CHARS
    return {
        "path": path,
        "content": content[:MAX_ARTIFACT_CHARS],
        "truncated": truncated,
        "chars": len(content),
    }


@server.tool(
    description="按 seq 拉取任务事件（进度/门禁/房间消息），用于需要细粒度过程时。"
)
async def get_events(
    mission_id: str, since_seq: int = 0, limit: int = 100, ctx: Context = None  # type: ignore[assignment]
) -> dict[str, Any]:
    principal = _principal_from(ctx)
    try:
        rows = get_mission_service().events(principal, mission_id, since_seq)
    except ServiceError as exc:
        raise ValueError(f"{exc.code}: {exc.message}") from exc
    rows = rows[: max(1, min(limit, 300))]
    return {
        "events": [{"seq": seq, "event": event} for seq, event in rows],
        "next_seq": rows[-1][0] if rows else since_seq,
    }


@server.tool(description="列出可用航线与阶段，用于选择 track 参数。")
def list_tracks(ctx: Context = None) -> list[dict[str, Any]]:  # type: ignore[assignment]
    from sortie_deck.pipeline import list_tracks as _tracks

    return _tracks()


@server.tool(description="在企业知识库里检索相关片段（RAG）。")
def retrieve_knowledge(
    query: str, limit: int = 5, ctx: Context = None  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    _principal_from(ctx)  # 仍要求有效凭据，避免匿名耗尽检索额度
    hits = _knowledge_gateway().retrieve(query, limit=max(1, min(limit, 20)))
    return [
        {
            "title": hit.title,
            "source": hit.source,
            "score": hit.score,
            "text": hit.content[:1500],
        }
        for hit in hits
    ]


@server.tool(description="写入一条共享记忆（kind 取 fact / preference / episode / lesson）。")
def create_memory(
    content: str,
    kind: str = "lesson",
    scope: str = "workspace",
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    principal = _principal_from(ctx)
    from sortie_deck.memory import CreateMemoryRequest

    try:
        request = CreateMemoryRequest(scope=scope, kind=kind, content=content)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — 枚举校验失败要给出可读提示
        raise ValueError(f"validation: kind/scope 取值非法（{exc}）") from exc
    entry = _memory_store().create(
        request,
        author_id=principal.user.id,
        author_name=principal.display_name,
    )
    return {"memory_id": entry.id, "kind": entry.kind, "scope": entry.scope}


# ---------------------------------------------------------------------------
# 资源
# ---------------------------------------------------------------------------


@server.resource(
    "mission://{mission_id}/state",
    description="任务的实时状态与门禁（只读快照）",
)
def mission_state(mission_id: str, ctx: Context = None) -> str:  # type: ignore[assignment]
    principal = _principal_from(ctx)
    ini = get_mission_service().get(principal, mission_id)
    return json.dumps(_shape(ini, "detailed"), ensure_ascii=False)


@server.resource(
    "mission://{mission_id}/artifact/{path}",
    description="单个产物的文本内容",
)
def mission_artifact(
    mission_id: str, path: str, ctx: Context = None  # type: ignore[assignment]
) -> str:
    principal = _principal_from(ctx)
    return get_mission_service().read_artifact(principal, mission_id, path)


@server.prompt(description="把一个模糊需求整理成可提交的任务简报")
def plan_mission(goal: str) -> str:
    return (
        "请把下面的目标整理成任务简报后调用 start_mission：\n"
        "1) 用 3-5 句话说清交付物与验收标准；\n"
        "2) 指出涉及的代码仓库或系统边界；\n"
        "3) 标注哪些是不可逆操作（部署/发布），以便门禁时确认。\n\n"
        f"目标：{goal}"
    )


# ---------------------------------------------------------------------------
# 延迟初始化的共享依赖
# ---------------------------------------------------------------------------

_gateway: Any = None
_memory: Any = None


def _knowledge_gateway() -> Any:
    global _gateway
    if _gateway is None:
        from sortie_deck.knowledge import KnowledgeStore
        from sortie_deck.knowledge_gateway import gateway_from_settings
        from sortie_deck.settings import settings

        _gateway = gateway_from_settings(settings, KnowledgeStore(settings.knowledge_db))
    return _gateway


def _memory_store() -> Any:
    global _memory
    if _memory is None:
        from sortie_deck.memory import build_memory_store
        from sortie_deck.settings import settings

        _memory = build_memory_store(settings.memory_db)
    return _memory


# ---------------------------------------------------------------------------
# ASGI 装配
# ---------------------------------------------------------------------------


class _ResolvePrincipalMiddleware:
    """在进入 MCP 子应用前把 Authorization 解析成 Principal。

    无状态模式下每个请求独立解析，不做跨请求缓存；工具层通过 ContextVar
    读取，因此工具参数里永远不需要（也不允许）出现 user_id。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        token = headers.get("authorization") or f"Bearer {headers.get('x-api-key', '')}"
        # 这里只做 fail-fast 的门禁（无效凭据直接 401，不进入 JSON-RPC 处理）；
        # 工具层仍会按请求头再解析一次，作为唯一身份依据。
        try:
            resolve_principal(token, source="mcp")
        except ServiceError as exc:
            body = json.dumps({"error": exc.as_dict()}, ensure_ascii=False).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": exc.http_status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="sortie-deck"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


_sub_app: Any = None


def build_mcp_app(
    *,
    stateless: bool = True,
    allowed_hosts: list[str] | None = None,
    path: str = "/mcp",
) -> Any:
    """构造可挂到 FastAPI 的 ASGI 应用（路径由挂载点决定）。

    注意 DNS-rebinding 防护：SDK 在 host 参数为 127.0.0.1/localhost 时会自动开启，
    并把 Host 白名单限制成 localhost——容器/反代场景下 Host 是外网域名，
    会被直接判 421。这里默认关闭（身份由 API Key 保证），需要时用
    ``TDT_MCP_ALLOWED_HOSTS`` 显式开启白名单。
    """
    global _sub_app
    from mcp.server.transport_security import TransportSecuritySettings

    security = (
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=list(allowed_hosts)
        )
        if allowed_hosts
        else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    _sub_app = server.streamable_http_app(
        streamable_http_path=path,
        json_response=False,
        stateless_http=stateless,
        transport_security=security,
    )
    return _ResolvePrincipalMiddleware(_sub_app)


def lifespan_context() -> Any:
    """MCP 子应用的 lifespan（启动 session manager）。

    Starlette 的 ``Mount`` 不会自动运行子应用 lifespan，父应用必须代为进入；
    测试里也直接用它包住请求。
    """
    if _sub_app is None:
        return contextlib.nullcontext()
    return _sub_app.router.lifespan_context(_sub_app)
