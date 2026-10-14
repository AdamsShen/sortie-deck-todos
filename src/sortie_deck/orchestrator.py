from __future__ import annotations

import asyncio
import re
from typing import Any

from langgraph.types import Command

from sortie_deck.agents import AgentCatalog
from sortie_deck.artifacts_s3 import build_artifact_store
from sortie_deck.graph import build_graph_from_template, compile_pipelines
from sortie_deck.manager import plan_digest, plan_digest_matches
from sortie_deck.materials import compose_brief
from sortie_deck.memory import MemoryStore
from sortie_deck.models import (
    CODING_ROLE_VALUES,
    ActorKind,
    ArtifactRef,
    ConfirmPipelineRequest,
    CreateInitiativeRequest,
    ExternalMaterial,
    HarnessLockedError,
    HitlAction,
    HitlDecision,
    HitlRequest,
    Initiative,
    InitiativeStatus,
    PipelineNotConfirmedError,
    RepoConfig,
    RoleAgent,
    RoleId,
    RoomMessage,
    RoomParticipant,
    TimelineEvent,
    UnknownHarnessError,
    UpdatePipelineRequest,
    new_id,
    utc_now,
)
from sortie_deck.org_policy import (
    apply_defaults,
    enforce_track,
    filter_toolkit_ids,
    load_org_policy,
)
from sortie_deck.pipeline import (
    PLANNER_AGENT,
    PLANNER_TITLE,
    apply_proposal_to_meta,
    materialize_template,
    normalize_specs,
    stage_catalog,
)
from sortie_deck.pipeline_llm import plan_pipeline_smart
from sortie_deck.plugins.factory import build_default_registry
from sortie_deck.plugins.queue import ENG_QUEUE
from sortie_deck.repositories import build_initiative_store
from sortie_deck.rooms import RoomStore
from sortie_deck.run_queue import SqliteRunQueue
from sortie_deck.settings import Settings, settings
from sortie_deck.templates import load_persona, load_pipeline_template
from sortie_deck.toolkit import ToolkitStore
from sortie_deck.webhooks import notify_webhook

# Optional shared stores (wired by API lifespan); orchestrator stays usable without them.
_agent_catalog: AgentCatalog | None = None
_memory_store: MemoryStore | None = None
_toolkit_store: ToolkitStore | None = None


def bind_catalogs(
    *,
    agents: AgentCatalog | None = None,
    memory: MemoryStore | None = None,
    toolkit: ToolkitStore | None = None,
) -> None:
    global _agent_catalog, _memory_store, _toolkit_store
    if agents is not None:
        _agent_catalog = agents
    if memory is not None:
        _memory_store = memory
    if toolkit is not None:
        _toolkit_store = toolkit


def mission_guardrails(ini: Initiative) -> Any:
    """Guardrails for one mission (budget + wait graph + causal chain)."""
    from sortie_deck.guardrails import guardrails_from_meta

    return guardrails_from_meta(ini.meta)


def mission_memory_brief(*, initiative_id: str, stage: str, user_id: str | None = None) -> str:
    """Layered memory brief for one stage of one mission.

    Stage-scoped entries (sub-room conclusions) only surface for the stage that
    owns them, so sibling parallel branches are not polluted.
    """
    if _memory_store is None:
        return ""
    try:
        return _memory_store.context_block(
            user_id=user_id,
            mission_id=initiative_id,
            stage=stage,
        )
    except Exception:  # noqa: BLE001 — memory must never break a stage run
        return ""


