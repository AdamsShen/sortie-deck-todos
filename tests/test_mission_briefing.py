from __future__ import annotations

import asyncio
from pathlib import Path

from sortie_deck.agents import AgentCatalog
from sortie_deck.harness import (
    EventKind,
    Harness,
    HarnessState,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from sortie_deck.mission_briefing import BriefingAttachment, MissionBriefingAgent
from sortie_deck.models import CreateInitiativeRequest, ExternalMaterial, Initiative
from sortie_deck.settings import Settings


def test_harness_tool_and_confirm():
    reg = ToolRegistry()

    async def ping(_args):
        return ToolResult(ok=True, summary="pong", data={"x": 1})

    reg.register(ToolSpec("ping", "ping", ping))
    h = Harness(reg)
    state = HarnessState(session_id="s1")

    async def _run():
        r = await h.call_tool(state, "ping", {})
        assert r.ok and r.data["x"] == 1
        h.request_confirm(state, title="ok?", plan={"a": 1})
        assert state.status == "awaiting_confirm"
        h.confirm(state, approved=True)
        assert state.context["confirmed"] is True

    asyncio.run(_run())
    assert any(e.kind == EventKind.CONFIRM for e in state.events)


def test_mission_briefing_plan_confirm_create(tmp_path: Path):
    cfg = Settings(
        data_dir=tmp_path / "data",
        pipeline_llm=False,
        llm_cli_fallback=False,
        default_coding_executor="mock",
    )
    created: list[Initiative] = []
    reqs: list[CreateInitiativeRequest] = []

    async def create(req: CreateInitiativeRequest) -> Initiative:
        reqs.append(req)
        ini = Initiative(title=req.title, brief=req.brief, template="default")
        created.append(ini)
        return ini

    agents = AgentCatalog(cfg.agents_db)
    # empty catalog so ensure_missing_roles creates
    agents._write([])  # type: ignore[attr-defined]

    agent = MissionBriefingAgent(
        settings=cfg,
        agents=agents,
        create_initiative=create,
        list_initiatives=lambda: created,
        save_initiative=lambda ini: created.__setitem__(0, ini) if created else None,
    )

    async def _run():
        sess = agent.start(owner_id="usr_ada")
        assert sess.owner_id == "usr_ada"
        sess = await agent.handle(
            sess.id, "门户预发健康检查 iOS Web 后端", user_name="Ada", user_id="usr_ada"
        )
        assert sess.status == "awaiting_confirm"
        assert any(e.kind == EventKind.WATERFALL for e in sess.events)
        assert sess.draft.get("stages")
        sess = await agent.handle(sess.id, "确认", user_name="Ada", user_id="usr_ada")
        assert sess.status == "done"
        assert sess.initiative_id
        assert created and created[0].title
        # 简报建队必须写入归属，否则任务会落进无归属口径
        assert reqs and reqs[0].created_by == "usr_ada"

    asyncio.run(_run())


def test_mission_briefing_multimodal_attachments(tmp_path: Path):
    cfg = Settings(
        data_dir=tmp_path / "data",
        pipeline_llm=False,
        llm_cli_fallback=False,
        default_coding_executor="mock",
    )
    created: list[Initiative] = []

    async def create(req: CreateInitiativeRequest) -> Initiative:
        ini = Initiative(title=req.title, brief=req.brief, template="default")
        created.append(ini)
        return ini

    agent = MissionBriefingAgent(
        settings=cfg,
        agents=AgentCatalog(cfg.agents_db),
        create_initiative=create,
        list_initiatives=lambda: created,
    )

    async def _run():
        sess = agent.start()
        att = BriefingAttachment(
            name="requirements.md",
            mime="text/markdown",
            size=42,
            kind="file",
            text_excerpt="# Portal health\nNeed iOS + Web smoke checks",
        )
        img = BriefingAttachment(
            name="wireframe.png",
            mime="image/png",
            size=120,
            kind="image",
            data_url="data:image/png;base64,aaaa",
        )
        sess = await agent.handle(
            sess.id,
            "按附件做预发检查",
            user_name="Ada",
            attachments=[att, img],
        )
        assert sess.status == "awaiting_confirm"
        user_msgs = [m for m in sess.messages if m.role == "user"]
        assert user_msgs and len(user_msgs[-1].attachments) == 2
        assert "requirements.md" in sess.brief
        assert "Portal health" in sess.brief
        assert any(e.data and e.data.get("step") == "attachments" for e in sess.events)
        assert sess.draft.get("attachments")

    asyncio.run(_run())


def test_mission_briefing_materials_flow(tmp_path: Path):
    """All materials join the planning discussion; on confirm they ride into the
    created mission, while the request brief stays clean (orchestrator folds the
    pinned ones into the persistent-context section)."""
    cfg = Settings(
        data_dir=tmp_path / "data",
        pipeline_llm=False,
        llm_cli_fallback=False,
        default_coding_executor="mock",
    )
    reqs: list[CreateInitiativeRequest] = []

    async def create(req: CreateInitiativeRequest) -> Initiative:
        reqs.append(req)
        return Initiative(title=req.title, brief=req.brief, template="default")

    agent = MissionBriefingAgent(
        settings=cfg,
        agents=AgentCatalog(cfg.agents_db),
        create_initiative=create,
        list_initiatives=lambda: [],
    )

    async def _run():
        sess = agent.start()
        mats = [
            ExternalMaterial(name="PRD.md", kind="prd", pinned=True, text_excerpt="需要 iOS 与 Web 预发健康检查"),
            ExternalMaterial(name="metrics.csv", kind="data", pinned=False, text_excerpt="latency,p99\n120,300"),
        ]
        sess = await agent.handle(sess.id, "做预发健康检查", user_name="Ada", materials=mats)
        assert sess.status == "awaiting_confirm"
        # Materials persist on the session (snapshot refreshed per turn)
        assert [m.name for m in sess.materials] == ["PRD.md", "metrics.csv"]
        assert any(e.data and e.data.get("step") == "materials" for e in sess.events)
        sess = await agent.handle(sess.id, "确认", user_name="Ada")
        assert sess.status == "done"
        assert sess.initiative_id
        # All materials ride into the creation request
        assert reqs and [m.name for m in reqs[0].materials] == ["PRD.md", "metrics.csv"]
        # Request brief stays clean — no discussion-context section leaks in
        assert "【外部资料" not in reqs[0].brief

    asyncio.run(_run())
