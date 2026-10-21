"""Generic harness stage executor — routes *every* stage to the mission's harness.

The initiative-locked harness name arrives in ``ctx.harness`` (set by the graph
from the pipeline state). This executor:

1. resolves the provider and probes it (TTL-cached);
2. bootstraps the mission context pack (rolling memory + bound files + RAG);
3. sends one conversation turn on a per-stage session;
4. records the turn into mission memory, auto-summarising over the token budget;
5. degrades to the legacy stage executors (mock / coding CLIs) when the provider
   is unavailable and ``TDT_HARNESS_FALLBACK`` is on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from sortie_deck.models import CODING_ROLE_VALUES, ENG_BUILD_STAGES, StageContext, StageResult

_TOOLKIT_META_PROTOCOL = """Toolkit meta-tool (in-loop capability requests):
If you are missing a capability (tool / MCP / skill), you may request it from the
mission toolkit library instead of inventing one:
1. Ask for candidates: {"toolkit_search": {"query": "<free text>", "kinds": ["tool", "mcp", "skill"]}}
2. The orchestrator replies with candidate summaries in your toolkit brief.
3. To use one, declare: {"toolkit_pick": {"item_id": "<id from candidates>"}}
   Validated items are floated into your context (budget: 8 picks per stage).
   Picked MCP servers become callable via mcp_calls afterwards.
4. To run a script of a picked skill: {"skill_run": {"item_id": "...", "script": "scripts/x.py", "args": []}}
   The item must be picked first; write-looking script names require "confirmed": true.
   Budget: 4 skill runs per stage.
