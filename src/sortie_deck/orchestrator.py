from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from typing import Any

from langgraph.types import Command
from pydantic import ValidationError

from sortie_deck import mission_input
from sortie_deck import observability as obs
from sortie_deck.agents import AgentCatalog
from sortie_deck.artifacts_s3 import build_artifact_store
from sortie_deck.event_log import EventLog
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
    ForgeReviewRequiredError,
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
from sortie_deck.store import InitiativeStore
from sortie_deck.templates import load_persona, load_pipeline_template
from sortie_deck.toolkit import ToolkitStore
from sortie_deck.webhooks import notify_webhook

logger = logging.getLogger(__name__)

# Optional shared stores (wired by API lifespan); orchestrator stays usable without them.
_agent_catalog: AgentCatalog | None = None
_memory_store: MemoryStore | None = None
_toolkit_store: ToolkitStore | None = None
# 当前进程的活动编排器：图内的 host/进度回写按 initiative_id 反查实例，
# 显式构造的 Orchestrator 必须成为这个实例，否则 CLI / 验收脚本里会查到
# 另一个用全局 settings 懒创建的默认实例（读不到任务 → 小群/升级能力静默失效）。
_orch: Orchestrator | None = None


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


def get_memory_store() -> MemoryStore | None:
    """共享记忆库实例（API lifespan 绑定；未绑定时 None，调用方自行兜底）。"""
    return _memory_store


def mission_guardrails(ini: Initiative) -> Any:
    """Guardrails for one mission (budget + wait graph + causal chain)."""
    from sortie_deck.guardrails import guardrails_from_meta

    return guardrails_from_meta(ini.meta)


def mission_memory_brief(
    *,
    initiative_id: str,
    stage: str,
    user_id: str | None = None,
    role: str | None = None,
) -> str:
    """Layered memory brief for one stage of one mission.

    Stage-scoped entries (sub-room conclusions) only surface for the stage that
    owns them, so sibling parallel branches are not polluted.
    ``role``：当前 stage 角色值（如 "eng"），用于把 agent 作用域的角色 lesson
    （反思沉淀，scope_id=角色值）并入候选集。
    """
    if _memory_store is None:
        return ""
    # user 域记忆跟随任务创建者：stage 执行链路没有"当前操作账号"，用 created_by 兜底
    query = None
    try:
        ini = get_orchestrator().get(initiative_id)
        if not user_id:
            user_id = (getattr(ini, "created_by", None) or "") or None
        # 检索打分用任务 brief+title：stage 注入也保持相关性排序
        if query is None:
            query = f"{ini.title}\n{ini.brief}"
        # agent 作用域：已发布角色 id（agt_）+ 角色值（反思沉淀按角色值挂载）
        agent_ids = [r.id for r in (ini.roles or [])]
        if role:
            agent_ids.append(role)
        return _memory_store.context_block(
            user_id=user_id,
            mission_id=initiative_id,
            agent_ids=agent_ids,
            stage=stage,
            query=query,
        )
    except Exception:  # noqa: BLE001 — memory must never break a stage run
        return ""


def set_orchestrator(orch: Orchestrator | None) -> None:
    """绑定/解绑进程级活动编排器（构造时自动绑定，测试可显式清理）。"""
    global _orch
    _orch = orch


_HITL_SOURCE_LABELS = {"web": "网页", "cli": "命令行", "mcp": "Claude Code"}


def hitl_source_label(source: str | None) -> str:
    """审批通道的人话名称：写进时间线与房间消息。"""
    key = (source or "web").strip().lower()
    return _HITL_SOURCE_LABELS.get(key, key or "网页")