class Orchestrator:
    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or settings
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = build_initiative_store(
            storage=self.cfg.storage,
            json_path=self.cfg.initiatives_db,
            sqlite_path=self.cfg.initiatives_sqlite,
        )
        self.artifacts = build_artifact_store(self.cfg)
        self.rooms = RoomStore(self.cfg.rooms_dir)
        self.registry = build_default_registry(
            repo_root=self.cfg.data_dir.parent,
            worktrees_root=self.cfg.worktrees_dir,
        )
        self.graph = None
        self.graphs: dict[str, Any] = {}
        self._resources: dict = {}
        self._lock = asyncio.Lock()
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._worker_task: asyncio.Task | None = None
        self.run_queue: SqliteRunQueue | None = None
        if str(self.cfg.run_backend).lower() == "sqlite":
            self.run_queue = SqliteRunQueue(
                self.cfg.runs_sqlite, lease_seconds=self.cfg.run_lease_seconds
            )
        self.org_policy = load_org_policy(self.cfg.org_policy_path)
        ENG_QUEUE.configure(self.cfg.eng_concurrency)

    async def startup(self) -> None:
        self.graphs, self._resources = await compile_pipelines(
            self.registry,
            self.artifacts,
            self.cfg.checkpoint_db,
            postgres_uri=self.cfg.postgres_uri,
        )
        self.graph = self.graphs.get("default")
        for ini in self.store.list():
            if ini.meta.get("pipeline_confirmed") and ini.meta.get("pipeline_specs"):
                self._compile_custom_graph(ini)
        self._wire_subrooms()
        self._wire_decisions()
        if self.run_queue is not None:
            self._worker_task = asyncio.create_task(self._worker_loop())

    def _wire_decisions(self) -> None:
        """Expose the human decision centre (escalation ladder's last resort)."""
        from sortie_deck.decisions import DecisionService, DecisionStore, set_decision_service
        from sortie_deck.notify import NotificationChannel, Notifier

        channels: list[Any] = []
        if self.cfg.dingtalk_webhook_url:
            channels.append(
                NotificationChannel(
                    provider="dingtalk",
                    url=self.cfg.dingtalk_webhook_url,
                    secret=self.cfg.dingtalk_secret,
                    label="dingtalk",
                )
            )
        if self.cfg.feishu_webhook_url:
            channels.append(
                NotificationChannel(
                    provider="feishu", url=self.cfg.feishu_webhook_url, label="feishu"
                )
            )
        enabled = bool(getattr(self.cfg, "im_escalation", False))
        fallback = str(getattr(self.cfg, "decision_fallback_action", "pause") or "pause")
        if fallback not in {"pause", "continue", "abort"}:
            fallback = "pause"
        set_decision_service(
            DecisionService(
                store=DecisionStore(self.cfg.data_dir / "decisions.json"),
                notifier=Notifier(
                    channels=channels, timeout=self.cfg.webhook_timeout, enabled=enabled
                ),
                default_sla_seconds=float(
                    getattr(self.cfg, "decision_sla_seconds", 1800) or 1800
                ),
                default_fallback=fallback,
                enabled=enabled,
            )
        )

    def _wire_subrooms(self) -> None:
        """Expose the sub-room service to stage executors (via the graph host)."""
        from sortie_deck.subroom import build_subroom_service, set_subroom_service

        def _subroom_sink(payload: dict[str, Any]) -> None:
            """Bridge sub-room events onto the initiative SSE stream.

            The service calls this synchronously from inside the discussion
            coroutine, so the async fan-out is scheduled on the running loop
            instead of being awaited here. Best-effort by design: a mission page
            (or its absence) must never be able to break a running stage.
            """
            initiative_id = str(payload.get("initiative_id") or "")
            if not initiative_id:
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # Sync execution (CLI/tests): no subscriber exists, nothing to do.
                return
            task = loop.create_task(self._emit(initiative_id, payload))
            # Pop the result so a delivery error is not reported as an
            # unretrieved task exception.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())

        set_subroom_service(
            build_subroom_service(
                rooms=self.rooms,
                memory=_memory_store,
                path=self.cfg.data_dir / "subrooms.json",
                enabled=bool(getattr(self.cfg, "subroom_enabled", False)),
                max_tokens=int(getattr(self.cfg, "subroom_max_tokens", 20000) or 20000),
                max_rounds=int(getattr(self.cfg, "subroom_max_rounds", 2) or 2),
                timeout_seconds=float(getattr(self.cfg, "subroom_timeout_seconds", 300) or 300),
                guardrails_load=self.guardrails_for_id,
                guardrails_save=self._save_guardrails_for_id,
                on_event=_subroom_sink,
            )
        )

    async def shutdown(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        cm = self._resources.get("cm")
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
        conn = self._resources.get("conn")
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        self._resources = {}

    def _graph_for(self, ini: Initiative) -> Any:
        custom_key = f"custom:{ini.id}"
        if custom_key in self.graphs:
            return self.graphs[custom_key]
        track = (
            (ini.meta or {}).get("pipeline_track")
            or ini.template
            or "default"
        )
        if track == "custom":
            self._compile_custom_graph(ini)
            return self.graphs[custom_key]
        graph = self.graphs.get(str(track)) or self.graphs.get("default")
        if graph is None:
            raise RuntimeError("pipeline graphs not compiled — call startup()")
        return graph

    def _stages_for(self, ini: Initiative) -> list[dict[str, Any]]:
        """The frozen plan stages this initiative is compiled from."""
        meta = ini.meta or {}
        specs = meta.get("pipeline_specs") or []
        if specs and meta.get("pipeline_confirmed"):
            try:
                return list(materialize_template(specs, name=f"custom-{ini.id}")["stages"])
            except Exception:  # noqa: BLE001 — fall back to the track template
                pass
        track = str(meta.get("pipeline_track") or ini.template or "default")
        try:
            return list(load_pipeline_template(track)["stages"])
        except Exception:  # noqa: BLE001 — unknown track must not break resume checks
            return []

    def _assert_plan_unchanged(self, ini: Initiative) -> None:
        """Refuse to resume a run whose plan was edited after it started."""
        if not bool(getattr(self.cfg, "plan_digest_check", True)):
            return
        expected = str((ini.meta or {}).get("plan_digest") or "")
        if not expected:
            return
        stages = self._stages_for(ini)
        if not stages:
            return
        if not plan_digest_matches(expected, stages):
            raise RuntimeError(
                "pipeline changed after this run started — re-confirm the plan before "
                f"resuming (expected {expected}, got {plan_digest(stages)})"
            )

    def guardrails_for(self, ini: Initiative) -> Any:
        """Mission guardrails, seeded from the configured budget on first use."""
        from sortie_deck.guardrails import MissionBudget, guardrails_from_meta

        rails = guardrails_from_meta(ini.meta)
        if not isinstance((ini.meta or {}).get("guardrails"), dict):
            rails.budget = MissionBudget(
                max_tokens=int(
                    getattr(self.cfg, "guardrail_max_tokens", 2_000_000) or 2_000_000
                ),
                max_seconds=float(
                    getattr(self.cfg, "guardrail_max_seconds", 21600) or 21600
                ),
                max_subrooms=int(getattr(self.cfg, "guardrail_max_subrooms", 20) or 20),
                max_handoffs=int(getattr(self.cfg, "guardrail_max_handoffs", 40) or 40),
            )
            self.save_guardrails(ini, rails)
        return rails

    def save_guardrails(self, ini: Initiative, rails: Any) -> None:
        from sortie_deck.guardrails import guardrails_to_meta

        ini.meta = guardrails_to_meta(ini.meta, rails)
        self.store.upsert(ini)

    def guardrails_for_id(self, initiative_id: str) -> Any | None:
        ini = self.store.get(initiative_id)
        if ini is None:
            return None
        return self.guardrails_for(ini)

    def _save_guardrails_for_id(self, initiative_id: str, rails: Any) -> None:
        ini = self.store.get(initiative_id)
        if ini is not None:
            self.save_guardrails(ini, rails)

    def _compile_custom_graph(self, ini: Initiative) -> None:
        specs = ini.meta.get("pipeline_specs") or []
        if not specs:
            raise RuntimeError("no pipeline_specs to compile")
        saver = self._resources.get("saver")
        if saver is None:
            raise RuntimeError("checkpointer not ready")
        tpl = materialize_template(specs, name=f"custom-{ini.id}")
        builder = build_graph_from_template(tpl, self.registry, self.artifacts)
        self.graphs[f"custom:{ini.id}"] = builder.compile(checkpointer=saver)

    def _apply_pipeline_specs(
        self,
        ini: Initiative,
        specs: list[dict[str, Any]],
        *,
        confirmed: bool,
        rationale: str | None = None,
        proposal_extra: dict[str, Any] | None = None,
    ) -> None:
        proposal = {
            "agent": PLANNER_AGENT,
            "agent_title": PLANNER_TITLE,
            "track_hint": (ini.meta or {}).get("pipeline_track") or "custom",
            "rationale": rationale or (ini.meta or {}).get("track_reason") or "",
            "stages": specs,
            "status": "confirmed" if confirmed else "pending",
            **(proposal_extra or {}),
        }
        ini.meta = apply_proposal_to_meta(ini.meta or {}, proposal, confirmed=confirmed)
        if confirmed:
            ini.template = "custom"
            self._compile_custom_graph(ini)

    def subscribe(self, initiative_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(initiative_id, []).append(q)
        return q

    def unsubscribe(self, initiative_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(initiative_id, [])
        if q in subs:
            subs.remove(q)

    async def _emit(self, initiative_id: str, event: dict[str, Any]) -> None:
        for q in list(self._subscribers.get(initiative_id, [])):
            await q.put(event)

    async def _post_room(
        self,
        initiative_id: str,
        *,
        text: str,
        actor_kind: ActorKind,
        actor_name: str,
        actor_id: str | None = None,
        role: str | None = None,
        msg_type: str = "chat",
        meta: dict[str, Any] | None = None,
    ) -> RoomMessage:
        msg = self.rooms.append(
            RoomMessage(
                initiative_id=initiative_id,
                actor_kind=actor_kind,
                actor_id=actor_id or new_id("act_"),
                actor_name=actor_name,
                role=role,
                text=text,
                msg_type=msg_type,  # type: ignore[arg-type]
                meta=meta or {},
            )
        )
        await self._emit(
            initiative_id,
            {"type": "room_message", "message": msg.model_dump(mode="json")},
        )
        return msg

    def _append_timeline(self, ini: Initiative, event: TimelineEvent) -> None:
        ini.timeline.append(event)
        ini.updated_at = utc_now()

    # --- mission knowledge namespace (RAG writeback) ----------------------

    _KB_INDEX_KINDS = frozenset(
        {"prd.md", "tech_design.md", "design.md", "handoff.md", "integrate.md"}
    )

    async def _index_mission_artifacts(self, ini: Initiative) -> None:
        """Best-effort writeback of key stage artifacts into the mission
        knowledge namespace (tags ``ini:<id>``, WeKnora folder per mission).

        Called after artifact sync (per run segment); scans ALL staged refs so
        interrupting stages are covered too. Idempotent via ``meta["kb_indexed"]``;
        failures degrade silently.
        """
        if not ini.artifacts:
            return
        try:
            from sortie_deck.knowledge_gateway import gateway_from_settings

            gateway = gateway_from_settings(self.cfg)
        except Exception:  # noqa: BLE001 — knowledge is an enhancement
            return
        meta = dict(ini.meta or {})
        indexed = list(meta.get("kb_indexed") or [])
        pending = [
            ref
            for ref in ini.artifacts
            if ref.stage not in {"", "intake", "done"}
            and ref.kind in self._KB_INDEX_KINDS
            and f"{ref.stage}:{ref.kind}" not in indexed
        ]
        if not pending:
            return
        try:
            for ref in pending:
                try:
                    content = self.artifacts.read_text(ref.path)
                except Exception:  # noqa: BLE001 — unreadable artifact is skipped
                    continue
                gateway.index_mission_artifact(
                    initiative_id=ini.id,
                    stage=ref.stage,
                    kind=ref.kind,
                    title=f"{ini.title} · {ref.stage} · {ref.kind}",
                    content=content,
                )
                indexed.append(f"{ref.stage}:{ref.kind}")
            meta["kb_indexed"] = indexed
            ini.meta = meta
            self.store.upsert(ini)
        except Exception:  # noqa: BLE001 — knowledge writeback must never break a stage
            pass

    def _sync_artifacts_from_upstream(self, ini: Initiative, upstream: dict[str, str], stage: str) -> None:
        existing = {(a.path) for a in ini.artifacts}
        for kind, rel in upstream.items():
            if rel in existing:
                continue
            # Parallel waves store stage-scoped copies as `{stage}::{filename}`
            display_kind = kind.split("::", 1)[1] if "::" in str(kind) else kind
            inferred = stage
            if "::" in str(kind):
                inferred = str(kind).split("::", 1)[0]
            parts = str(rel).replace("\\", "/").split("/")
            if len(parts) >= 2 and parts[0] == ini.id:
                inferred = parts[1]
            ini.artifacts.append(
                ArtifactRef(kind=display_kind, path=rel, stage=inferred, created_at=utc_now())
            )

    def _roles_from_template(
        self,
        coding_executor: str,
        role_agents: dict[str, str] | None = None,
        role_toolkits: dict[str, list[str]] | None = None,
        template: str = "default",
        role_repos: dict[str, list[RepoConfig]] | None = None,
    ) -> list[RoleAgent]:
        from sortie_deck.llm import LlmClient, resolve_coding_executor

        coding = resolve_coding_executor(coding_executor)
        llm_on = LlmClient(
            cli_fallback=bool(getattr(self.cfg, "llm_cli_fallback", True))
        ).enabled
        tpl = load_pipeline_template(template)
        overrides: dict[RoleId, Any] = {}
        if role_agents and _agent_catalog is not None:
            overrides = _agent_catalog.resolve_slot_map(role_agents)
        roles: list[RoleAgent] = []
        for item in tpl.get("roles", []):
            role = RoleId(item["id"])
            executor = item.get("executor", "mock_product")
            if role == RoleId.PRODUCT and llm_on:
                executor = "llm_product"
            if role.value in CODING_ROLE_VALUES:
                executor = {
                    "mock": "mock_eng",
                    "claude_code": "claude_code",
                    "cursor_cli": "cursor_cli",
                }.get(coding, "mock_eng")
            if role == RoleId.DEPLOY and self.cfg.deploy_executor == "deploy_shell":
                executor = "deploy_shell"
            published = overrides.get(role)
            title = published.title if published else item.get("title", role.value)
            persona = (
                published.persona
                if published
                else load_persona(item.get("persona", role.value))
            )
            # Mission override > agent default toolkit
            toolkit_ids: list[str] = []
            if role_toolkits and role.value in role_toolkits:
                toolkit_ids = list(role_toolkits[role.value])
            elif published and published.toolkit_ids:
                toolkit_ids = list(published.toolkit_ids)
            roles.append(
                RoleAgent(
                    id=new_id(f"{role.value}_"),
                    role=role,
                    title=title,
                    persona=persona,
                    executor=executor,
                    toolkit_ids=toolkit_ids,
                    repos=list((role_repos or {}).get(role.value) or []),
                    catalog_agent_id=published.id if published else None,
                    avatar=(
                        (published.avatar if published and published.avatar else None)
                        or f"preset:{role.value}"
                    ),
                )
            )
        return roles

    def _toolkit_brief(self, toolkit_ids: list[str]) -> str:
        if not toolkit_ids or _toolkit_store is None:
            return ""
        lines: list[str] = []
        for tid in toolkit_ids:
            try:
                item = _toolkit_store.get(tid)
            except KeyError:
                continue
            if not item.enabled:
                continue
            if item.kind == "tool":
                lines.append(f"- [tool/{item.runtime}] {item.name}: {item.endpoint or item.summary}")
            elif item.kind == "mcp":
                target = item.url or item.command
                lines.append(f"- [mcp/{item.transport}] {item.name}: {target}")
            else:
                script_n = len(item.scripts)
                lines.append(
                    f"- [skill] {item.name}: {item.summary or 'skill card'}"
                    + (f" (+{script_n} scripts)" if script_n else "")
                )
        if not lines:
            return ""
        return "Toolkit loadout:\n" + "\n".join(lines)

    def resolve_stage_toolkits(self, ini: Initiative, stage: str, role: str) -> list[str]:
        stage_map = ini.meta.get("stage_toolkits") or {}
        if stage_map.get(stage):
            return list(stage_map[stage])
        for r in ini.roles:
            if r.role.value == role:
                return list(r.toolkit_ids)
        role_map = ini.meta.get("role_toolkits") or {}
        return list(role_map.get(role) or [])

    def _pipeline_loadout(self, ini: Initiative) -> dict[str, Any]:
        role_personas = {r.role.value: r.persona for r in ini.roles}
        role_toolkits = {
            r.role.value: list(r.toolkit_ids)
            for r in ini.roles
            if r.toolkit_ids
        }
        # Prefer explicit meta maps (may include empty overrides)
        meta_roles = ini.meta.get("role_toolkits") or {}
        for slot, ids in meta_roles.items():
            role_toolkits[slot] = list(ids)
        stage_toolkits = {
            stage: list(ids)
            for stage, ids in (ini.meta.get("stage_toolkits") or {}).items()
            if ids
        }
        toolkit_briefs: dict[str, str] = {}
        for slot, ids in role_toolkits.items():
            brief = self._toolkit_brief(ids)
            if brief:
                toolkit_briefs[f"role:{slot}"] = brief
        for stage, ids in stage_toolkits.items():
            brief = self._toolkit_brief(ids)
            if brief:
                toolkit_briefs[f"stage:{stage}"] = brief
        role_repos: dict[str, list[dict[str, Any]]] = {
            r.role.value: [repo.model_dump(mode="json") for repo in r.repos]
            for r in ini.roles
            if r.repos
        }
        meta_repos = ini.meta.get("role_repos") or {}
        for slot, repos in meta_repos.items():
            if repos:
                role_repos[slot] = [RepoConfig.model_validate(r).model_dump(mode="json") for r in repos]
        return {
            "role_personas": role_personas,
            "role_toolkits": role_toolkits,
            "stage_toolkits": stage_toolkits,
            "toolkit_briefs": toolkit_briefs,
            "role_repos": role_repos,
        }

    async def _apply_dynamic_loadout(self, ini: Initiative, loadout: dict[str, Any]) -> None:
        """JIT capability loading: recall → select → probe → merge into the loadout.

        Off by default (``TDT_LOADOUT_AUTO``). Manual pre-binding still wins: the
        dynamic pass only *adds* items a stage asked for by capability.
        """
        if not bool(getattr(self.cfg, "loadout_auto", False)):
            return
        try:
            from sortie_deck.loadout import CapabilityNeed, build_loadout
        except Exception:  # noqa: BLE001 — optional capability
            return

        client = None
        try:
            from sortie_deck.llm import LlmClient

            client = LlmClient(cli_fallback=bool(self.cfg.llm_cli_fallback))
        except Exception:  # noqa: BLE001 — heuristic selection is a valid fallback
            client = None

        stage_toolkits = dict(loadout.get("stage_toolkits") or {})
        briefs = dict(loadout.get("toolkit_briefs") or {})
        plans: list[dict[str, Any]] = []
        for stage in self._stages_for(ini):
            name = str(stage.get("name") or stage.get("id") or "")
            if not name or name == "done":
                continue
            role = str(stage.get("role") or "")
            label = str(stage.get("label") or stage.get("label_en") or "")
            needs = [
                CapabilityNeed(capability=f"{role} {name}", reason="stage role and node id"),
                CapabilityNeed(capability=label or name, reason="stage label"),
            ]
            try:
                plan = await build_loadout(
                    store=_toolkit_store,
                    needs=needs,
                    stage=name,
                    role=role,
                    brief=ini.brief,
                    use_llm=bool(self.cfg.pipeline_llm),
                    client=client,
                )
            except Exception:  # noqa: BLE001 — loadout must never block a start
                continue
            loaded = [c.item_id for c in plan.selected if c.loaded]
            if loaded:
                existing = list(stage_toolkits.get(name) or [])
                stage_toolkits[name] = existing + [i for i in loaded if i not in existing]
            if plan.manifest:
                key = f"stage:{name}"
                briefs[key] = f"{briefs.get(key) or ''}\n{plan.manifest}".strip()
            if plan.selected or plan.rejected:
                plans.append(
                    {
                        "stage": name,
                        "role": role,
                        "planner": plan.planner,
                        "loaded": loaded,
                        "selected": [c.model_dump(mode="json") for c in plan.selected],
                        "rejected": plan.rejected,
                        "manifest": plan.manifest,
                    }
                )
        loadout["stage_toolkits"] = stage_toolkits
        loadout["toolkit_briefs"] = briefs
        loadout["loadout_plans"] = plans
        if plans:
            ini.meta = {**(ini.meta or {}), "loadout_plans": plans}
            self.store.upsert(ini)

    def _seed_participants(self, ini: Initiative, creator_name: str) -> None:
        humans = [
            RoomParticipant(
                name=creator_name, kind=ActorKind.HUMAN, role="owner", discussing=True
            ),
        ]
        agents = [
            RoomParticipant(
                id=r.id,
                name=r.title,
                kind=ActorKind.AGENT,
                role=r.role.value,
                discussing=True,
            )
            for r in ini.roles
        ]
        ini.participants = humans + agents

    async def set_discussing(
        self,
        initiative_id: str,
        *,
        discussing: bool,
        participant_id: str | None = None,
        role: str | None = None,
        name: str | None = None,
        actor_name: str = "Operator",
    ) -> Initiative:
        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)

        target: RoomParticipant | None = None
        if participant_id:
            target = next((p for p in ini.participants if p.id == participant_id), None)
        if target is None and role:
            target = next(
                (
                    p
                    for p in ini.participants
                    if p.kind == ActorKind.AGENT and (p.role or "") == role
                ),
                None,
            )
        if target is None and name:
            target = next(
                (p for p in ini.participants if p.kind == ActorKind.HUMAN and p.name == name),
                None,
            )
        if target is None:
            raise RuntimeError("participant not found")

        if target.discussing == discussing:
            return ini

        target.discussing = discussing
        self.store.upsert(ini)
        verb = "加入" if discussing else "退出"
        who = f"{target.name}" + (f"/{target.role}" if target.role else "")
        await self._post_room(
            ini.id,
            text=f"{who} {verb}了讨论" + (f"（由 {actor_name}）" if actor_name != target.name else "。"),
            actor_kind=ActorKind.SYSTEM,
            actor_name="Sortie",
            msg_type="system",
        )
        await self._emit(ini.id, {"type": "update", "initiative": ini.model_dump(mode="json")})
        return ini

    async def create_initiative(self, req: CreateInitiativeRequest) -> Initiative:
        from sortie_deck.llm import LlmClient, resolve_coding_executor

        # Resolve the mission-level agent harness (user choice → global default)
        harness = self._resolve_harness(req.harness)

        # Apply org defaults / track allowlist before planning
        hint = req.pipeline_track
        if self.org_policy and self.org_policy.default_track and not hint:
            hint = self.org_policy.default_track
        enforce_track(self.org_policy, hint)

        coding = resolve_coding_executor(req.coding_executor)
        llm = LlmClient(cli_fallback=bool(getattr(self.cfg, "llm_cli_fallback", True)))
        proposal = await plan_pipeline_smart(
            title=req.title,
            brief=req.brief,
            hint_track=hint,
            complexity=req.complexity,
            use_llm=bool(self.cfg.pipeline_llm),
            client=llm,
        )
        auto_confirm = bool(req.auto_confirm_pipeline or req.auto_start)
        track_hint = proposal.get("track_hint") or "default"
        enforce_track(self.org_policy, track_hint if track_hint != "custom" else None)
        role_template = track_hint if track_hint in {"express", "standard", "default"} else "default"

        role_toolkits = {k: list(v) for k, v in (req.role_toolkits or {}).items()}
        if _agent_catalog is not None:
            for slot, agent_id in (req.role_agents or {}).items():
                if not agent_id or (role_toolkits.get(slot)):
                    continue
                try:
                    agent = _agent_catalog.get(agent_id)
                except KeyError:
                    continue
                if agent.toolkit_ids:
                    role_toolkits[slot] = list(agent.toolkit_ids)
        role_toolkits = {
            k: filter_toolkit_ids(self.org_policy, v) for k, v in role_toolkits.items()
        }
        stage_toolkits = {
            k: filter_toolkit_ids(self.org_policy, list(v))
            for k, v in (req.stage_toolkits or {}).items()
        }
        role_repos: dict[str, list[RepoConfig]] = {
            slot: [RepoConfig.model_validate(r) for r in repos]
            for slot, repos in (req.role_repos or {}).items()
            if repos
        }

        ini = Initiative(
            title=req.title,
            brief=compose_brief(req.brief, req.materials),
            template=role_template,
            status=InitiativeStatus.DRAFT,
            harness=harness,
            materials=list(req.materials),
            roles=self._roles_from_template(
                coding, req.role_agents, role_toolkits, template=role_template,
                role_repos=role_repos,
            ),
            webhook_url=req.webhook_url,
            meta=apply_defaults(
                self.org_policy,
                {
                    "coding_executor": coding,
                    "harness": harness,
                    "role_agents": req.role_agents or {},
                    # Raw user brief, kept so materials can be re-pinned later
                    # without stacking duplicate context sections.
                    "brief_base": req.brief,
                    "role_repos": {
                        slot: [repo.model_dump(mode="json") for repo in repos]
                        for slot, repos in role_repos.items()
                    },
                    "role_toolkits": role_toolkits,
                    "stage_toolkits": stage_toolkits,
                    "complexity": req.complexity,
                    "planner": proposal.get("planner"),
                    "planner_backend": proposal.get("planner_backend"),
                },
            ),
        )
        ini.meta = apply_proposal_to_meta(ini.meta, proposal, confirmed=auto_confirm)
        if auto_confirm:
            ini.template = "custom"
            self._compile_custom_graph(ini)

        self._seed_participants(ini, req.creator_name)
        # Surface planner as a room participant
        if not any(p.role == PLANNER_AGENT for p in ini.participants):
            ini.participants.append(
                RoomParticipant(
                    id=new_id("agt_"),
                    name=PLANNER_TITLE,
                    kind=ActorKind.AGENT,
                    role=PLANNER_AGENT,
                )
            )

        mem_brief = ""
        if _memory_store is not None:
            agent_ids = list((req.role_agents or {}).values())
            mem_brief = _memory_store.context_block(
                mission_id=ini.id,
                agent_ids=agent_ids,
                limit=8,
            )
            if mem_brief:
                ini.meta["memory_brief"] = mem_brief
        toolkit_lines = []
        for r in ini.roles:
            brief = self._toolkit_brief(r.toolkit_ids)
            if brief:
                toolkit_lines.append(f"**{r.title} ({r.role.value})**\n{brief}")
        toolkit_section = "\n\n".join(toolkit_lines)
        if toolkit_section:
            ini.meta["toolkit_brief"] = toolkit_section

        rail = ini.meta.get("pipeline_rail") or []
        stage_names = " → ".join(s.get("label", s) if isinstance(s, dict) else str(s) for s in rail)
        self._append_timeline(
            ini,
            TimelineEvent(
                stage="intake",
                kind="info",
                message=(
                    f"Squad assembled · planner={PLANNER_AGENT} · "
                    f"confirmed={auto_confirm} · stages {[s.get('id') if isinstance(s, dict) else s for s in rail]}"
                ),
            ),
        )
        self.store.upsert(ini)
        confirm_hint = (
            "航线已自动确认，可 `/start` 出击。"
            if auto_confirm
            else "请确认航线或手动增删改节点后再 `/start` / 点击「确认航线」。"
        )
        await self._post_room(
            ini.id,
            text=(
                f"小队已集结：**{ini.title}**\n"
                f"Brief：{ini.brief}\n\n"
                f"编制：{', '.join(f'{r.title}({r.role.value})' for r in ini.roles)}\n"
                + (f"\n{mem_brief}\n" if mem_brief else "")
                + (f"\n{toolkit_section}\n" if toolkit_section else "")
                + f"\n成员可在此讨论方案。{confirm_hint}\n"
                f"门禁阶段可用 `/approve` `/reject` `/stop`。"
            ),
            actor_kind=ActorKind.SYSTEM,
            actor_name="Sortie",
            msg_type="system",
        )
        await self._post_room(
            ini.id,
            text=(
                f"{proposal.get('rationale')}\n\n"
                f"提案节点：{stage_names}\n"
                f"状态：{'已确认' if auto_confirm else '待确认'}"
            ),
            actor_kind=ActorKind.AGENT,
            actor_name=PLANNER_TITLE,
            role=PLANNER_AGENT,
            msg_type="stage",
        )
        await self._post_room(
            ini.id,
            text=f"{req.creator_name} 加入小队（owner）。",
            actor_kind=ActorKind.SYSTEM,
            actor_name="Sortie",
            msg_type="system",
        )
        await self._emit(ini.id, {"type": "created", "initiative": ini.model_dump(mode="json")})
        await notify_webhook(
            ini.webhook_url,
            "initiative.created",
            {"initiative_id": ini.id, "title": ini.title},
            timeout=self.cfg.webhook_timeout,
        )
        if req.auto_start:
            await self.start_pipeline(ini.id)
            ini = self.get(ini.id) or ini
        return ini

    async def update_pipeline(
        self, initiative_id: str, req: UpdatePipelineRequest, *, author_name: str = "Operator"
    ) -> Initiative:
        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)
        if ini.status != InitiativeStatus.DRAFT:
            raise RuntimeError("pipeline can only be edited while draft")
        specs = normalize_specs([s.model_dump() for s in req.stages])
        self._apply_pipeline_specs(
            ini,
            specs,
            confirmed=bool(req.confirm),
            rationale=f"{author_name} 手动调整了航线节点",
            proposal_extra={"edited_by": author_name, "status": "edited" if not req.confirm else "confirmed"},
        )
        if not req.confirm:
            ini.meta["pipeline_confirmed"] = False
            ini.meta["pipeline_track"] = ini.meta.get("pipeline_proposal", {}).get("track_hint") or "custom"
            # drop compiled custom until confirm
            self.graphs.pop(f"custom:{ini.id}", None)
        self._append_timeline(
            ini,
            TimelineEvent(
                stage="intake",
                kind="info",
                message=f"Pipeline {'confirmed' if req.confirm else 'edited'} by {author_name}",
            ),
        )
        self.store.upsert(ini)
        rail = ini.meta.get("pipeline_rail") or []
        await self._post_room(
            ini.id,
            text=(
                f"{author_name} {'确认' if req.confirm else '编辑'}了航线：\n"
                + " → ".join(s.get("label", s.get("id", "?")) for s in rail)
            ),
            actor_kind=ActorKind.HUMAN,
            actor_name=author_name,
            msg_type="decision",
        )
        await self._emit(ini.id, {"type": "update", "initiative": ini.model_dump(mode="json")})
        return ini

    async def confirm_pipeline(
        self, initiative_id: str, req: ConfirmPipelineRequest | None = None, *, author_name: str = "Operator"
    ) -> Initiative:
        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)
        if ini.status != InitiativeStatus.DRAFT:
            raise RuntimeError("pipeline can only be confirmed while draft")
        author = (req.author_name if req else None) or author_name
        if req and req.stages is not None:
            specs = normalize_specs([s.model_dump() for s in req.stages])
        else:
            specs = normalize_specs(ini.meta.get("pipeline_specs") or [])
        self._apply_pipeline_specs(
            ini,
            specs,
            confirmed=True,
            rationale=f"{author} 确认了航线规划官的提案",
        )
        self._append_timeline(
            ini,
            TimelineEvent(stage="intake", kind="info", message=f"Pipeline confirmed by {author}"),
        )
        self.store.upsert(ini)
        rail = ini.meta.get("pipeline_rail") or []
        await self._post_room(
            ini.id,
            text=(
                f"{author} 确认航线，可开始出击。\n"
                + " → ".join(s.get("label", s.get("id", "?")) for s in rail)
            ),
            actor_kind=ActorKind.HUMAN,
            actor_name=author,
            msg_type="decision",
        )
        await self._emit(ini.id, {"type": "update", "initiative": ini.model_dump(mode="json")})
        return ini

    def list_stage_catalog(self) -> list[dict[str, Any]]:
        return stage_catalog()

    async def create_and_start(self, req: CreateInitiativeRequest) -> Initiative:
        req.auto_start = True
        req.auto_confirm_pipeline = True
        return await self.create_initiative(req)

    # --- agent harness selection & lock ----------------------------------

    def _resolve_harness(self, preferred: str | None) -> str:
        """User choice → TDT_DEFAULT_HARNESS; validates against the provider registry."""
        from sortie_deck.harness_providers import get_harness_registry, is_valid_harness
        from sortie_deck.settings import settings

        harness = (preferred or settings.default_harness or "claude_code").strip()
        if not is_valid_harness(harness) or not get_harness_registry().has(harness):
            raise UnknownHarnessError(f"Unknown harness provider: `{harness}`")
        return harness

    async def update_harness(self, initiative_id: str, harness: str) -> Initiative:
        """Change the mission harness — draft only; started missions are locked."""
        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)
        if ini.harness_locked or ini.status not in {InitiativeStatus.DRAFT, InitiativeStatus.STOPPED}:
            raise HarnessLockedError(
                "Harness is locked once the mission has started — use HITL `switch_harness`"
            )
        target = self._resolve_harness(harness)
        ini.harness = target
        meta = dict(ini.meta or {})
        meta["harness"] = target
        ini.meta = meta
        self.store.upsert(ini)
        await self._post_room(
            ini.id,
            text=f"harness 已切换为 `{target}`（任务未开始，可直接生效）。",
            actor_kind=ActorKind.SYSTEM,
            actor_name="Sortie",
            msg_type="system",
        )
        return ini

    async def switch_harness_via_hitl(
        self, ini: Initiative, decision: HitlDecision
    ) -> Initiative:
        """Mid-run harness change — only reachable through HITL approval."""
        target = self._resolve_harness(decision.harness or "")
        meta = dict(ini.meta or {})
        meta["harness"] = target
        ini.harness = target
        ini.meta = meta
        self._append_timeline(
            ini,
            TimelineEvent(
                stage=ini.current_stage,
                kind="hitl",
                message=f"Harness switched to `{target}` (HITL authorised)",
            ),
        )
        await self._post_room(
            ini.id,
            text=f"HITL 授权：harness 已切换为 `{target}`，后续阶段将在新 harness 中运行。",
            actor_kind=ActorKind.HUMAN,
            actor_name="Sortie",
            msg_type="decision",
        )
        return ini

    def _bind_mission_context_files(self, ini: Initiative) -> None:
        """Persist requirement-bound context files at start (loaded by new sessions)."""
        try:
            from sortie_deck.mission_memory import MissionMemoryStore

            mm = MissionMemoryStore(self.cfg.missions_dir)
            mm.bind_context_file(ini.id, "brief.md", f"# Brief\n\n{ini.brief}")
            rail = ini.meta.get("pipeline_rail") or ini.meta.get("pipeline_stages") or []
            if rail:
                lines = [s.get("label", s) if isinstance(s, dict) else str(s) for s in rail]
                numbered = "\n".join(f"{i + 1}. {label}" for i, label in enumerate(lines))
                mm.bind_context_file(ini.id, "plan.md", f"# Pipeline plan\n\n{numbered}")
            mm.bind_context_file(
                ini.id,
                "harness.md",
                f"# Harness\n\nThis mission runs on harness `{ini.harness}` (locked).",
            )
        except Exception:  # noqa: BLE001 — mission memory must never block start
            pass

    async def start_pipeline(self, initiative_id: str, author_name: str = "Operator") -> Initiative:
        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)
        if not ini.meta.get("pipeline_confirmed"):
            raise PipelineNotConfirmedError()
        if ini.status not in {InitiativeStatus.DRAFT, InitiativeStatus.STOPPED, InitiativeStatus.FAILED}:
            if ini.status == InitiativeStatus.RUNNING:
                return ini
            if ini.status == InitiativeStatus.WAITING_HITL:
                return ini
            if ini.status == InitiativeStatus.DONE:
                raise RuntimeError("Initiative already completed")
        # Ensure custom graph exists for confirmed specs
        if f"custom:{ini.id}" not in self.graphs and ini.meta.get("pipeline_specs"):
            self._compile_custom_graph(ini)
        # Lock the harness for the lifetime of the mission (HITL unlock only).
        if not ini.harness_locked:
            ini.harness_locked = True
            meta = dict(ini.meta or {})
            meta["harness_locked"] = True
            ini.meta = meta
            self._bind_mission_context_files(ini)
        ini.status = InitiativeStatus.RUNNING
        self.store.upsert(ini)
        await self._post_room(
            ini.id,
            text=(
                f"{author_name} 启动了流水线（已确认航线："
                + " → ".join(
                    s.get("label", s) if isinstance(s, dict) else str(s)
                    for s in (ini.meta.get("pipeline_rail") or ini.meta.get("pipeline_stages") or ["intake"])
                )
                + "）。"
            ),
            actor_kind=ActorKind.HUMAN,
            actor_name=author_name,
            msg_type="decision",
        )
        await self._schedule_run(ini.id, kind="start")
        return ini

    async def _schedule_run(
        self,
        initiative_id: str,
        *,
        kind: str = "start",
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.run_queue is not None:
            self.run_queue.enqueue(initiative_id, kind=kind, payload=payload)
            return
        if kind == "resume" and payload and payload.get("decision"):
            cmd = Command(resume=payload["decision"])
            asyncio.create_task(self._run_until_interrupt(initiative_id, resume=cmd))
            return
        asyncio.create_task(self._run_until_interrupt(initiative_id))

    async def _worker_loop(self) -> None:
        assert self.run_queue is not None
        while True:
            try:
                job = self.run_queue.claim(self.cfg.worker_id)
                if not job:
                    await asyncio.sleep(0.4)
                    continue
                try:
                    if job.kind == "resume" and job.payload.get("decision"):
                        cmd = Command(resume=job.payload["decision"])
                        await self._run_until_interrupt(job.initiative_id, resume=cmd)
                    else:
                        await self._run_until_interrupt(job.initiative_id)
                    self.run_queue.complete(job.id)
                except Exception as exc:  # noqa: BLE001
                    self.run_queue.fail(job.id, str(exc))
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)

    def list_initiatives(self) -> list[Initiative]:
        return self.store.list()

    def get(self, initiative_id: str) -> Initiative | None:
        return self.store.get(initiative_id)

    def set_materials(
        self, initiative_id: str, materials: list[ExternalMaterial]
    ) -> Initiative:
        """Replace a mission's external materials; recompose the brief.

        Keeps the raw user brief under ``meta["brief_base"]`` so pinning /
        unpinning never stacks duplicate context sections.
        """
        from sortie_deck.materials import compose_brief, strip_materials_section

        ini = self.store.get(initiative_id)
        if ini is None:
            raise KeyError(initiative_id)
        meta = dict(ini.meta or {})
        base = str(meta.get("brief_base") or strip_materials_section(ini.brief))
        meta["brief_base"] = base
        ini.meta = meta
        ini.materials = list(materials)
        ini.brief = compose_brief(base, ini.materials)
        ini.updated_at = utc_now()
        self.store.upsert(ini)
        return ini

    async def delete_initiative(self, initiative_id: str) -> None:
        if not self.store.get(initiative_id):
            raise KeyError(initiative_id)
        self.store.delete(initiative_id)
        self.rooms.clear(initiative_id)
        self.graphs.pop(f"custom:{initiative_id}", None)

    def list_room_messages(self, initiative_id: str) -> list[RoomMessage]:
        return self.rooms.list(initiative_id)

    async def update_role_avatar(
        self, initiative_id: str, role_id: str, avatar: str, *, author_name: str = "Operator"
    ) -> Initiative:
        """Set avatar for a squad role (preset:… / URL / data URI)."""
        from sortie_deck.avatars import default_avatar_for_role

        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)
        found = False
        cleaned = (avatar or "").strip()
        for r in ini.roles:
            if r.id == role_id or r.role.value == role_id:
                r.avatar = cleaned or default_avatar_for_role(r.role.value)
                found = True
                break
        if not found:
            raise KeyError(role_id)
        self.store.upsert(ini)
        await self._post_room(
            ini.id,
            text=f"{author_name} 更新了编制头像：{role_id}",
            actor_kind=ActorKind.SYSTEM,
            actor_name="Sortie",
            msg_type="system",
            meta={"role_id": role_id, "avatar": cleaned},
        )
        await self._emit(ini.id, {"type": "update", "initiative": ini.model_dump(mode="json")})
        return ini

    async def join_room(self, initiative_id: str, name: str, title: str | None = None) -> Initiative:
        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)
        existing = next((p for p in ini.participants if p.kind == ActorKind.HUMAN and p.name == name), None)
        if not existing:
            ini.participants.append(
                RoomParticipant(
                    name=name, kind=ActorKind.HUMAN, role=title or "member", discussing=True
                )
            )
            self.store.upsert(ini)
            await self._post_room(
                ini.id,
                text=f"{name} 加入了小队" + (f"（{title}）" if title else "。"),
                actor_kind=ActorKind.SYSTEM,
                actor_name="Sortie",
                msg_type="system",
            )
            await self._emit(ini.id, {"type": "update", "initiative": ini.model_dump(mode="json")})
        return ini

    async def post_human_message(
        self,
        initiative_id: str,
        text: str,
        author_name: str = "Operator",
        author_id: str | None = None,
    ) -> tuple[RoomMessage, Initiative | None]:
        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)

        # ensure participant
        if not any(p.kind == ActorKind.HUMAN and p.name == author_name for p in ini.participants):
            ini.participants.append(
                RoomParticipant(id=author_id or new_id("user_"), name=author_name, kind=ActorKind.HUMAN)
            )
            self.store.upsert(ini)

        msg = await self._post_room(
            initiative_id,
            text=text,
            actor_kind=ActorKind.HUMAN,
            actor_name=author_name,
            actor_id=author_id,
            msg_type="chat",
        )

        # Slash commands
        stripped = text.strip()
        updated: Initiative | None = None
        lower = stripped.lower()
        if lower.startswith("/start"):
            try:
                updated = await self.start_pipeline(initiative_id, author_name=author_name)
            except PipelineNotConfirmedError as exc:
                await self._post_room(
                    initiative_id,
                    text=f"无法出击：{exc.message}（code={exc.code}）",
                    actor_kind=ActorKind.SYSTEM,
                    actor_name="Sortie",
                    msg_type="system",
                )
            except RuntimeError as exc:
                await self._post_room(
                    initiative_id,
                    text=f"无法出击：{exc}",
                    actor_kind=ActorKind.SYSTEM,
                    actor_name="Sortie",
                    msg_type="system",
                )
        elif lower.startswith("/confirm"):
            updated = await self.confirm_pipeline(
                initiative_id, ConfirmPipelineRequest(), author_name=author_name
            )
        elif lower.startswith("/approve"):
            note = stripped[8:].strip() or None
            updated = await self.submit_hitl(
                initiative_id,
                HitlDecision(action=HitlAction.APPROVE, note=note),
                author_name=author_name,
            )
        elif lower.startswith("/reject"):
            instruction = stripped[7:].strip() or "Rejected in room"
            updated = await self.submit_hitl(
                initiative_id,
                HitlDecision(action=HitlAction.REJECT, instruction=instruction),
                author_name=author_name,
            )
        elif lower.startswith("/stop"):
            updated = await self.stop(initiative_id, author_name=author_name)
        elif lower.startswith("/rewrite"):
            instruction = stripped[8:].strip()
            updated = await self.submit_hitl(
                initiative_id,
                HitlDecision(action=HitlAction.EDIT_INSTRUCTION, instruction=instruction or "请修订"),
                author_name=author_name,
            )
        else:
            # Agent discussion reply (mention-aware)
            await self._maybe_agent_discuss(ini, text, author_name)

        return msg, updated or self.get(initiative_id)

    async def _maybe_agent_discuss(self, ini: Initiative, text: str, author_name: str) -> None:
        """Lightweight room discussion — only roles opted into discussing reply."""
        mention = re.search(
            r"@(product|design|eng_ios|eng_android|eng_web|eng_backend|eng_agent|eng|qa|deploy|all)\b",
            text,
            re.IGNORECASE,
        )
        target = mention.group(1).lower() if mention else None
        if not target and ini.status != InitiativeStatus.DRAFT:
            return
        if not target:
            target = "product"

        active_roles = {
            (p.role or "")
            for p in ini.participants
            if p.kind == ActorKind.AGENT and p.discussing and p.role
        }
        responders = [r for r in ini.roles if r.role.value in active_roles]
        if target != "all":
            responders = [r for r in responders if r.role.value == target]
        if not responders:
            if mention:
                await self._post_room(
                    ini.id,
                    text=f"`@{target}` 当前未参与讨论（点编制徽章可开关）。",
                    actor_kind=ActorKind.SYSTEM,
                    actor_name="Sortie",
                    msg_type="system",
                )
            return

        for role in responders[:2]:
            reply = await self._discussion_reply(ini, role, text, author_name)
            await self._post_room(
                ini.id,
                text=reply,
                actor_kind=ActorKind.AGENT,
                actor_name=role.title,
                actor_id=role.id,
                role=role.role.value,
                msg_type="chat",
            )

    async def _discussion_reply(
        self, ini: Initiative, role: RoleAgent, text: str, author_name: str
    ) -> str:
        from sortie_deck.room_llm import (
            craft_discussion_reply_llm,
            craft_discussion_reply_template,
        )

        if self.cfg.room_llm:
            memory_block = ""
            if _memory_store is not None:
                try:
                    memory_block = _memory_store.context_block(mission_id=ini.id)
                except Exception:
                    memory_block = ""
            llm_reply = await craft_discussion_reply_llm(
                ini=ini,
                role=role,
                text=text,
                author_name=author_name,
                rooms=self.rooms,
                memory_block=memory_block,
            )
            if llm_reply:
                return llm_reply
        return craft_discussion_reply_template(ini, role, text, author_name)

    def _craft_discussion_reply(
        self, ini: Initiative, role: RoleAgent, text: str, author_name: str
    ) -> str:
        from sortie_deck.room_llm import craft_discussion_reply_template

        return craft_discussion_reply_template(ini, role, text, author_name)

    async def _apply_graph_result(self, ini: Initiative, result: dict[str, Any] | Any) -> Initiative:
        if isinstance(result, dict) and "__interrupt__" in result:
            interrupts = result["__interrupt__"]
            payload = interrupts[0].value if interrupts else {}
            if not isinstance(payload, dict):
                payload = {"prompt": str(payload), "stage": ini.current_stage}
            stage = payload.get("stage") or ini.current_stage
            ini.pending_hitl = HitlRequest(
                stage=stage,
                prompt=payload.get("prompt", "Confirm to continue"),
                allowed_actions=[HitlAction(a) for a in payload.get("allowed_actions", [e.value for e in HitlAction])],
                artifacts=[a for a in ini.artifacts if a.stage == stage],
            )
            ini.status = InitiativeStatus.WAITING_HITL
            ini.current_stage = stage
            self._append_timeline(
                ini,
                TimelineEvent(stage=stage, kind="hitl", message="Waiting for human confirmation", data=payload),
            )
            self.store.upsert(ini)
            kind = payload.get("kind") or "hitl"
            if kind == "permission":
                msg = payload.get("message") or payload.get("prompt", "")
                authish = "login" in msg.lower() or "未登录" in msg or "CURSOR_API_KEY" in msg
                if authish:
                    hitl_text = (
                        f"🔑 编码 Agent 鉴权门禁 · 阶段 `{stage}`\n"
                        f"{msg}\n"
                        f"本机完成登录后回复 `/approve` 重试，`/reject` 放弃，`/stop` 停止。"
                    )
                else:
                    hitl_text = (
                        f"🔐 编码权限门禁 · 阶段 `{stage}`\n"
                        f"{msg}\n"
                        f"回复 `/approve` 以 bypass 权限重试写 worktree，"
                        f"`/reject` 放弃，`/stop` 停止。"
                    )
            else:
                hitl_text = (
                    f"【门禁】阶段 `{stage}` 等待真人确认。\n"
                    f"{payload.get('prompt', '')}\n"
                    f"回复 `/approve` 继续，`/reject 原因` 回退，`/rewrite 指令` 重跑，`/stop` 停止。"
                )
            await self._post_room(
                ini.id,
                text=hitl_text,
                actor_kind=ActorKind.SYSTEM,
                actor_name="Sortie",
                msg_type="hitl",
                meta={"stage": stage, "kind": kind},
            )
            await self._emit(ini.id, {"type": "hitl", "initiative": ini.model_dump(mode="json")})
            await notify_webhook(
                ini.webhook_url,
                "initiative.hitl",
                {"initiative_id": ini.id, "stage": stage},
                timeout=self.cfg.webhook_timeout,
            )
            await self._index_mission_artifacts(ini)
            return ini

        snap = await self._graph_for(ini).aget_state({"configurable": {"thread_id": ini.thread_id}})
        values = snap.values or {}
        if values.get("upstream"):
            self._sync_artifacts_from_upstream(ini, values["upstream"], values.get("stage", ini.current_stage))
            await self._index_mission_artifacts(ini)
        ini.current_stage = values.get("stage", ini.current_stage)
        status = values.get("status", "running")
        if status == "done":
            ini.status = InitiativeStatus.DONE
            ini.pending_hitl = None
        elif status == "stopped":
            ini.status = InitiativeStatus.STOPPED
            ini.pending_hitl = None
        elif snap.next:
            ini.status = InitiativeStatus.RUNNING
        else:
            ini.status = InitiativeStatus.DONE if ini.current_stage == "done" else InitiativeStatus.RUNNING

        existing_msgs = {(e.stage, e.message) for e in ini.timeline}
        for ev in values.get("events") or []:
            key = (ev.get("stage", ""), ev.get("message", ""))
            if key not in existing_msgs:
                data = dict(ev.get("data") or {})
                for k in ("executor", "status", "proposed_next", "qa_attempt"):
                    if k in ev and k not in data:
                        data[k] = ev[k]
                self._append_timeline(
                    ini,
                    TimelineEvent(
                        stage=ev.get("stage", ini.current_stage),
                        role=ev.get("role"),
                        kind=ev.get("kind", "info"),
                        message=ev.get("message", ""),
                        data=data,
                    ),
                )
                role_name = ev.get("role") or "system"
                agent = next((r for r in ini.roles if r.role.value == role_name), None)
                await self._post_room(
                    ini.id,
                    text=f"[{ev.get('stage')}] {ev.get('message', '')}",
                    actor_kind=ActorKind.AGENT if agent else ActorKind.SYSTEM,
                    actor_name=agent.title if agent else "Pipeline",
                    actor_id=agent.id if agent else None,
                    role=role_name if agent else None,
                    msg_type="stage",
                )

        if ini.status == InitiativeStatus.DONE:
            await self._post_room(
                ini.id,
                text="流水线完成。可在产物区查看 PRD / diff / 测试报告 / 部署结果。",
                actor_kind=ActorKind.SYSTEM,
                actor_name="Sortie",
                msg_type="system",
            )

        self.store.upsert(ini)
        await self._emit(ini.id, {"type": "update", "initiative": ini.model_dump(mode="json")})
        return ini

    async def _run_until_interrupt(self, initiative_id: str, resume: Command | None = None) -> Initiative:
        async with self._lock:
            ini = self.store.get(initiative_id)
            if not ini:
                raise KeyError(initiative_id)
            assert self.graph is not None or self.graphs

            config = {"configurable": {"thread_id": ini.thread_id}}
            graph = self._graph_for(ini)
            try:
                if resume is None:
                    loadout = self._pipeline_loadout(ini)
                    await self._apply_dynamic_loadout(ini, loadout)
                    self.guardrails_for(ini)
                    digest = plan_digest(self._stages_for(ini))
                    meta = dict(ini.meta or {})
                    if meta.get("plan_digest") != digest:
                        # Freeze the plan fingerprint for every later resume
                        meta["plan_digest"] = digest
                        ini.meta = meta
                        self.store.upsert(ini)
                    payload: Any = {
                        "initiative_id": ini.id,
                        "thread_id": ini.thread_id,
                        "brief": ini.brief,
                        "title": ini.title,
                        "stage": "intake",
                        "human_instruction": ini.human_instruction,
                        "coding_executor": ini.meta.get("coding_executor", "mock"),
                        "harness": ini.harness or ini.meta.get("harness") or self.cfg.default_harness,
                        "harness_locked": bool(ini.harness_locked),
                        "deploy_executor": self.cfg.deploy_executor,
                        "artifact_root": str(self.cfg.artifacts_dir),
                        "workspace_dir": str(self.cfg.worktrees_dir / ini.id),
                        "upstream": {},
                        "qa_attempt": 1,
                        "events": [],
                        "status": "running",
                        "stopped": False,
                        "pipeline_track": ini.meta.get("pipeline_track", ini.template),
                        "plan_digest": digest,
                        **loadout,
                    }
                else:
                    self._assert_plan_unchanged(ini)
                    payload = resume

                final = None
                async for event in graph.astream(payload, config, stream_mode="values"):
                    final = event
                    stage = event.get("stage")
                    if stage and stage != ini.current_stage:
                        ini.current_stage = stage
                        self.store.upsert(ini)
                        await self._emit(
                            ini.id, {"type": "progress", "stage": ini.current_stage, "initiative_id": ini.id}
                        )
                    if event.get("upstream"):
                        self._sync_artifacts_from_upstream(
                            ini, event["upstream"], event.get("stage", ini.current_stage)
                        )
                        if event.get("stage"):
                            ini.current_stage = event["stage"]
                        self.store.upsert(ini)
                        await self._emit(
                            ini.id, {"type": "progress", "stage": ini.current_stage, "initiative_id": ini.id}
                        )

                snap = await graph.aget_state(config)
                if snap.tasks:
                    for task in snap.tasks:
                        if task.interrupts:
                            interrupt_val = task.interrupts[0].value
                            return await self._apply_graph_result(
                                ini, {"__interrupt__": [type("I", (), {"value": interrupt_val})()]}
                            )
                return await self._apply_graph_result(ini, final or {})
            except Exception as exc:
                ini.status = InitiativeStatus.FAILED
                self._append_timeline(
                    ini,
                    TimelineEvent(stage=ini.current_stage, kind="error", message=str(exc)),
                )
                self.store.upsert(ini)
                await self._post_room(
                    ini.id,
                    text=f"流水线失败：{exc}",
                    actor_kind=ActorKind.SYSTEM,
                    actor_name="Sortie",
                    msg_type="system",
                )
                await self._emit(ini.id, {"type": "error", "message": str(exc)})
                raise

    async def submit_hitl(
        self,
        initiative_id: str,
        decision: HitlDecision,
        author_name: str = "Operator",
    ) -> Initiative:
        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)
        if ini.status != InitiativeStatus.WAITING_HITL:
            raise RuntimeError("Initiative is not waiting for HITL")

        self._append_timeline(
            ini,
            TimelineEvent(
                stage=ini.current_stage,
                kind="hitl",
                message=f"Human action: {decision.action.value}",
                data=decision.model_dump(mode="json"),
            ),
        )
        await self._post_room(
            ini.id,
            text=f"{author_name} → `{decision.action.value}`"
            + (f"：{decision.instruction or decision.note}" if (decision.instruction or decision.note) else ""),
            actor_kind=ActorKind.HUMAN,
            actor_name=author_name,
            msg_type="decision",
        )
        if decision.action == HitlAction.EDIT_INSTRUCTION and decision.instruction:
            ini.human_instruction = decision.instruction
        if decision.action == HitlAction.SWITCH_HARNESS:
            # Only an explicit HITL decision may unlock/switch the harness mid-run.
            ini = await self.switch_harness_via_hitl(ini, decision)
        ini.pending_hitl = None
        ini.status = InitiativeStatus.RUNNING
        self.store.upsert(ini)

        decision_payload = decision.model_dump(mode="json")
        if self.run_queue is not None:
            self.run_queue.enqueue(
                initiative_id, kind="resume", payload={"decision": decision_payload}
            )
            # Wait briefly for worker to progress so API callers still see updates
            for _ in range(50):
                await asyncio.sleep(0.1)
                cur = self.store.get(initiative_id)
                if cur and cur.status != InitiativeStatus.RUNNING:
                    return cur
            return self.store.get(initiative_id) or ini
        cmd = Command(resume=decision_payload)
        return await self._run_until_interrupt(initiative_id, resume=cmd)

    # --- human escalation (last resort) ---------------------------------

    async def escalate_to_human(
        self,
        initiative_id: str,
        question: str,
        *,
        stage: str | None = None,
        context: str = "",
        options: list[str] | None = None,
        assignee: str = "",
        channel: str = "",
        requested_by: str = "manager",
    ) -> Any | None:
        """Hand an undecidable question to a human and pause until they answer.

        Returns the :class:`~sortie_deck.decisions.HumanDecision`, or ``None``
        when IM escalation is disabled (the request is still recorded so the
        mission does not silently swallow it).
        """
        from sortie_deck.decisions import get_decision_service

        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)
        stage_name = stage or ini.current_stage
        service = get_decision_service()
        if service is None or not bool(getattr(self.cfg, "im_escalation", False)):
            self._append_timeline(
                ini,
                TimelineEvent(
                    stage=stage_name,
                    kind="hitl",
                    message=f"escalation requested but IM is disabled: {question}",
                    data={"requested_by": requested_by},
                ),
            )
            self.store.upsert(ini)
            return None

        fallback = str(getattr(self.cfg, "decision_fallback_action", "pause") or "pause")
        if fallback not in {"pause", "continue", "abort"}:
            fallback = "pause"
        decision = await service.raise_decision(
            initiative_id=ini.id,
            stage=stage_name,
            question=question,
            context=context or f"brief: {ini.brief[:400]}",
            options=options,
            assignee=assignee,
            channel=channel,
            sla_seconds=float(getattr(self.cfg, "decision_sla_seconds", 1800) or 1800),
            fallback_action=fallback,
        )
        ini.pending_hitl = HitlRequest(
            stage=stage_name,
            prompt=f"[human-decision {decision.decision_id}] {question}",
            artifacts=[],
        )
        ini.status = InitiativeStatus.WAITING_HITL
        ini.meta = {**(ini.meta or {}), "pending_decision_id": decision.decision_id}
        self._append_timeline(
            ini,
            TimelineEvent(
                stage=stage_name,
                kind="hitl",
                message=f"escalated to a human: {question}",
                data={
                    "decision_id": decision.decision_id,
                    "assignee": assignee,
                    "sla_seconds": decision.sla_seconds,
                    "channels": [n.get("provider") for n in decision.notifications],
                },
            ),
        )
        self.store.upsert(ini)
        await self._post_room(
            ini.id,
            text=f"⏸ 需要真人决策（`{decision.decision_id}`）：{question}",
            actor_kind=ActorKind.SYSTEM,
            actor_name="Sortie",
            msg_type="decision",
        )
        await self._emit(ini.id, {"type": "update", "initiative": ini.model_dump(mode="json")})
        return decision

    async def resolve_decision(
        self,
        decision_id: str,
        *,
        answer: str,
        by: str,
        channel: str = "",
        is_admin: bool = False,
    ) -> Any:
        """Record a human answer and hand it back to the paused mission."""
        from sortie_deck.decisions import get_decision_service

        service = get_decision_service()
        if service is None:
            raise RuntimeError("decision centre is not wired")
        outcome = service.answer(
            decision_id, answer=answer, by=by, channel=channel, is_admin=is_admin
        )
        if not outcome.accepted:
            return outcome

        ini = self.store.get(outcome.decision.initiative_id)
        if ini is None:
            return outcome
        if (ini.meta or {}).get("pending_decision_id") != decision_id:
            return outcome
        ini.meta = {**(ini.meta or {}), "pending_decision_id": None}
        self.store.upsert(ini)
        if ini.status == InitiativeStatus.WAITING_HITL:
            try:
                await self.submit_hitl(
                    ini.id,
                    HitlDecision(
                        action=HitlAction.EDIT_INSTRUCTION,
                        instruction=f"[human-decision {decision_id}] {answer}",
                        note=f"answered by {by}",
                    ),
                    author_name=by,
                )
            except Exception as exc:  # noqa: BLE001 — a reply must not 500 the IM webhook
                self._append_timeline(
                    ini,
                    TimelineEvent(
                        stage=ini.current_stage,
                        kind="error",
                        message=f"decision {decision_id} recorded but resume failed: {exc}",
                        data={"decision_id": decision_id},
                    ),
                )
                ini.status = InitiativeStatus.FAILED
                self.store.upsert(ini)
        return outcome

    async def sweep_decisions(self) -> list[Any]:
        """Expire overdue decisions and apply their documented fallback."""
        from sortie_deck.decisions import get_decision_service

        service = get_decision_service()
        if service is None:
            return []
        expired = service.expire_due()
        for decision in expired:
            ini = self.store.get(decision.initiative_id)
            if ini is None:
                continue
            self._append_timeline(
                ini,
                TimelineEvent(
                    stage=decision.stage,
                    kind="hitl",
                    message=decision.fallback_reason,
                    data={
                        "decision_id": decision.decision_id,
                        "fallback": decision.fallback_action,
                    },
                ),
            )
            if decision.fallback_action == "abort" and ini.status == InitiativeStatus.WAITING_HITL:
                ini.status = InitiativeStatus.STOPPED
                ini.pending_hitl = None
            self.store.upsert(ini)
        return expired

    async def stop(self, initiative_id: str, author_name: str = "Operator") -> Initiative:
        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)
        if ini.status in {InitiativeStatus.STOPPED, InitiativeStatus.DONE}:
            return ini
        if ini.status == InitiativeStatus.WAITING_HITL:
            return await self.submit_hitl(
                initiative_id, HitlDecision(action=HitlAction.STOP), author_name=author_name
            )
        ini.status = InitiativeStatus.STOPPED
        self._append_timeline(
            ini, TimelineEvent(stage=ini.current_stage, kind="info", message="Stopped by user")
        )
        self.store.upsert(ini)
        await self._post_room(
            ini.id,
            text=f"{author_name} 停止了流水线。",
            actor_kind=ActorKind.HUMAN,
            actor_name=author_name,
            msg_type="decision",
        )
        await self._emit(ini.id, {"type": "stopped", "initiative": ini.model_dump(mode="json")})
        return ini


_orch: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orch
    if _orch is None:
        _orch = Orchestrator()
    return _orch