How to declare intents: put the JSON object(s) inside ONE ```toolkit_intent fenced
block anywhere in your reply (a single object or an array of objects):
```toolkit_intent
{"toolkit_search": {"query": "postgres slow log"}}
```
The orchestrator parses the block and fulfils the intents; results arrive in your
next toolkit brief. Omit the block when you need nothing.
Disabled or policy-blocked items are never returned."""

_MEMORY_META_PROTOCOL = """Memory tools (in-loop):
You may search or read the shared memory library mid-stage by declaring intents in
the SAME ```toolkit_intent fenced block:
{"memory_search": {"query": "<free text>"}}   → matching memory brief lines arrive in your next brief
{"memory_read": {"id": "mem_xxx"}}            → full content (truncated) arrives in your next brief
Budget: memory reads are capped per stage — search first, read only what you need."""

# 允许经回复文本解析的意图键（mcp_calls 是既有声明协议，顺路支持真实 harness；
# memory_search/memory_read 由编排层 run_memory_meta 兑现）
_TOOLKIT_INTENT_KEYS = frozenset(
    {"toolkit_search", "toolkit_pick", "skill_run", "mcp_calls", "memory_search", "memory_read"}
)
_TOOLKIT_INTENT_BLOCK_RE = re.compile(r"```toolkit_intent\s*(.*?)```", re.DOTALL)

logger = logging.getLogger(__name__)

# 反思触发下限：回复太短（如纯确认语）没有沉淀价值，跳过 LLM 反思
_REFLECT_MIN_REPLY_CHARS = 200


def _parse_toolkit_intents(text: str) -> dict[str, Any]:
    """从 harness 回复文本提取元工具意图 → 可并入 StageResult.meta 的 dict。

    协议约定：模型把意图 JSON 包在 ```toolkit_intent 围栏块里（单对象或数组）。
    解析失败/无块时返回 {}（无意图 = meta 无键，与旧行为一致，零回归）；
    同类意图多份时后写的覆盖先写的（模型一轮只应声明一次）。
    """
    intents: dict[str, Any] = {}
    for block in _TOOLKIT_INTENT_BLOCK_RE.findall(text or ""):
        try:
            payload = json.loads(block.strip())
        except ValueError:
            continue
        for row in payload if isinstance(payload, list) else [payload]:
            if not isinstance(row, dict):
                continue
            for key in _TOOLKIT_INTENT_KEYS & row.keys():
                intents[key] = row[key]
    return intents


def _toolkit_search_enabled() -> bool:
    """元工具总开关（缺省关闭与否读 settings，测试可 monkeypatch）。"""
    try:
        from sortie_deck.settings import settings

        return bool(getattr(settings, "toolkit_search", False))
    except Exception:  # noqa: BLE001 — settings 不可用时按关闭处理
        return False


def _memory_tools_enabled() -> bool:
    """内置记忆元工具开关（memory_search / memory_read）。"""
    try:
        from sortie_deck.settings import settings

        return bool(getattr(settings, "memory_tools", False))
    except Exception:  # noqa: BLE001 — settings 不可用时按关闭处理
        return False


def _legacy_executor_name(ctx: StageContext) -> str:
    """Pre-harness routing (mirrors graph._executor_name minus harness)."""
    stage = ctx.stage
    if stage == "intake":
        return "mock_intake"
    if stage == "product_prd":
        return "mock_product"
    if stage.endswith("_tech_design"):
        return "mock_tech_design"
    if stage == "design_ui":
        return "mock_design"
    if stage in ENG_BUILD_STAGES:
        return "mock_eng"
    if stage == "eng_selftest" or stage.endswith("_selftest"):
        return "mock_selftest"
    if stage == "eng_integrate":
        return "mock_integrate"
    if stage == "qa_handoff":
        return "mock_handoff"
    if stage == "qa_cases":
        return "mock_qa_cases"
    if stage == "qa_verify":
        return "mock_qa"
    if stage == "deploy_preview":
        return "mock_deploy"
    if str(getattr(ctx.role, "value", ctx.role)) in CODING_ROLE_VALUES:
        return "mock_eng"
    return "mock_intake"


class HarnessStageExecutor:
    name = "harness_stage"

    def probe(self) -> dict[str, str]:
        from sortie_deck.harness_providers.base import get_harness_registry
        from sortie_deck.settings import settings

        harness = settings.default_harness
        status = get_harness_registry().probe(harness)
        return {"status": status.status, "detail": f"({harness}) {status.detail}"}

    async def run(self, ctx: StageContext) -> StageResult:
        from sortie_deck.harness_providers.base import ProbeStatus, get_harness_registry
        from sortie_deck.settings import settings

        harness = (ctx.harness or "").strip() or settings.default_harness
        registry = get_harness_registry()
        probe = ProbeStatus(status="missing", detail=f"harness `{harness}` is not registered")
        if registry.has(harness):
            probe = registry.probe(harness)
        if not probe.ok:
            if settings.harness_fallback:
                return await self._fallback_run(ctx, harness, probe)
            return StageResult(
                status="fail",
                message=f"harness `{harness}` unavailable: {probe.detail}",
                meta={"harness": harness, "probe": probe.as_dict(), "fallback": False},
            )
        return await self._run_harness(ctx, harness)

    # --- degradation ---------------------------------------------------------

    async def _fallback_run(self, ctx: StageContext, harness: str, probe: Any) -> StageResult:
        from sortie_deck.registry import get_registry, run_executor

        legacy_name = _legacy_executor_name(ctx)
        executor = get_registry().get(legacy_name)
        result = await run_executor(executor, ctx)
        result.meta = {
            **(result.meta or {}),
            "harness": harness,
            "harness_fallback": legacy_name,
            "harness_probe": probe.as_dict(),
        }
        if result.status == "fail" and not result.message:
            result.message = f"harness `{harness}` unavailable; legacy executor `{legacy_name}` also failed"
        return result

    # --- main path -------------------------------------------------------------

    async def _run_harness(self, ctx: StageContext, harness: str) -> StageResult:
        from sortie_deck.harness_providers.base import get_harness_registry
        from sortie_deck.mission_memory import build_stage_summary, get_mission_memory
        from sortie_deck.plugins.coding import (
            _count_impl_files,
            clone_repos,
            ensure_worktree,
        )
        from sortie_deck.settings import settings

        workspace = ensure_worktree(
            settings.data_dir.parent, ctx.initiative_id, settings.worktrees_dir
        )
        repo_section, repo_log = await clone_repos(ctx.repos, workspace, ctx.initiative_id)

        pack = await self._build_context_pack(ctx)
        prompt = self._build_prompt(ctx, workspace, pack, repo_section)
        provider = get_harness_registry().get(harness)
        session = await provider.start_session(
            initiative_id=ctx.initiative_id,
            stage=ctx.stage,
            meta={"role": str(getattr(ctx.role, "value", ctx.role))},
        )
        reply = await provider.send(session, prompt, cwd=str(workspace), context_pack=pack)

        mm = get_mission_memory()
        mm.record_turn(
            ctx.initiative_id,
            session.native_id or session.id,
            stage=ctx.stage,
            role=str(getattr(ctx.role, "value", ctx.role)),
            prompt=prompt,
            reply=reply.text,
            provider=harness,
        )
        # 角色分层摘要：协调角色 LLM 提炼决策/结论/风险，执行角色降级为增量流水
        # （替代旧的 reply 前 600 字符无条件流水账，见 docs/plans/2026-10-21-memory-governance.md #2）
        stage_summary = await build_stage_summary(
            stage=ctx.stage,
            role=str(getattr(ctx.role, "value", ctx.role)),
            reply_text=reply.text,
            harness=harness,
        )
        mm.append_memory(ctx.initiative_id, stage_summary)
        await mm.maybe_summarize(ctx.initiative_id)
        # 程序性记忆反思沉淀：fire-and-forget，不阻塞 stage 返回（失败静默）
        if (
            reply.status == "ok"
            and len((reply.text or "").strip()) >= _REFLECT_MIN_REPLY_CHARS
        ):
            asyncio.create_task(self._reflect_to_memory(ctx, reply.text))

        log_lines = [f"harness={harness}", f"session={session.native_id or session.id}"]
        log_lines.extend(repo_log)
        if ctx.toolkit_ids:
            log_lines.append(f"toolkits={','.join(ctx.toolkit_ids)}")
        log_lines.append(f"reply_status={reply.status}")
        log_lines.append((reply.text or "")[:3500])

        needs_hitl = reply.status in {"needs_permission", "needs_auth"}
        ok = reply.status == "ok" and bool(reply.text.strip())
        artifacts, diff = await self._artifacts(ctx, workspace, reply, log_lines, harness=harness)
        meta = {
            "harness": harness,
            "session_id": session.native_id or session.id,
            "provider_reply_status": reply.status,
            "workspace": str(workspace),
            "files_written": _count_impl_files(workspace),
            "context_tokens": int(pack.get("token_estimate") or 0),
        }
        # 意图解析层：回复文本里的 ```toolkit_intent 围栏 → StageResult.meta，
        # 编排层（graph.run_toolkit_meta / _run_mcp_calls）据此兑现
        meta.update(_parse_toolkit_intents(reply.text or ""))
        if needs_hitl:
            return StageResult(
                status="needs_hitl",
                message=(
                    f"harness `{harness}` 需要授权/权限（{reply.status}）。"
                    "请在门禁放行重试，或经 HITL 授权更换 harness。"
                ),
                artifacts=artifacts,
                proposed_next=ctx.stage,
                meta={**meta, "permission_block": True, "auth_block": reply.status == "needs_auth"},
            )
        return StageResult(
            status="ok" if ok else "fail",
            message=f"harness `{harness}` stage finished" if ok else (
                f"harness `{harness}` 未产出有效回复（见 reply.md / implementation.md）"
            ),
            artifacts=artifacts,
            proposed_next=("qa_verify" if ok and str(getattr(ctx.role, "value", ctx.role)) in CODING_ROLE_VALUES else None),
            meta=meta,
        )

    async def _reflect_to_memory(self, ctx: StageContext, reply_text: str) -> None:
        """反思沉淀（程序性记忆）：从本次 stage 经验生成 agent 作用域 lesson。

        业界对应 AgentCore "procedural" / LangMem 反思式自我改进——同角色做同类
        stage 时经 agent 作用域注入链路自动复用。fire-and-forget 调用，任何失败
        静默（记忆是增强不是依赖）；同角色 lesson 超过配置上限时淘汰最旧。
        """
        from sortie_deck.memory import CreateMemoryRequest, enforce_agent_lesson_cap
        from sortie_deck.settings import settings

        role = str(getattr(ctx.role, "value", ctx.role))
        if not settings.memory_reflect_enabled:
            return
        try:
            from sortie_deck.api.app import get_memory
            from sortie_deck.llm import LlmClient

            client = LlmClient(timeout=15.0)
            if not client.enabled:
                return
            lesson = await client.chat_text(
                system=(
                    "你是执行经验沉淀器。根据本次 stage 的角色与回复，提炼一条可复用的"
                    "程序性经验（下次同角色做同类工作时应该怎么做/避免什么）。"
                    "输出一行不超过 120 字的具体经验，不要空话。"
                ),
                user=f"stage={ctx.stage}\nrole={role}\n回复：\n{reply_text[:4000]}",
                temperature=0.1,
            )
            if not lesson.strip():
                return
            store = get_memory()
            entry = store.create(
                CreateMemoryRequest(
                    scope="agent",
                    scope_id=role,
                    kind="lesson",
                    content=lesson.strip(),
                    tags=[f"stage:{ctx.stage}", "procedural"],
                    source="reflection",
                ),
                author_id="system",
                author_name="反思沉淀",
            )
            removed = enforce_agent_lesson_cap(store, settings.memory_reflect_agent_lesson_cap)
            logger.info(
                "反思沉淀完成: initiative=%s stage=%s role=%s entry=%s evicted=%s",
                ctx.initiative_id,
                ctx.stage,
                role,
                entry.id,
                removed,
            )
        except Exception:  # noqa: BLE001 — 反思是增强项，绝不阻塞或污染 stage 结果
            logger.warning(
                "反思沉淀失败(忽略): initiative=%s stage=%s role=%s",
                ctx.initiative_id,
                ctx.stage,
                role,
                exc_info=True,
            )

    # --- context -----------------------------------------------------------------

    async def _build_context_pack(self, ctx: StageContext) -> dict[str, Any]:
        from sortie_deck.mission_memory import get_mission_memory
        from sortie_deck.orchestrator import get_orchestrator

        mm = get_mission_memory()
        ini = None
        artifact_paths: list[str] = []
        try:
            ini = get_orchestrator().get(ctx.initiative_id)
        except Exception:  # noqa: BLE001 — orchestrator optional in unit tests
            ini = None
        if ini is not None:
            artifact_paths = [ref.path for ref in ini.artifacts][-40:]
        pack = await mm.bootstrap_context(ini, artifact_paths=artifact_paths) if ini is not None else None
        out: dict[str, Any] = {
            "memory_md": pack.memory_md if pack else "",
            "context_files": dict(pack.context_files) if pack else {},
            "artifact_index": list(pack.artifact_index) if pack else [],
            "token_estimate": pack.token_estimate if pack else 0,
            "rag_brief": self._rag_brief(ctx),
        }
        return out

    def _rag_brief(self, ctx: StageContext) -> str:
        try:
            from sortie_deck.knowledge_gateway import gateway_from_settings
            from sortie_deck.settings import settings

            gateway = gateway_from_settings(settings)
            # Mission namespace first (this requirement's knowledge), then global.
            hits = gateway.retrieve(
                f"{ctx.brief} {ctx.stage}", limit=6, initiative_id=ctx.initiative_id
            )
            if not hits:
                return ""
            lines = []
            for h in hits:
                marker = "mission" if (h.meta or {}).get("mission") else h.source
                lines.append(f"- [{marker}] {h.title}: {h.content[:300]}")
            return "Knowledge brief:\n" + "\n".join(lines)
        except Exception:  # noqa: BLE001 — RAG is an enhancement
            return ""

    def _build_prompt(
        self,
        ctx: StageContext,
        workspace: Path,
        pack: dict[str, Any],
        repo_section: str,
    ) -> str:
        prd = ctx.upstream_artifacts.get("prd.md", ctx.brief)
        sections: list[str] = [
            ctx.persona or f"You are the {ctx.role} agent in Sortie Deck.",
            (
                "You are running inside a Sortie Deck mission. Complete THIS stage and "
                "produce the stage artifact. Do not ask the user questions — decide and act."
            ),
            f"Stage: `{ctx.stage}` — Role: `{getattr(ctx.role, 'value', ctx.role)}`",
            f"Workspace (cwd): `{workspace}` — you may create/edit files here.",
            f"Human instruction: {ctx.human_instruction or 'none'}",
            f"Mission brief:\n{ctx.brief}",
            f"Upstream PRD:\n{prd}",
        ]
        memory_md = str(pack.get("memory_md") or "")
        if memory_md.strip():
            sections.append(f"Rolling mission memory (from bound files):\n{memory_md.strip()}")
        context_files = pack.get("context_files") or {}
        for name, content in context_files.items():
            sections.append(f"Bound file `{name}`:\n{content.strip()[:6000]}")
        rag = str(pack.get("rag_brief") or "")
        if rag:
            sections.append(rag)
        if ctx.memory_brief:
            sections.append(f"Layered memory brief:\n{ctx.memory_brief}")
        if ctx.toolkit_brief:
            sections.append(ctx.toolkit_brief)
        # In-loop 元工具协议说明：教模型"缺能力时如何申请"（search 召回候选 → pick 上浮）。
        # 意图经 StageResult.meta 透传，本段只是说明书；关闭开关时不注入。
        if _toolkit_search_enabled():
            sections.append(_TOOLKIT_META_PROTOCOL)
        if _memory_tools_enabled():
            sections.append(_MEMORY_META_PROTOCOL)
        if repo_section:
            sections.append(repo_section.strip())
        artifact_index = list(pack.get("artifact_index") or [])
        if artifact_index:
            sections.append(
                "Mission artifact index:\n" + "\n".join(f"- `{p}`" for p in artifact_index[:20])
            )
        sections.append(
            "Output contract: finish with a markdown section titled `## Result` summarising "
            "what this stage produced (files written, decisions, next step)."
        )
        return "\n\n".join(s for s in sections if s and s.strip())

    # --- artifacts ------------------------------------------------------------------

    async def _artifacts(
        self,
        ctx: StageContext,
        workspace: Path,
        reply: Any,
        log_lines: list[str],
        *,
        harness: str,
    ) -> tuple[dict[str, str], str]:
        """Build the stage's artifact set, always satisfying the stage contract."""
        from sortie_deck.contracts import REQUIRED
        from sortie_deck.plugins.coding import _count_impl_files, _git_diff
        from sortie_deck.settings import settings

        needs_diff = "diff.patch" in REQUIRED.get(ctx.stage, [])
        diff = ""
        if needs_diff:
            repo_root = settings.data_dir.parent
            diff = await _git_diff(workspace, repo_root, ctx.repos)
        log = "\n".join(log_lines)
        reply_md = (
            f"# Harness stage reply — `{ctx.stage}`\n\n{reply.text}\n\n"
            f"## Log\n{log[:4000]}\n"
        )
        artifacts: dict[str, str] = {"reply.md": reply_md}
        for name in REQUIRED.get(ctx.stage, []):
            if name == "diff.patch":
                artifacts[name] = diff
            elif name.endswith(".json"):
                artifacts[name] = json.dumps(
                    {
                        "stage": ctx.stage,
                        "status": "ok" if reply.status == "ok" else "failed",
                        "summary": (reply.text or "")[:1500],
                        "harness": harness,
                        "workspace": str(workspace),
                        "files_written": _count_impl_files(workspace),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            else:
                artifacts[name] = (
                    f"# {name} — stage `{ctx.stage}` (harness `{harness}`)\n\n"
                    f"{reply.text}\n\n## Log\n{log[:2000]}\n"
                )
        return artifacts, diff
