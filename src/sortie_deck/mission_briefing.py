"""Mission briefing harness — multi-turn chat to plan squad + pipeline.

Replaces the complex create-task form with:
  user chat → waterfall (history / KB / pipeline / loadout / roles) → confirm → create
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from sortie_deck.agent_forge import AgentForge, ForgeRequest
from sortie_deck.agents import AgentCatalog, CreateAgentRequest
from sortie_deck.harness import (
    EventKind,
    Harness,
    HarnessEvent,
    HarnessState,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    new_session_id,
)
from sortie_deck.knowledge_gateway import KnowledgeGateway, gateway_from_settings
from sortie_deck.models import CreateInitiativeRequest, Initiative, RepoConfig, utc_now
from sortie_deck.pipeline import apply_proposal_to_meta
from sortie_deck.settings import Settings
from sortie_deck.settings import settings as default_settings
from sortie_deck.templates import load_persona

CreateFn = Callable[[CreateInitiativeRequest], Awaitable[Initiative]]
ListFn = Callable[[], list[Any]]
SaveFn = Callable[[Initiative], Awaitable[Any] | Any]


class BriefingAttachment(BaseModel):
    name: str
    mime: str = "application/octet-stream"
    size: int = 0
    kind: str = "file"  # image | file
    data_url: str = ""
    text_excerpt: str = ""


class BriefingMessage(BaseModel):
    role: str
    text: str
    attachments: list[BriefingAttachment] = Field(default_factory=list)
    at: str = Field(default_factory=lambda: utc_now().isoformat())


class BriefingSession(BaseModel):
    id: str = Field(default_factory=new_session_id)
    status: str = "planning"
    title: str = ""
    brief: str = ""
    coding_executor: str = "auto"
    messages: list[BriefingMessage] = Field(default_factory=list)
    events: list[HarnessEvent] = Field(default_factory=list)
    draft: dict[str, Any] = Field(default_factory=dict)
    initiative_id: str | None = None
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


_MAX_ATTACH = 6
_MAX_ATTACH_BYTES = 4 * 1024 * 1024
_MAX_DATA_URL_STORE = 1_500_000  # keep chat previewable without huge JSON


def _sanitize_attachments(raw: list[BriefingAttachment] | None) -> list[BriefingAttachment]:
    out: list[BriefingAttachment] = []
    for att in (raw or [])[:_MAX_ATTACH]:
        size = int(att.size or 0)
        data_url = att.data_url or ""
        if size > _MAX_ATTACH_BYTES or len(data_url) > _MAX_DATA_URL_STORE * 2:
            # drop oversized payload; keep metadata for planning context
            data_url = ""
        kind = att.kind if att.kind in {"image", "file"} else (
            "image" if (att.mime or "").startswith("image/") else "file"
        )
        out.append(
            BriefingAttachment(
                name=(att.name or "file")[:180],
                mime=(att.mime or "application/octet-stream")[:120],
                size=size,
                kind=kind,
                data_url=data_url[:_MAX_DATA_URL_STORE] if kind == "image" else "",
                text_excerpt=(att.text_excerpt or "")[:24_000],
            )
        )
    return out


def _compose_brief(text: str, attachments: list[BriefingAttachment]) -> str:
    parts: list[str] = []
    if text.strip():
        parts.append(text.strip())
    if attachments:
        parts.append("【附件】")
        for att in attachments:
            parts.append(f"- {att.name} ({att.kind}, {att.mime}, {att.size} bytes)")
            if att.text_excerpt.strip():
                parts.append(f"```\n{att.text_excerpt.strip()[:8000]}\n```")
            elif att.kind == "image":
                parts.append("  (用户上传图片，请结合任务描述理解需求)")
    return "\n".join(parts).strip()


class BriefingStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[BriefingSession]:
        return [BriefingSession.model_validate(x) for x in json.loads(self.path.read_text(encoding="utf-8"))]

    def _write(self, rows: list[BriefingSession]) -> None:
        self.path.write_text(
            json.dumps([r.model_dump(mode="json") for r in rows], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def create(self) -> BriefingSession:
        sess = BriefingSession()
        with self._lock:
            rows = self._read()
            rows.append(sess)
            self._write(rows)
        return sess

    def get(self, session_id: str) -> BriefingSession:
        for s in self._read():
            if s.id == session_id:
                return s
        raise KeyError(session_id)

    def save(self, sess: BriefingSession) -> BriefingSession:
        sess.updated_at = utc_now().isoformat()
        with self._lock:
            rows = self._read()
            for i, s in enumerate(rows):
                if s.id == sess.id:
                    rows[i] = sess
                    self._write(rows)
                    return sess
            rows.append(sess)
            self._write(rows)
        return sess


_CONFIRM_RE = re.compile(r"^(确认|confirm|/confirm|同意|就这样|开干|组建)\b", re.I)
_REJECT_RE = re.compile(r"^(驳回|reject|/reject|重来|改一下)\b", re.I)

_SLOT_TITLES = {
    "design": "Design Navigator",
    "eng_ios": "iOS Raider",
    "eng_android": "Android Raider",
    "eng_web": "Web Raider",
    "eng_backend": "Backend Raider",
    "eng_agent": "Agent Raider",
    "eng": "Platform Eng Raider",
    "product": "PRD Scout",
    "qa": "QA Gatekeeper",
    "deploy": "Deploy Officer",
}


class MissionBriefingAgent:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        store: BriefingStore | None = None,
        agents: AgentCatalog | None = None,
        knowledge: KnowledgeGateway | None = None,
        forge: AgentForge | None = None,
        create_initiative: CreateFn | None = None,
        list_initiatives: ListFn | None = None,
        save_initiative: SaveFn | None = None,
    ) -> None:
        self.cfg = settings or default_settings
        self.store = store or BriefingStore(self.cfg.data_dir / "briefings.json")
        self.agents = agents
        self.forge = forge
        # Brief of the mission currently being planned — used to seed forge drafts
        self._current_brief = ""
        self.knowledge = knowledge or gateway_from_settings(self.cfg)
        self._create_initiative = create_initiative
        self._list_initiatives = list_initiatives
        self._save_initiative = save_initiative

    def _llm(self) -> LlmClient:
        from sortie_deck.llm import LlmClient

        return LlmClient(cli_fallback=bool(self.cfg.llm_cli_fallback))

    def _coding_executor(self) -> str:
        from sortie_deck.llm import resolve_coding_executor

        return resolve_coding_executor(self.cfg.default_coding_executor)

    def start(self) -> BriefingSession:
        sess = self.store.create()
        coding = self._coding_executor()
        sess.coding_executor = coding
        llm = self._llm()
        planner_hint = (
            f"规划官后端：`{llm.backend}` · 工程执行器：`{coding}`"
            if llm.enabled
            else f"规划官：启发式（无 LLM）· 工程执行器：`{coding}`"
        )
        welcome = (
            "我是建队规划官。用自然语言描述任务目标——我会查阅历史任务与知识库，"
            "由真实 agent 提案航线与小队编制；缺角色时自动创建。确认后才组建小队。\n"
            f"{planner_hint}\n"
            "例：`做一个门户预发健康检查` · 回复 `确认` / `驳回`"
        )
        sess.messages.append(BriefingMessage(role="assistant", text=welcome))
        sess.events.append(
            HarnessEvent(
                kind=EventKind.ASSISTANT,
                text=welcome,
                data={"step": "welcome", "coding_executor": coding, "llm_backend": llm.backend},
            )
        )
        return self.store.save(sess)


    async def handle(
        self,
        session_id: str,
        text: str,
        *,
        user_name: str = "Operator",
        attachments: list[BriefingAttachment] | None = None,
    ) -> BriefingSession:
        sess = self.store.get(session_id)
        text = (text or "").strip()
        atts = _sanitize_attachments(attachments)
        if not text and not atts:
            return sess
        if sess.status == "done":
            sess.messages.append(
                BriefingMessage(role="assistant", text="本会话已组建完成，请到「小队」查看。")
            )
            return self.store.save(sess)

        display_text = text or (f"[附件 ×{len(atts)}]" if atts else "")
        sess.messages.append(BriefingMessage(role="user", text=display_text, attachments=atts))
        harness, state = self._bind(sess)

        async def run_tool(name: str, args: dict[str, Any]) -> ToolResult:
            result = await harness.call_tool(state, name, args)
            if result.ok and result.data:
                if name == "search_history":
                    state.context["history_hits"] = result.data.get("hits") or []
                elif name == "retrieve_knowledge":
                    state.context["knowledge_hits"] = result.data.get("hits") or []
                elif name == "propose_pipeline":
                    state.context["pipeline"] = result.data.get("pipeline")
                    state.context["stages"] = result.data.get("stages") or []
                elif name == "propose_loadout":
                    state.context["needed_slots"] = result.data.get("needed_slots") or []
                    state.context["role_agents"] = result.data.get("role_agents") or {}
                    state.context["loadout_planner"] = result.data.get("loadout_planner") or {}
                    state.context["stages"] = state.context.get("stages") or []
                elif name == "ensure_missing_roles":
                    created = result.data.get("created") or []
                    state.context["created_agents"] = created
                    state.context["forged_agents"] = result.data.get("forged") or []
                    role_agents = dict(state.context.get("role_agents") or {})
                    for c in created:
                        role_agents[c["slot"]] = c["id"]
                    state.context["role_agents"] = role_agents
            return result

        if sess.status == "awaiting_confirm" and _CONFIRM_RE.search(text):
            harness.confirm(state, approved=True, note="用户确认规划")
            await self._commit(sess, state, harness, user_name=user_name)
            return self._persist(sess, state)

        if sess.status == "awaiting_confirm" and _REJECT_RE.search(text):
            harness.confirm(state, approved=False, note="用户驳回")
            sess.status = "planning"
            sess.draft = {}
            sess.brief = ""
            sess.title = ""
            reply = "已驳回。请重新描述任务目标。"
            sess.messages.append(BriefingMessage(role="assistant", text=reply))
            harness.emit(state, EventKind.ASSISTANT, reply)
            return self._persist(sess, state)

        composed = _compose_brief(text, atts)
        sess.title = (text.split("\n", 1)[0] if text else (atts[0].name if atts else "Untitled"))[:48]
        sess.brief = composed
        self._current_brief = composed
        state.status = "planning"
        state.goal = composed
        if atts:
            harness.emit(
                state,
                EventKind.WATERFALL,
                f"⓪ 收到附件 {len(atts)} 个",
                step="attachments",
                names=[a.name for a in atts],
            )

        harness.emit(state, EventKind.WATERFALL, "① 查阅历史任务", step="history")
        await run_tool("search_history", {"query": sess.brief})
        harness.emit(state, EventKind.WATERFALL, "② 检索知识库", step="knowledge")
        await run_tool("retrieve_knowledge", {"query": sess.brief})
        harness.emit(state, EventKind.WATERFALL, "③ 提案交付航线", step="pipeline")
        await run_tool("propose_pipeline", {"title": sess.title, "brief": sess.brief})
        harness.emit(state, EventKind.WATERFALL, "④ 匹配小队编制", step="loadout")
        await run_tool(
            "propose_loadout",
            {"brief": sess.brief, "stages": state.context.get("stages") or []},
        )
        harness.emit(state, EventKind.WATERFALL, "⑤ 补齐缺失角色", step="roles")
        await run_tool("ensure_missing_roles", {"slots": state.context.get("needed_slots") or []})

        draft = {
            "title": sess.title,
            "brief": sess.brief,
            "coding_executor": sess.coding_executor or self._coding_executor(),
            "pipeline": state.context.get("pipeline"),
            "stages": state.context.get("stages"),
            "role_agents": state.context.get("role_agents") or {},
            "history_hits": state.context.get("history_hits") or [],
            "knowledge_hits": state.context.get("knowledge_hits") or [],
            "created_agents": state.context.get("created_agents") or [],
            "forged_agents": state.context.get("forged_agents") or [],
            "attachments": [
                {"name": a.name, "mime": a.mime, "kind": a.kind, "size": a.size} for a in atts
            ],
            "planner": (state.context.get("pipeline") or {}).get("planner"),
            "planner_backend": (state.context.get("pipeline") or {}).get("planner_backend"),
            "loadout_planner": state.context.get("loadout_planner"),
        }
        sess.draft = draft
        harness.request_confirm(
            state,
            title="请确认航线与编制",
            plan={
                "title": draft["title"],
                "stages": [s.get("id") for s in (draft["stages"] or [])],
                "roles": list((draft["role_agents"] or {}).keys()),
                "created_agents": draft["created_agents"],
                "forged_agents": draft.get("forged_agents") or [],
                "attachments": draft["attachments"],
            },
        )
        sess.status = "awaiting_confirm"
        reply = self._format_plan_reply(draft)
        sess.messages.append(BriefingMessage(role="assistant", text=reply))
        harness.emit(state, EventKind.ASSISTANT, reply, step="propose")
        return self._persist(sess, state)

    def _bind(self, sess: BriefingSession) -> tuple[Harness, HarnessState]:
        registry = ToolRegistry()
        registry.register(ToolSpec("search_history", "Search past missions", self._tool_history))
        registry.register(ToolSpec("retrieve_knowledge", "Retrieve KB", self._tool_kb))
        registry.register(ToolSpec("propose_pipeline", "Propose stages", self._tool_pipeline))
        registry.register(ToolSpec("propose_loadout", "Propose loadout", self._tool_loadout))
        registry.register(ToolSpec("ensure_missing_roles", "Create missing agents", self._tool_ensure_roles))
        harness = Harness(registry, name="mission-briefing")
        state = HarnessState(session_id=sess.id, goal=sess.brief or sess.title)
        state.events = list(sess.events)
        state.context = {}
        return harness, state

    def _persist(self, sess: BriefingSession, state: HarnessState) -> BriefingSession:
        sess.events = list(state.events)
        if state.status == "done":
            sess.status = "done"
        elif state.status == "awaiting_confirm":
            sess.status = "awaiting_confirm"
        return self.store.save(sess)

    def set_role_repos(
        self,
        session_id: str,
        role_repos: dict[str, list[dict[str, Any]]],
    ) -> BriefingSession:
        """Attach per-slot git repos to the pending draft (UI edit before confirm).

        Each entry: ``{"url": ..., "branch": ..., "name": ..., "token": ...}``.
        Empty ``url`` rows are dropped; only slots keep non-empty lists.
        """
        sess = self.store.get(session_id)
        clean: dict[str, list[dict[str, Any]]] = {}
        for slot, repos in (role_repos or {}).items():
            rows = []
            for r in repos or []:
                if not isinstance(r, dict):
                    continue
                url = str(r.get("url") or "").strip()
                if not url:
                    continue
                rows.append(
                    {
                        "url": url,
                        "branch": str(r.get("branch") or "").strip(),
                        "name": str(r.get("name") or "").strip(),
                        "token": str(r.get("token") or "").strip() or None,
                    }
                )
            if rows:
                clean[str(slot)] = rows
        sess.draft = {**(sess.draft or {}), "role_repos": clean}
        return self.store.save(sess)

    async def _commit(
        self,
        sess: BriefingSession,
        state: HarnessState,
        harness: Harness,
        *,
        user_name: str,
    ) -> None:
        if not self._create_initiative:
            harness.emit(state, EventKind.ERROR, "create_initiative not wired")
            sess.status = "failed"
            return
        draft = sess.draft or {}
        req = CreateInitiativeRequest(
            title=str(draft.get("title") or sess.title or "Untitled"),
            brief=str(draft.get("brief") or sess.brief),
            coding_executor=str(draft.get("coding_executor") or self._coding_executor()),  # type: ignore[arg-type]
            role_agents=dict(draft.get("role_agents") or {}),
            role_repos={
                slot: [RepoConfig.model_validate(r) for r in repos]
                for slot, repos in (draft.get("role_repos") or {}).items()
                if repos
            },
            creator_name=user_name,
            auto_confirm_pipeline=False,
            auto_start=False,
        )
        ini = await self._create_initiative(req)
        stages = draft.get("stages") or []
        if stages:
            proposal = draft.get("pipeline") or {
                "agent": "pipeline_planner",
                "track_hint": "standard",
                "rationale": "mission briefing harness",
                "stages": stages,
                "status": "pending",
            }
            ini.meta = apply_proposal_to_meta(ini.meta, proposal, confirmed=False)
            if self._save_initiative:
                maybe = self._save_initiative(ini)
                if hasattr(maybe, "__await__"):
                    await maybe  # type: ignore[misc]
        sess.initiative_id = ini.id
        sess.status = "done"
        state.status = "done"
        msg = f"小队已组建：`{ini.id}`。请到「小队」确认航线后出击。"
        sess.messages.append(BriefingMessage(role="assistant", text=msg))
        harness.emit(state, EventKind.DONE, msg, initiative_id=ini.id)
        harness.emit(state, EventKind.WATERFALL, "⑥ 组建完成", step="done", initiative_id=ini.id)

    async def _tool_history(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "").lower()
        hits: list[dict[str, Any]] = []
        if self._list_initiatives:
            try:
                items = self._list_initiatives()
            except Exception as exc:  # noqa: BLE001
                return ToolResult(ok=False, summary=str(exc))
            tokens = [t for t in re.findall(r"[\w\u4e00-\u9fff]{2,}", query)]
            for ini in items[:50]:
                blob = f"{ini.title} {ini.brief}".lower()
                if not tokens or any(t in blob for t in tokens):
                    hits.append(
                        {
                            "id": ini.id,
                            "title": ini.title,
                            "status": getattr(ini.status, "value", str(ini.status)),
                            "brief": (ini.brief or "")[:160],
                        }
                    )
                if len(hits) >= 5:
                    break
        return ToolResult(ok=True, summary=f"历史任务命中 {len(hits)} 条", data={"hits": hits})

    async def _tool_kb(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "")
        try:
            hits = self.knowledge.retrieve(query, limit=4)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, summary=f"知识库检索失败: {exc}")
        return ToolResult(
            ok=True,
            summary=f"知识库命中 {len(hits)} 段",
            data={
                "hits": [
                    {
                        "title": h.title,
                        "score": h.score,
                        "source": h.source,
                        "excerpt": h.content[:240],
                    }
                    for h in hits
                ]
            },
        )

    async def _tool_pipeline(self, args: dict[str, Any]) -> ToolResult:
        from sortie_deck.pipeline_llm import plan_pipeline_smart

        proposal = await plan_pipeline_smart(
            title=str(args.get("title") or ""),
            brief=str(args.get("brief") or ""),
            use_llm=bool(self.cfg.pipeline_llm),
            client=self._llm(),
        )
        stages = proposal.get("stages") or []
        backend = proposal.get("planner_backend") or proposal.get("planner") or "heuristic"
        return ToolResult(
            ok=True,
            summary=f"航线提案 `{proposal.get('track_hint')}` · {len(stages)} 节点 · {backend}",
            data={"pipeline": proposal, "stages": stages},
        )

    async def _tool_loadout(self, args: dict[str, Any]) -> ToolResult:
        from sortie_deck.pipeline_llm import propose_loadout_llm

        stages = args.get("stages") or []
        brief = str(args.get("brief") or "")
        loadout_meta: dict[str, Any] = {"planner": "heuristic"}
        roles: list[str] = []
        if self.cfg.pipeline_llm:
            llm_loadout = await propose_loadout_llm(
                brief=brief, stages=stages if isinstance(stages, list) else [], client=self._llm()
            )
            if llm_loadout:
                roles = list(llm_loadout.get("needed_slots") or [])
                loadout_meta = {
                    "planner": "llm",
                    "planner_backend": llm_loadout.get("planner_backend"),
                    "rationale": llm_loadout.get("rationale") or "",
                }
        if not roles:
            roles = sorted({str(s.get("role") or "eng") for s in stages if isinstance(s, dict)})
            for r in ("product", "qa", "deploy"):
                if r not in roles:
                    roles.append(r)
        role_agents: dict[str, str] = {}
        if self.agents is not None:
            for slot in roles:
                cands = self.agents.list(slot=slot)
                if cands:
                    role_agents[slot] = cands[0].id
        return ToolResult(
            ok=True,
            summary=f"编制位 {len(roles)}：{', '.join(roles)} · {loadout_meta.get('planner')}",
            data={
                "needed_slots": roles,
                "role_agents": role_agents,
                "loadout_planner": loadout_meta,
            },
        )


    async def _tool_ensure_roles(self, args: dict[str, Any]) -> ToolResult:
        slots = [str(s) for s in (args.get("slots") or []) if str(s).strip()]
        created: list[dict[str, str]] = []
        forged: list[dict[str, Any]] = []
        if self.agents is None:
            return ToolResult(ok=True, summary="无角色目录，跳过", data={"created": []})
        from sortie_deck.roles import get_role_registry

        registry = get_role_registry()
        for slot in slots:
            if self.agents.list(slot=slot):
                continue
            title = _SLOT_TITLES.get(slot) or (registry.maybe(slot).title if registry.maybe(slot) else "")
            title = title or f"{slot} specialist"
            try:
                persona = load_persona(slot)
            except Exception:  # noqa: BLE001 — persona is optional
                persona = ""
            tags = [slot, "auto", "briefing"]
            # Unknown role key → forge a pending-review draft instead of faking a persona
            if self.forge is not None and not registry.is_known(slot):
                try:
                    draft = await self.forge.propose(
                        ForgeRequest(
                            capability=f"{slot} role for mission: {(self._current_brief or title)[:400]}",
                            title=title,
                            role_key=slot,
                            requested_by="mission-briefing",
                        )
                    )
                    forged.append(
                        {
                            "slot": slot,
                            "draft_id": draft.id,
                            "role_key": draft.role_key,
                            "issues": draft.issues,
                            "similar": draft.similar,
                        }
                    )
                    title = draft.title or title
                    persona = draft.persona or persona
                    tags = [slot, "forge", "pending-review"]
                except Exception:  # noqa: BLE001 — forge failure falls back to placeholder
                    pass
            if not persona:
                persona = f"You are the fireteam specialist for `{slot}`."
            is_pending = "pending-review" in tags
            agent = self.agents.create(
                CreateAgentRequest(
                    title=title,
                    slot=slot,
                    summary=f"Auto-created by mission briefing (slot={slot})",
                    persona=persona,
                    tags=tags,
                    visibility="public",
                    # Unverified forged personas must be reviewed before going live
                    published=not is_pending,
                ),
                author_id="harness",
                author_name="Mission Briefing",
            )
            created.append({"slot": slot, "id": agent.id, "title": agent.title})
        if created:
            summary = f"新建角色 {len(created)} 个"
            if forged:
                summary += f"（其中 {len(forged)} 个待审核 forge 草稿）"
        else:
            summary = "编制角色齐全"
        return ToolResult(ok=True, summary=summary, data={"created": created, "forged": forged})

    def _format_plan_reply(self, draft: dict[str, Any]) -> str:
        stages = draft.get("stages") or []
        labels = " → ".join(str(s.get("label") or s.get("id")) for s in stages)
        roles = ", ".join((draft.get("role_agents") or {}).keys()) or "(默认模板)"
        hist = draft.get("history_hits") or []
        kb = draft.get("knowledge_hits") or []
        created = draft.get("created_agents") or []
        forged = draft.get("forged_agents") or []
        lines = [
            f"**提案：{draft.get('title') or '未命名'}**",
            "",
            f"航线：{labels or '(空)'}",
            f"编制：{roles}",
        ]
        if hist:
            lines.append(f"历史参考：{', '.join(h['title'] for h in hist[:3])}")
        if kb:
            lines.append(f"知识库：{', '.join(h['title'] for h in kb[:3])}")
        if created:
            lines.append(f"新建角色：{', '.join(c['title'] for c in created)}")
        if forged:
            keys = ", ".join(str(f.get("role_key")) for f in forged)
            lines.append(f"⚙ 新角色草稿待审核（未发布）：{keys}")
        atts = draft.get("attachments") or []
        if atts:
            lines.append(f"附件：{', '.join(a['name'] for a in atts[:5])}")
        planner = draft.get("planner") or (draft.get("pipeline") or {}).get("planner")
        backend = draft.get("planner_backend") or (draft.get("pipeline") or {}).get("planner_backend")
        if planner:
            lines.append(f"规划引擎：{planner}" + (f"/{backend}" if backend else ""))
        coding = draft.get("coding_executor")
        if coding:
            lines.append(f"工程执行器：`{coding}`")
        role_repos = draft.get("role_repos") or {}
        if role_repos:
            lines.append("代码仓库：")
            for slot, repos in role_repos.items():
                for r in repos:
                    branch = r.get("branch") or "默认分支"
                    lines.append(f"- {slot}：`{r.get('url')}` @ {branch}")
        lines.extend(["", "回复 **确认** 组建小队，或 **驳回** 后重述需求。"])
        return "\n".join(lines)
