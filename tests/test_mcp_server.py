"""P3：MCP 服务端（Streamable HTTP）协议与工具契约。

用项目自带的 ``HttpMcpSession`` 指向自己的 ``/mcp``——这既是协议一致性测试，
也证明我们写的服务端能与现成的客户端互通。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from sortie_deck import mcp_server
from sortie_deck.auth import PublicUser
from sortie_deck.models import Initiative, InitiativeStatus
from sortie_deck.service import Principal
from sortie_deck.toolkit import ToolkitItem


class _FakeService:
    """只实现 MCP 工具用到的那几个方法。"""

    def __init__(self, mission: Initiative) -> None:
        self.mission = mission
        self.calls: list[tuple[str, dict]] = []
        self.orch = SimpleNamespace(run_queue=None)

    def get(self, principal, mission_id):
        return self.mission

    def list_mine(self, principal, *, status=None, limit=None):
        return [self.mission]

    async def start_mission(self, principal, **kwargs):
        self.calls.append(("start", kwargs))
        return self.mission

    async def hitl(self, principal, mission_id, *, action, instruction=None, note=None):
        self.calls.append(("hitl", {"action": str(action), "instruction": instruction}))
        return self.mission

    async def stop(self, principal, mission_id):
        self.calls.append(("stop", {}))
        return self.mission

    def artifacts(self, principal, mission_id):
        return list(self.mission.artifacts)

    def read_artifact(self, principal, mission_id, path):
        return "# hello"

    def events(self, principal, mission_id, since_seq=0):
        return []


@pytest.fixture()
def wired(monkeypatch):
    principal = Principal(
        user=PublicUser(
            id="usr_1", username="ada", display_name="Ada", role="member", status="active"
        ),
        source="mcp",
        key_id="key_1",
    )
    mission = Initiative(title="通知中心", brief="b", status=InitiativeStatus.RUNNING)
    fake = _FakeService(mission)

    monkeypatch.setattr(
        mcp_server, "resolve_principal", lambda *_a, **_k: principal
    )
    monkeypatch.setattr(mcp_server, "get_mission_service", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_tools_list_and_call_over_streamable_http(wired):
    from sortie_deck.mcp_http import HttpMcpSession

    app = mcp_server.build_mcp_app()
    item = ToolkitItem(kind="mcp", name="sortie-deck", transport="http", url="http://test/mcp")

    async with mcp_server.lifespan_context():
        session = HttpMcpSession(item, http_transport=httpx.ASGITransport(app=app))
        try:
            await session.start()
            names = {tool.name for tool in session.tools}
            # 工具面收敛：核心动作 + 装配动作必须在
            assert {
                "start_mission",
                "await_mission",
                "submit_hitl",
                "get_mission",
                "create_mission",
                "bind_repos",
                "attach_materials",
                "list_forge_drafts",
                "approve_forge_draft",
            } <= names
            assert len(names) == 17, sorted(names)

            result = await session.call_tool("start_mission", {"brief": "加一个健康检查接口"})
            assert result["isError"] is False
            started = result["structuredContent"]
            assert started["mission_id"] == wired.mission.id
            assert started["next"]

            detail = await session.call_tool(
                "get_mission", {"mission_id": wired.mission.id, "response_format": "detailed"}
            )
            detail_payload = detail["structuredContent"]
            assert detail_payload["title"] == "通知中心"
            assert "brief" in detail_payload
        finally:
            await session.close()


@pytest.mark.asyncio
async def test_missing_credential_is_rejected(wired, monkeypatch):
    """没有/无效的 Authorization 必须 401，且不进工具层。"""
    from sortie_deck.service import ServiceError

    def _raise(*_a, **_k):
        raise ServiceError("auth_required", "凭据无效")

    monkeypatch.setattr(mcp_server, "resolve_principal", _raise)
    app = mcp_server.build_mcp_app()

    async with mcp_server.lifespan_context():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert resp.status_code == 401
            assert resp.json()["error"]["code"] == "auth_required"


@pytest.mark.asyncio
async def test_tool_error_message_reaches_the_client(wired, monkeypatch):
    """业务错误必须带 code 透给模型。

    SDK 默认屏蔽普通异常的消息，客户端只会看到 "Error executing tool xxx"，
    模型无从判断该重试、换 id 还是放弃——所以工具里要抛 ``ToolError``。
    """
    from sortie_deck.mcp_http import HttpMcpSession
    from sortie_deck.service import ServiceError

    class _Boom:
        def __init__(self) -> None:
            self.orch = SimpleNamespace(run_queue=None)

        def get(self, principal, mission_id):
            raise ServiceError("not_found", f"任务不存在: {mission_id}")

    monkeypatch.setattr(mcp_server, "get_mission_service", lambda: _Boom())
    app = mcp_server.build_mcp_app()
    item = ToolkitItem(kind="mcp", name="sortie-deck", transport="http", url="http://test/mcp")

    async with mcp_server.lifespan_context():
        session = HttpMcpSession(item, http_transport=httpx.ASGITransport(app=app))
        try:
            await session.start()
            result = await session.call_tool("get_mission", {"mission_id": "ini_nope"})
            assert result["isError"] is True
            text = result["content"][0]["text"]
            assert "not_found" in text
            assert "ini_nope" in text
        finally:
            await session.close()


@pytest.mark.asyncio
async def test_assembly_tools_go_through_service(wired, monkeypatch):
    """create → bind → start 必须依次落到服务层，而不是各自为政。"""
    from sortie_deck.mcp_http import HttpMcpSession

    calls: list[tuple[str, dict]] = []
    draft = Initiative(title="草稿", brief="b", status=InitiativeStatus.DRAFT)
    running = Initiative(title="跑起来了", brief="b", status=InitiativeStatus.RUNNING)

    class _Assembly:
        def __init__(self) -> None:
            self.orch = SimpleNamespace(run_queue=None)

        async def create_mission(self, principal, **kwargs):
            calls.append(("create", kwargs))
            return draft

        def bind_repos(self, principal, mission_id, repos):
            calls.append(("bind", {"mission_id": mission_id, "repos": repos}))
            return draft

        async def start(self, principal, mission_id):
            calls.append(("start", {"mission_id": mission_id}))
            return running

    monkeypatch.setattr(mcp_server, "get_mission_service", lambda: _Assembly())
    app = mcp_server.build_mcp_app()
    item = ToolkitItem(kind="mcp", name="sortie-deck", transport="http", url="http://test/mcp")

    async with mcp_server.lifespan_context():
        session = HttpMcpSession(item, http_transport=httpx.ASGITransport(app=app))
        try:
            await session.start()
            created = await session.call_tool("create_mission", {"brief": "加导出按钮"})
            assert created["structuredContent"]["mission_id"] == draft.id

            bound = await session.call_tool(
                "bind_repos",
                {
                    "mission_id": draft.id,
                    "role_repos": {"eng": [{"url": "git@x:repo.git", "branch": "dev"}]},
                },
            )
            assert bound["structuredContent"]["role_repos"]["eng"] == ["git@x:repo.git"]

            started = await session.call_tool("start_mission", {"mission_id": draft.id})
            assert started["structuredContent"]["mission_id"] == running.id
        finally:
            await session.close()

    assert [name for name, _ in calls] == ["create", "bind", "start"]


def test_api_app_mounts_mcp_route():
    """根挂载 + 子应用自带 /mcp 路由：避免 Mount("/mcp") 的 307 重定向。"""
    from sortie_deck import mcp_server
    from sortie_deck.api.app import app as fastapi_app

    mount_paths = [getattr(route, "path", "") for route in fastapi_app.routes]
    assert "/" in mount_paths, "MCP 子应用应挂在根路径"
    inner = [getattr(route, "path", "") for route in mcp_server._sub_app.routes]
    assert "/mcp" in inner, inner
