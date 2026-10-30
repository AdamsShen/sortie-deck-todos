from __future__ import annotations

import asyncio
import json
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
from sortie_deck.mission_briefing import (
    BriefingAttachment,
    MissionBriefingAgent,
    PlannerSessionStore,
)
from sortie_deck.models import CreateInitiativeRequest, ExternalMaterial, Initiative
from sortie_deck.settings import Settings


def _cfg(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        pipeline_llm=False,
        llm_cli_fallback=False,
        default_coding_executor="mock",
    )


def _chat_dir(tmp_path: Path, mission_id: str) -> Path:
    return tmp_path / "data" / "missions" / mission_id / "chat"


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
    cfg = _cfg(tmp_path)
    created: list[Initiative] = []
    reqs: list[CreateInitiativeRequest] = []
    captured: dict[str, str] = {}

    async def create(req: CreateInitiativeRequest, *, initiative_id: str | None = None):
        reqs.append(req)
        ini = Initiative(id=initiative_id or "ini_fallback", title=req.title, brief=req.brief)
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
        # 会话一开启就有任务 id，且目录已建好（不再写全局 briefings.json）
        assert sess.id.startswith("ini_")
        assert (_chat_dir(tmp_path, sess.id) / "planner.state.json").exists()
        assert not (tmp_path / "data" / "briefings.json").exists()

        sess = await agent.handle(
            sess.id, "门户预发健康检查 iOS Web 后端", user_name="Ada", user_id="usr_ada"
        )
        assert sess.status == "awaiting_confirm"
        assert any(e.kind == EventKind.WATERFALL for e in sess.events)
        assert sess.draft.get("stages")
        sess = await agent.handle(sess.id, "确认", user_name="Ada", user_id="usr_ada")
        assert sess.status == "done"
        assert sess.initiative_id == sess.id
        # 建队完成时间要落盘（区分「会话开着」与「已交付」）
        assert sess.committed_at
        assert created and created[0].id == sess.id
        # 简报建队必须写入归属，否则任务会落进无归属口径
        assert reqs and reqs[0].created_by == "usr_ada"
        captured["id"] = sess.id

    asyncio.run(_run())

    # 落盘校验：消息 append 到 jsonl，状态单独存
    chat = _chat_dir(tmp_path, captured["id"])
    lines = (chat / "planner.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 3
    assert all(json.loads(line).get("text") for line in lines)
    state = json.loads((chat / "planner.state.json").read_text(encoding="utf-8"))
    assert state["status"] == "done"
    assert state["owner_id"] == "usr_ada"
    assert "messages" not in state and "materials" not in state


def test_mission_briefing_multimodal_attachments(tmp_path: Path):
    cfg = _cfg(tmp_path)
    created: list[Initiative] = []

    async def create(req: CreateInitiativeRequest, *, initiative_id: str | None = None):
        ini = Initiative(id=initiative_id or "ini_fallback", title=req.title, brief=req.brief)
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
    cfg = _cfg(tmp_path)
    reqs: list[CreateInitiativeRequest] = []
    seen_ids: list[str] = []

    async def create(req: CreateInitiativeRequest, *, initiative_id: str | None = None):
        reqs.append(req)
        seen_ids.append(initiative_id or "")
        return Initiative(id=initiative_id or "ini_fallback", title=req.title, brief=req.brief)

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
        # 资料清单落在任务目录，而不是会话状态里
        manifest = json.loads(
            (tmp_path / "data" / "missions" / sess.id / "input" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        assert [row["name"] for row in manifest["files"]] == ["PRD.md", "metrics.csv"]

        sess = await agent.handle(sess.id, "确认", user_name="Ada")
        assert sess.status == "done"
        assert sess.initiative_id
        # All materials ride into the creation request
        assert reqs and [m.name for m in reqs[0].materials] == ["PRD.md", "metrics.csv"]
        # Request brief stays clean — no discussion-context section leaks in
        assert "【外部资料" not in reqs[0].brief
        # 预生成 id 被复用
        assert seen_ids == [sess.id]

    asyncio.run(_run())


def test_planner_state_reload_keeps_owner_and_messages(tmp_path: Path):
    """进程重启后（新 store 实例）会话仍可续聊，owner 与消息不丢。"""
    cfg = _cfg(tmp_path)
    store = PlannerSessionStore(cfg)
    sess = store.create(owner_id="usr_ada")
    store.save(sess)

    reopened = PlannerSessionStore(cfg).get(sess.id)

    assert reopened.owner_id == "usr_ada"
    assert reopened.id == sess.id


def test_drop_legacy_briefing_store_removes_global_file(tmp_path: Path):
    from sortie_deck.mission_briefing import drop_legacy_briefing_store

    data = tmp_path / "data"
    data.mkdir(parents=True)
    legacy = data / "briefings.json"
    legacy.write_text(json.dumps([{"id": "sess_1"}, {"id": "sess_2"}]), encoding="utf-8")

    assert drop_legacy_briefing_store(data) == 2
    assert not legacy.exists()
    # 幂等：再次调用不报错
    assert drop_legacy_briefing_store(data) == 0