class Orchestrator:
    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or settings
        # 显式构造的实例接管为活动编排器（见 _orch 注释）
        set_orchestrator(self)
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = build_initiative_store(
            storage=self.cfg.storage,
            json_path=self.cfg.initiatives_db,
            sqlite_path=self.cfg.initiatives_sqlite,
        )
        self.artifacts = build_artifact_store(self.cfg)
        # 任务目录收敛：聊天记录落 missions/<id>/chat/（layout="mission"）
        self.rooms = RoomStore(self.cfg.missions_dir, layout="mission")
        self.registry = build_default_registry(
            repo_root=self.cfg.data_dir.parent,
            worktrees_root=self.cfg.worktrees_dir,
        )
        self.graph = None
        self.graphs: dict[str, Any] = {}
        self._resources: dict = {}
        # 并发粒度是「mission」而不是「进程」：同一任务必须互斥（否则 start/resume
        # 会双写同一 thread 的 checkpoint），不同任务必须能并行。
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._run_locks_guard = asyncio.Lock()
        # 事件表：SSE 的唯一数据源，worker 进程写、API 进程读，跨进程可见
        self.events = EventLog(self.cfg.events_sqlite)
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._worker_task: asyncio.Task | None = None
        self._sweep_task: asyncio.Task | None = None
        self.run_queue: SqliteRunQueue | None = None
        if str(self.cfg.run_backend).lower() == "sqlite":
            self.run_queue = SqliteRunQueue(
                self.cfg.runs_sqlite, lease_seconds=self.cfg.run_lease_seconds
            )
        self.org_policy = load_org_policy(self.cfg.org_policy_path)
        ENG_QUEUE.configure(self.cfg.eng_concurrency)

    async def startup(self) -> None:
        self._migrate_storage_once()
        self.graphs, self._resources = await compile_pipelines(
            self.registry,
            self.artifacts,
            self.cfg.checkpoint_db,
            postgres_uri=self.cfg.postgres_uri,
            cfg=self.cfg,
        )
        self.graph = self.graphs.get("default")
        for ini in self.store.list():
            if ini.meta.get("pipeline_confirmed") and ini.meta.get("pipeline_specs"):
                self._compile_custom_graph(ini)
        self._wire_subrooms()
        self._wire_decisions()
        # run_worker=False 的进程只做 API/前端；执行交给独立的 `sortie worker` 副本。
        # 否则多副本 API 会各自起一个 worker，抢同一个 job。
        if self.run_queue is not None and self.cfg.run_worker:
            self._worker_task = asyncio.create_task(self._worker_loop())
        self._sweep_task = asyncio.create_task(self._sweep_loop())
        # 启动时先做一次 TTL 兜底扫描（回收上次进程崩溃残留的 workspace）
        self._sweep_worktrees_once()
        self._sweep_orphan_intakes_once()
        self._prune_events_once()

    def _migrate_storage_once(self) -> None:
        """把旧的 initiatives.json 一次性导入 sqlite（幂等）。

        默认后端已是 sqlite；没有这一步，老部署升级后旧任务会从工作台消失。
        迁移前备份 JSON、迁移后写 marker：既保证可回滚（老版本代码仍读 JSON），
        也能在启动时发现「迁移后 JSON 又被写过」——那意味着旧版本进程还在跑。
        """
        json_path = self.cfg.initiatives_db
        marker = self.cfg.data_dir / ".storage-migration.json"
        try:
            if self.store.list():
                self._warn_if_legacy_json_newer(json_path, marker)
                return
        except Exception as exc:  # noqa: BLE001 — 迁移失败不应阻断启动
            logger.warning("检查 sqlite 任务表失败（跳过迁移）: %s", exc)
            return
        if not json_path.exists():
            return
        try:
            legacy = InitiativeStore(json_path).list()
        except Exception as exc:  # noqa: BLE001 — 旧文件损坏不能拖垮启动
            logger.warning("旧 JSON 任务读取失败（跳过迁移）: %s", exc)
            return
        if not legacy:
            return

        import json as _json
        import shutil as _shutil

        backup = json_path.with_name(f"{json_path.name}.bak-{utc_now():%Y%m%d%H%M%S}")
        try:
            _shutil.copy2(json_path, backup)
        except OSError as exc:
            logger.warning("迁移前备份 JSON 失败（继续迁移）: %s", exc)
        for ini in legacy:
            self.store.upsert(ini)
        try:
            marker.write_text(
                _json.dumps(
                    {
                        "migrated_at": utc_now().isoformat(),
                        "source": str(json_path),
                        "count": len(legacy),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("写迁移标记失败（不影响数据）: %s", exc)
        logger.info("旧 JSON 任务已迁移到 sqlite: %s 条（备份 %s）", len(legacy), backup.name)

    def _warn_if_legacy_json_newer(self, json_path: Path, marker: Path) -> None:
        """sqlite 已有数据时，检测 JSON 是否在迁移之后又被写入（新旧版本混跑）。

        混跑会导致两边各写各的：工作台只看到 sqlite 的那一份，JSON 那份永远不出现。
        这里只告警不自动合并，自动合并等于替用户猜谁是权威数据源。
        """
        if not (json_path.exists() and marker.exists()):
            return
        try:
            import json as _json
            from datetime import datetime

            migrated_at = datetime.fromisoformat(
                str(_json.loads(marker.read_text(encoding="utf-8")).get("migrated_at") or "")
            ).timestamp()
        except Exception:  # noqa: BLE001 — marker 损坏只是少一条告警
            return
        if json_path.stat().st_mtime > migrated_at + 1:
            logger.warning(
                "检测到 %s 在迁移到 sqlite 之后仍被写入：可能有旧版本进程同时运行。"
                "请确认只保留一套部署，否则任务数据会分裂在两边。",
                json_path.name,
            )

    def _prune_events_once(self) -> int:
        """按保留期清理事件表（启动与每小时各一次）。"""
        removed = self.events.prune(int(self.cfg.event_retention_days))
        if removed:
            logger.info("事件表清理: %s 条（保留 %s 天）", removed, self.cfg.event_retention_days)
        return removed

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
        # 同一个 Notifier 复用于两处真人触达：决策升级（sub-room escalate）与
        # HITL 门禁通知。门禁只写工作台时，没人盯着页面就会一直卡住。
        self.notifier = Notifier(
            channels=channels, timeout=self.cfg.webhook_timeout, enabled=enabled
        )
        set_decision_service(
            DecisionService(
                store=DecisionStore(self.cfg.data_dir / "decisions.json"),
                notifier=self.notifier,
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
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            self._sweep_task = None
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
        self._run_locks.clear()
        self.events.close()

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
        builder = build_graph_from_template(tpl, self.registry, self.artifacts, cfg=self.cfg)
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

    async def _run_lock_for(self, initiative_id: str) -> asyncio.Lock:
        """取（或创建）某个 mission 的运行锁。

        锁按 mission 划分：同一任务的 start/resume 必须串行，不同任务互不阻塞。
        guard 只保护字典的创建过程，不参与运行期等待。

        这里刻意不做「空闲锁回收」：判断无人使用与有人正要获取之间存在竞态，
        回收会让同一 mission 出现两把锁，互斥直接失效。代价是每个任务常驻一个
        Lock（几百字节），任务被解散时随 delete_initiative 释放。
        """
        async with self._run_locks_guard:
            lock = self._run_locks.get(initiative_id)
            if lock is None:
                lock = asyncio.Lock()
                self._run_locks[initiative_id] = lock
            return lock

    async def _emit(self, initiative_id: str, event: dict[str, Any]) -> None:
        """落事件表 + 扇出到进程内订阅者。

        seq 由事件表分配，是跨进程续读（SSE 重连、worker/API 分进程）的唯一凭据。
        写入失败时 seq<=0，事件退化为仅进程内可见。
        """
        seq = self.events.append(initiative_id, event)
        payload = event if seq <= 0 else {**event, "seq": seq}
        for q in list(self._subscribers.get(initiative_id, [])):
            await q.put(payload)

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
        # 事件与 span 的关联点：前端拿 trace_id 拼 Jaeger/Tempo 深链，从「业务事件」
        # 一跳直达「执行瀑布图」。未启用追踪时 current_trace_id() 返回空串，字段保持默认。
        if not event.trace_id:
            event.trace_id = obs.current_trace_id()
        ini.timeline.append(event)
        ini.updated_at = utc_now()

    async def _merge_state_events(self, ini: Initiative, state_events: list[dict[str, Any]] | None) -> None:
        """把 graph 状态里的 events 增量合并进 timeline（按 stage+message 去重）并广播到任务频道。

        供两处调用：astream 每个执行步（实时把 toolkit_usage / toolkit_meta 等
        事件推给前端）与 _apply_graph_result 的收尾全量合并（去重保证不重复）。
        """
        existing_msgs = {(e.stage, e.message) for e in ini.timeline}
        for ev in state_events or []:
            key = (ev.get("stage", ""), ev.get("message", ""))
            if key in existing_msgs:
                continue
            data = dict(ev.get("data") or {})
            for k in ("executor", "status", "proposed_next", "qa_attempt"):
                if k in ev and k not in data:
                    data[k] = ev[k]
            try:
                event = TimelineEvent(
                    stage=ev.get("stage", ini.current_stage),
                    role=ev.get("role"),
                    kind=ev.get("kind", "info"),
                    message=ev.get("message", ""),
                    data=data,
                )
            except ValidationError:
                # graph 侧若先上线了未登记的 kind，降级为 info 落地：
                # 单个可观测事件不允许打挂整条流水线（会连带任务判 failed）
                logging.getLogger(__name__).warning(
                    "未知 timeline 事件类型已降级: kind=%s stage=%s",
                    ev.get("kind"),
                    ev.get("stage"),
                )
                event = TimelineEvent(
                    stage=ev.get("stage", ini.current_stage),
                    role=ev.get("role"),
                    kind="info",
                    message=ev.get("message", ""),
                    data=data,
                )
            self._append_timeline(ini, event)
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
            # 旧(flat)布局: <ini>/<stage>/<file>；新(mission)布局: <ini>/artifacts/<stage>/<file>。
            # 跳过 ini 段与布局固定段（artifacts/workspace/chat/memory），剩余首段才是 stage。
            if len(parts) >= 2 and parts[0] == ini.id:
                rest = parts[1:]
                if rest and rest[0] in ("artifacts", "workspace", "chat", "memory"):
                    rest = rest[1:]
                if rest:
                    inferred = rest[0]
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
            # brief 摘要参与召回：只有角色名/节点名时词元太短，难以命中
            # 库内以描述性 summary/tags/capabilities 表达的条目
            brief_snippet = (ini.brief or "").strip()[:300]
            needs = [
                CapabilityNeed(capability=f"{role} {name}", reason="stage role and node id"),
                CapabilityNeed(capability=label or name, reason="stage label"),
            ]
            if brief_snippet:
                needs.append(
                    CapabilityNeed(capability=brief_snippet, reason="mission brief summary")
                )
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

    async def create_initiative(
        self, req: CreateInitiativeRequest, *, initiative_id: str | None = None
    ) -> Initiative:
        """创建任务并落库。

        ``initiative_id`` 用于复用规划官会话预生成的任务 id（会话目录
        ``missions/<id>/`` 在会话开启时就已建好，见 PlannerSessionStore），
        为空时按常规生成新 id。
        """
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
            id=initiative_id or new_id("ini_"),
            title=req.title,
            brief=compose_brief(req.brief, req.materials),
            template=role_template,
            status=InitiativeStatus.DRAFT,
            harness=harness,
            # 多租户归属：API 层从登录用户注入，用于 workspace/删除的归属校验
            created_by=getattr(req, "created_by", None),
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
            # Plan-time 检索绑定：按 brief+title 相关性选 top-K，仅注入简介行；
            # 绑定 ids 落 meta 供任务详情页「记忆」面板展示
            mem_entries = _memory_store.context_candidates(
                user_id=ini.created_by,  # 创建者的 user 域个人偏好随任务注入
                mission_id=ini.id,
                agent_ids=agent_ids,
                query=f"{req.title}\n{req.brief}",
                limit=8,
            )
            mem_brief = _memory_store.render_block(mem_entries)
            if mem_entries:
                ini.meta["memory_ids"] = [e.id for e in mem_entries]
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
            # brief 的唯一事实源是 input/brief.md：不再往 memory/context 复制一份，
            # 否则改资料后两份内容会各说各话（历史上就是这样漂移的）。
            mission_input.rebuild_brief(
                Path(self.cfg.data_dir),
                ini.id,
                str((ini.meta or {}).get("brief_base") or ini.brief),
                materials=list(ini.materials or []),
            )
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

    def _pending_forge_drafts(self, ini: Initiative) -> list[str]:
        """本任务建队时生成的、仍待人工审核的 forge 草稿 id。

        只读任务 meta 里记录过的草稿 id（不扫全库），并直读任务目录下的
        forge 存储，避免调用全局单例（其数据目录可能与本 Orchestrator 不同）。
        读取失败视为无待审草稿：审核门禁不能因存储问题把任务永久卡死。
        """
        ids = [str(x) for x in (ini.meta.get("forge_drafts") or []) if str(x).strip()]
        if not ids:
            return []
        try:
            from sortie_deck.agent_forge import ForgeStore

            store = ForgeStore(self.cfg.data_dir / "forge.json")
            return [draft_id for draft_id in ids if store.get(draft_id).status == "pending-review"]
        except Exception:  # noqa: BLE001 — 草稿缺失/损坏时放行，由审核页兜底
            logger.warning("forge 草稿状态读取失败（按无待审处理）: %s", ini.id, exc_info=True)
            return []

    async def start_pipeline(self, initiative_id: str, author_name: str = "Operator") -> Initiative:
        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)
        if not ini.meta.get("pipeline_confirmed"):
            raise PipelineNotConfirmedError()
        # 自动创建 ≠ 自动放行：目录外角色必须先审核注册，否则任务拒绝出击
        if bool(getattr(self.cfg, "forge_require_review", True)):
            pending = self._pending_forge_drafts(ini)
            if pending:
                raise ForgeReviewRequiredError(pending)
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
        # 记忆包落盘：索引 + 单条全文写入任务目录（Agent 执行时按需读取）
        if _memory_store is not None:
            try:
                from sortie_deck.mission_memory import write_memory_pack

                agent_ids = [r.id for r in ini.roles]
                write_memory_pack(
                    _memory_store,
                    initiative_id=ini.id,
                    user_id=ini.created_by,
                    agent_ids=agent_ids,
                )
            except Exception:  # noqa: BLE001 — memory must never block start
                logger.info("任务记忆包写入失败（忽略）: %s", ini.id)
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
            # 入队前把 traceparent 写进 payload：sqlite 后端下 worker 可能是另一个
            # 进程，contextvars 不过去，只能靠 W3C header 续接，否则一条任务会在
            # 队列处断成两条 trace
            merged = dict(payload or {})
            carrier = obs.headers_from_context()
            if carrier.get("traceparent"):
                merged["_traceparent"] = carrier["traceparent"]
            self.run_queue.enqueue(initiative_id, kind=kind, payload=merged or None)
            return
        if kind == "resume" and payload and payload.get("decision"):
            cmd = Command(resume=payload["decision"])
            asyncio.create_task(self._run_until_interrupt(initiative_id, resume=cmd))
            return
        asyncio.create_task(self._run_until_interrupt(initiative_id))

    def _active_mission_ids(self) -> set[str]:
        """运行中/等待门禁的任务 id——TTL 扫描必须跳过它们。"""
        return {
            ini.id
            for ini in self.store.list()
            if ini.status in {InitiativeStatus.RUNNING, InitiativeStatus.WAITING_HITL}
        }

    def _sweep_worktrees_once(self) -> list[str]:
        """执行一次 workspace TTL 兜底扫描（脏状态用延长 TTL），返回清理列表。"""
        from sortie_deck.git_publish import sweep_worktrees

        try:
            cleaned = sweep_worktrees(
                self.cfg.missions_dir,
                self._active_mission_ids(),
                ttl_seconds=self.cfg.worktree_ttl_seconds,
                dirty_ttl_seconds=self.cfg.worktree_dirty_ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — 兜底扫描失败不影响主流程
            logger.warning("workspace TTL sweep 异常: %s", exc)
            return []
        if cleaned:
            logger.info("workspace TTL sweep 清理: %s", cleaned)
        return cleaned

    def _sweep_orphan_intakes_once(self) -> list[str]:
        """回收「规划会话开了但从未建队」的孤儿任务目录。

        这类目录不在任务表里，``delete_initiative`` 永远不会碰到它们；不清理
        会随「随手点了新建任务又放弃」线性增长。
        """
        try:
            from sortie_deck.maintenance import sweep_orphan_missions

            known = {ini.id for ini in self.store.list()}
            return sweep_orphan_missions(
                self.cfg.data_dir,
                known,
                ttl_days=float(getattr(self.cfg, "intake_ttl_days", 7.0)),
            )
        except Exception as exc:  # noqa: BLE001 — 兜底扫描失败不影响主流程
            logger.warning("孤儿 intake 目录扫描异常: %s", exc)
            return []

    async def _sweep_loop(self) -> None:
        """定时维护（每小时一次）：回收非活跃 workspace / 孤儿 intake + 清理过期事件。"""
        while True:
            await asyncio.sleep(3600.0)
            await asyncio.to_thread(self._sweep_worktrees_once)
            await asyncio.to_thread(self._sweep_orphan_intakes_once)
            await asyncio.to_thread(self._prune_events_once)

    def _cleanup_mission_workspace(self, initiative_id: str) -> None:
        """任务终态/删除时回收 workspace 沙盒（含 repos clone）。

        终态（DONE/STOPPED）只删 workspace；delete_initiative 删整个任务目录。
        """
        from sortie_deck.git_publish import cleanup_workspace
        from sortie_deck.layout import mission_workspace_dir

        try:
            cleanup_workspace(mission_workspace_dir(self.cfg.data_dir, initiative_id))
        except Exception as exc:  # noqa: BLE001 — 清理失败不影响任务状态流转
            logger.warning("任务 workspace 清理失败 ini=%s: %s", initiative_id, exc)

    async def _worker_loop(self) -> None:
        assert self.run_queue is not None
        worker_id = self.cfg.resolved_worker_id
        while True:
            try:
                job = self.run_queue.claim(worker_id)
                if not job:
                    await asyncio.sleep(0.4)
                    continue
                # 长任务必须续租：否则超过 run_lease_seconds 后另一个 worker 会
                # 把同一个 job 当成「上一个 worker 崩了」重新领取，导致重复执行。
                heartbeat = asyncio.create_task(self._lease_heartbeat(job.id, worker_id))
                try:
                    # 续接入队时写入的 traceparent，让 worker 里的 span 挂回同一条 trace
                    raw_traceparent = str((job.payload or {}).get("_traceparent") or "")
                    with obs.context_from_carrier(
                        {"traceparent": raw_traceparent} if raw_traceparent else None
                    ):
                        if job.kind == "resume" and job.payload.get("decision"):
                            cmd = Command(resume=job.payload["decision"])
                            await self._run_until_interrupt(job.initiative_id, resume=cmd)
                        else:
                            await self._run_until_interrupt(job.initiative_id)
                    self.run_queue.complete(job.id)
                except Exception as exc:  # noqa: BLE001
                    self.run_queue.fail(job.id, str(exc))
                finally:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)

    async def _lease_heartbeat(
        self, job_id: str, worker_id: str, *, interval: float | None = None
    ) -> None:
        """运行期间周期续租（间隔默认 lease 的 1/3，最小 5s，留两次重试余量）。"""
        if self.run_queue is None:
            return
        if interval is None:
            interval = max(5.0, float(self.cfg.run_lease_seconds) / 3.0)
        step = float(interval)
        while True:
            await asyncio.sleep(step)
            try:
                self.run_queue.heartbeat(job_id, worker_id)
            except Exception as exc:  # noqa: BLE001 — 续租失败只降级告警，不打断执行
                logger.warning("运行租约续期失败 job=%s worker=%s: %s", job_id, worker_id, exc)

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
        # 以 manifest 为准做投影：客户端快照不含 stored_path/sha/抽取状态，
        # 直接覆盖会把已落盘原件的信息抹掉（上传接口是唯一写原件的入口）。
        ini.materials = mission_input.upsert_materials(
            Path(self.cfg.data_dir), initiative_id, materials
        )
        ini.brief = compose_brief(base, ini.materials)
        ini.updated_at = utc_now()
        self.store.upsert(ini)
        # 同步重建 input/brief.md：任务目录里的快照必须与存储里的 brief 一致，
        # 否则新会话 bootstrap 会读到过期版本。
        try:
            mission_input.rebuild_brief(
                Path(self.cfg.data_dir), initiative_id, base, materials=ini.materials
            )
        except Exception:  # noqa: BLE001 — 快照失败不应阻断资料更新
            logger.warning("input/brief.md 重建失败: %s", initiative_id, exc_info=True)
        return ini

    async def delete_initiative(self, initiative_id: str) -> None:
        if not self.store.get(initiative_id):
            raise KeyError(initiative_id)
        self.store.delete(initiative_id)
        self.rooms.clear(initiative_id)
        self.graphs.pop(f"custom:{initiative_id}", None)
        # 事件与运行锁随任务一起回收，否则重建同名任务会读到上一轮的事件
        self.events.delete_mission(initiative_id)
        async with self._run_locks_guard:
            lock = self._run_locks.get(initiative_id)
            # 运行中的锁不能拔：拔掉后下一次 resume 会新建一把，出现两把锁并存
            if lock is None or not lock.locked():
                self._run_locks.pop(initiative_id, None)
        # 任务目录整体回收（memory/artifacts/workspace/chat）——元数据已删，档案不再保留
        import shutil as _shutil

        from sortie_deck.layout import mission_root

        mission_dir = mission_root(self.cfg.data_dir, initiative_id)
        if mission_dir.exists():
            try:
                _shutil.rmtree(mission_dir)
                logger.info("任务目录已删除: %s", mission_dir)
            except OSError as exc:
                logger.warning("任务目录删除失败 %s: %s", mission_dir, exc)

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
            await self._maybe_agent_discuss(ini, text, author_name, author_id=author_id)

        return msg, updated or self.get(initiative_id)

    async def _maybe_agent_discuss(
        self, ini: Initiative, text: str, author_name: str, author_id: str | None = None
    ) -> None:
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
            reply = await self._discussion_reply(ini, role, text, author_name, author_id=author_id)
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
        self, ini: Initiative, role: RoleAgent, text: str, author_name: str,
        author_id: str | None = None,
    ) -> str:
        from sortie_deck.room_llm import (
            craft_discussion_reply_llm,
            craft_discussion_reply_template,
        )

        if self.cfg.room_llm:
            memory_block = ""
            if _memory_store is not None:
                try:
                    # 发言者本人未登录态缺失时退回任务创建者，保证 user 域记忆有归属
                    memory_block = _memory_store.context_block(
                        mission_id=ini.id,
                        user_id=author_id or ini.created_by,
                    )
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

    def _fresh_initiative(self, ini: Initiative) -> Initiative:
        """取存储里的最新任务快照；缺失时退回传入对象。

        运行期间的写者有多个（阶段循环、升级真人、决策中心、API），共享同一个
        JSON/SQLite 存储。用运行开始时的旧对象直接 upsert 会静默覆盖别人刚写入的
        字段（实测会把 `pending_decision_id` 抹掉，导致真人答复后任务不再恢复）。
        """
        fresh = self.store.get(ini.id)
        return fresh if fresh is not None else ini

    async def _apply_graph_result(self, ini: Initiative, result: dict[str, Any] | Any) -> Initiative:
        # 运行期间可能有其它写者（升级真人写 pending_decision_id、API 改 meta、
        # 决策中心回复等）；用最新快照落库，避免把并发写入覆盖丢失。
        ini = self._fresh_initiative(ini)
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
                # 结构化门禁：CLI/MCP 据此能让 agent 复述"在批什么、风险多大"
                artifact_summary=str(payload.get("artifact_summary") or ""),
                risk=str(payload.get("risk") or ""),
                irreversible=bool(payload.get("irreversible") or False),
                action_help={
                    str(k): str(v) for k, v in (payload.get("action_help") or {}).items()
                },
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
            # 全局 IM 通道（钉钉/飞书）：门禁必须真正触达真人，而不是只在工作台里等待。
            # 通道失败由 Notifier 内部降级（返回 ok=False），绝不阻塞门禁本身。
            notifier = getattr(self, "notifier", None)
            if notifier is not None:
                await notifier.send(
                    title=f"【门禁】{ini.title} · {stage}",
                    body=hitl_text,
                    meta={"initiative_id": ini.id, "stage": stage, "kind": kind},
                    level="hitl",
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
            self._cleanup_mission_workspace(ini.id)
        elif status == "stopped":
            ini.status = InitiativeStatus.STOPPED
            ini.pending_hitl = None
            self._cleanup_mission_workspace(ini.id)
        elif snap.next:
            ini.status = InitiativeStatus.RUNNING
        else:
            ini.status = InitiativeStatus.DONE if ini.current_stage == "done" else InitiativeStatus.RUNNING

        await self._merge_state_events(ini, values.get("events") or [])

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

    async def _run_until_interrupt(
        self, initiative_id: str, resume: Command | None = None
    ) -> Initiative:
        """一次 start / resume 对应一条 trace。

        故意不跨越 HITL 人工等待：``interrupt()`` 触发时 astream 正常返回，span
        随之关闭；恢复执行是新的 trace。跨段检索靠 ``sortie.initiative_id``，
        否则一条 span 会挂上几小时的人工等待时长，瀑布图失去参考价值。
        """
        ini = self.store.get(initiative_id)
        attrs: dict[str, Any] = {
            "sortie.initiative_id": initiative_id,
            "sortie.resume": resume is not None,
        }
        if ini is not None:
            attrs["sortie.track"] = str(
                (ini.meta or {}).get("pipeline_track") or getattr(ini, "template", "") or ""
            )
            attrs["sortie.stage_at_entry"] = ini.current_stage
        with obs.span("mission.run", **attrs) as mission_span:
            try:
                return await self._run_until_interrupt_inner(initiative_id, resume)
            finally:
                # 收尾状态在 span 关闭前落属性：done / failed / waiting_hitl 必须能区分，
                # 否则瀑布图上「跑完了」和「停住等人」长得一样
                current = self.store.get(initiative_id)
                if current is not None:
                    obs.set_attributes(mission_span, {"sortie.status": current.status.value})

    async def _run_until_interrupt_inner(
        self, initiative_id: str, resume: Command | None = None
    ) -> Initiative:
        # 同一 mission 的 start/resume 互斥；不同 mission 并行（见 _run_lock_for）
        async with await self._run_lock_for(initiative_id):
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
                        "workspace_dir": str(self.cfg.worktrees_dir / ini.id / "workspace"),
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
                        # 阶段中途可能有其它写者（升级真人写 meta、决策中心回复）；
                        # 先取最新快照再落库，避免用运行开始时的旧对象覆盖
                        ini = self._fresh_initiative(ini)
                        ini.current_stage = stage
                        self.store.upsert(ini)
                        await self._emit(
                            ini.id, {"type": "progress", "stage": ini.current_stage, "initiative_id": ini.id}
                        )
                    if event.get("upstream"):
                        ini = self._fresh_initiative(ini)
                        self._sync_artifacts_from_upstream(
                            ini, event["upstream"], event.get("stage", ini.current_stage)
                        )
                        if event.get("stage"):
                            ini.current_stage = event["stage"]
                        self.store.upsert(ini)
                        await self._emit(
                            ini.id, {"type": "progress", "stage": ini.current_stage, "initiative_id": ini.id}
                        )
                    if event.get("events"):
                        # 每个执行步实时合并事件（toolkit_usage / toolkit_meta 等），
                        # 前端经 SSE update 立即看到该阶段的工具调用
                        ini = self._fresh_initiative(ini)
                        await self._merge_state_events(ini, event["events"])
                        self.store.upsert(ini)
                        await self._emit(
                            ini.id, {"type": "update", "initiative": ini.model_dump(mode="json")}
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
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                # 兜住 SystemExit 等非 Exception 异常：运行环境（如 IDE 的批量删除守卫）
                # 会在清理动作里 raise SystemExit，它是 BaseException，若不在这一层拦住，
                # 会穿过 LangGraph 与 ASGI 直接杀掉进程，任务状态永远停在 running
                # （2026-10-27 事故）。CancelledError 已在上一个分支重新抛出。
                ini.status = InitiativeStatus.FAILED
                # 并行波次里异常可能在别的成员节点冒泡，优先用异常自带的阶段；
                # 否则 timeline 会把 `eng_backend_tech_design` 的失败记到 `qa_cases`。
                failed_stage = str(getattr(exc, "stage", "") or ini.current_stage)
                ini.current_stage = failed_stage
                self._append_timeline(
                    ini,
                    TimelineEvent(stage=failed_stage, kind="error", message=str(exc)),
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
                if isinstance(exc, SystemExit):
                    # 已置 failed 并写入事件，不能再上抛（上抛等于让进程退出）
                    logger.error(
                        "流水线以 SystemExit 终止（已置 failed）: initiative=%s stage=%s",
                        initiative_id,
                        failed_stage,
                    )
                    return ini
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

        # 升级真人时状态会先于图的 interrupt 落库（escalate_to_human 立即置
        # WAITING_HITL），此时用户/钉钉可能已经回复。若该任务的上一段运行仍持有
        # 运行锁，必须等它真正停在门禁后再投递 resume，否则 resume 会打到一个
        # 尚未挂起的图。注意只等这一个 mission 的锁——别的任务在跑与本次提交无关。
        run_lock = await self._run_lock_for(initiative_id)
        if run_lock.locked():
            async with run_lock:
                pass
            ini = self.store.get(initiative_id)
            if ini is None:
                raise KeyError(initiative_id)
            if ini.status != InitiativeStatus.WAITING_HITL:
                # 运行已自行结束或继续：这次 HITL 不再成立，按幂等处理
                logger.info(
                    "HITL 提交已失效（任务状态 %s）: initiative=%s",
                    ini.status,
                    initiative_id,
                )
                return ini

        # 三端（网页 / CLI / MCP）都会走到这里：来源要落到时间线与房间，
        # 否则事后没法回答"这个门禁是谁、从哪批的"
        source_label = hitl_source_label(decision.source)
        actor_label = author_name if decision.source in {"", "web"} else f"{author_name}（{source_label}）"
        self._append_timeline(
            ini,
            TimelineEvent(
                stage=ini.current_stage,
                kind="hitl",
                message=f"Human action: {decision.action.value} via {source_label}",
                data=decision.model_dump(mode="json"),
            ),
        )
        await self._post_room(
            ini.id,
            text=f"{actor_label} → `{decision.action.value}`"
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
        # 记下"谁批的"：另一端并发提交时据此提示"已被处理"，而不是静默冲突
        meta = dict(ini.meta or {})
        meta["last_hitl_by"] = actor_label
        meta["last_hitl_at"] = utc_now().isoformat()
        ini.meta = meta
        self.store.upsert(ini)

        decision_payload = decision.model_dump(mode="json")
        if self.run_queue is not None:
            resume_payload: dict[str, Any] = {"decision": decision_payload}
            carrier = obs.headers_from_context()
            if carrier.get("traceparent"):
                resume_payload["_traceparent"] = carrier["traceparent"]
            self.run_queue.enqueue(initiative_id, kind="resume", payload=resume_payload)
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
            # 决策已被记录，但任务侧没有对应的等待标记（通常是 HITL 已由其它路径
            # 恢复/停止）。记一条告警，避免"答复成功但任务不动"难以排查。
            logger.warning(
                "决策已答复但任务无匹配 pending_decision_id: decision=%s initiative=%s",
                decision_id,
                ini.id,
            )
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

    async def stop(
        self, initiative_id: str, author_name: str = "Operator", *, source: str = "web"
    ) -> Initiative:
        """停止任务。``source`` 记录通道（web/cli/mcp），停在门禁上时随决策一起落库。"""
        ini = self.store.get(initiative_id)
        if not ini:
            raise KeyError(initiative_id)
        if ini.status in {InitiativeStatus.STOPPED, InitiativeStatus.DONE}:
            return ini
        if ini.status == InitiativeStatus.WAITING_HITL:
            return await self.submit_hitl(
                initiative_id,
                HitlDecision(action=HitlAction.STOP, source=source),
                author_name=author_name,
            )
        ini.status = InitiativeStatus.STOPPED
        self._append_timeline(
            ini, TimelineEvent(stage=ini.current_stage, kind="info", message="Stopped by user")
        )
        self.store.upsert(ini)
        self._cleanup_mission_workspace(ini.id)
        await self._post_room(
            ini.id,
            text=f"{author_name} 停止了流水线。",
            actor_kind=ActorKind.HUMAN,
            actor_name=author_name,
            msg_type="decision",
        )
        await self._emit(ini.id, {"type": "stopped", "initiative": ini.model_dump(mode="json")})
        return ini


def get_orchestrator() -> Orchestrator:
    global _orch
    if _orch is None:
        _orch = Orchestrator()
    return _orch
