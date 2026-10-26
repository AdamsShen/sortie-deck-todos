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

# 小群讨论协议：真实 harness 的 agent loop 在 provider 进程里，没法像 mock 执行器那样
# 直接 await host.open_subroom；改由回复文本声明意图 → 编排层拉群拿结论 → 再发一轮收尾。
# 与 toolkit_intent / memory_search 保持同一"声明-兑现"范式。
_SUBROOM_BLOCK_RE = re.compile(r"```subroom\s*(.*?)```", re.DOTALL)

_SUBROOM_PROTOCOL = """Sub-room consult (mid-stage discussion):
When this stage genuinely needs another role's judgement (product / design / eng / qa / deploy),
declare ONE intent block anywhere in your reply:
```subroom
{"goal": "<what must be settled>", "questions": ["<specific question>"], "participants": ["qa"]}
```
The orchestrator convenes a temporary sub-room immediately, waits for the conclusion, then
sends it back so you can finish this stage with it folded in.
Rules: at most one sub-room per stage; participants must be roles other than your own;
skip it when you can decide alone."""


_CONTROL_BLOCK_RE = re.compile(r"```control\s*(.*?)```", re.DOTALL)

# QA 结论协议：图的门禁需要 qa_passed 才能区分「放行」与「打回重做」。
# 真实 harness 不写这个块时图不再默认打回（由人工门禁决定），但 QA 阶段仍应主动声明。
_QA_BLOCK_RE = re.compile(r"```qa\s*(.*?)```", re.DOTALL)

_QA_PROTOCOL = """Verification verdict (QA stage):
End your reply with ONE ```qa fenced block declaring the outcome:
```qa
{"passed": true, "summary": "<why it passes, or what blocks release>"}
```
Set passed=false when acceptance criteria are not met — the verdict decides whether the
mission loops back to build or continues to release. Omit the block only if you cannot judge."""


def _is_qa_stage(stage: str) -> bool:
    return stage == "qa_verify" or stage.endswith("_verify")


def _parse_qa_verdict(text: str) -> bool | None:
    """解析 QA 结论块；无块 / 非 bool 返回 None（不臆断通过与否）。"""
    for block in _QA_BLOCK_RE.findall(text or ""):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("passed"), bool):
            return bool(data["passed"])
    return None


def _strip_qa_block(text: str) -> str:
    return _QA_BLOCK_RE.sub("", text or "").strip()

# 控制平面协议：跨 agent 的交接与升级必须由编排层执行（模型只声明意图），
# 与 toolkit_intent / subroom 保持同一"声明-兑现"范式。
_CONTROL_PROTOCOL_HEAD = """Control-plane intent (hand-off / escalation):
Finish your own deliverables first. When this stage's work is complete you may hand the
next step to a teammate; when you hit a decision you cannot settle yourself, escalate it.
Declare at most ONE intent in a ```control fenced block somewhere in your reply:
```control
{"handoff": {"role": "<role>", "reason": "<why the next agent is needed>"}}
```
```control
{"escalate": {"question": "<what must be decided>", "options": ["<A>", "<B>"]}}
```
Rules: pick a role from the allowed targets below — never invent a role or stage; the
mission manager answers escalations first and only then a human, and the mission pauses
until that answer arrives. Omit the block when the default next step is correct."""

_CONTROL_TARGETS_HEAD = "Allowed hand-off targets (role → stage):"


def _parse_control_intent(text: str) -> dict[str, Any] | None:
    """解析回复里的控制平面意图块（handoff / escalate）。

    无块、JSON 损坏或两个键都不成立时返回 None，调用方保持原有行为（零回归）；
    同类意图多份时取第一份有效块。
    """
    for block in _CONTROL_BLOCK_RE.findall(text or ""):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("handoff"), dict) or isinstance(data.get("escalate"), dict):
            return data
    return None


def _strip_control_block(text: str) -> str:
    """去掉协议块本身，避免路由 JSON 污染阶段产物与记忆。"""
    return _CONTROL_BLOCK_RE.sub("", text or "").strip()


def _control_protocol(ctx: StageContext) -> str:
    """按本阶段实际可用的落点拼协议说明；无 handoff 目标时只教升级。"""
    targets = list(ctx.next_targets or [])
    lines = [_CONTROL_PROTOCOL_HEAD]
    if targets:
        lines.append(_CONTROL_TARGETS_HEAD)
        lines.extend(
            f"- `{t.get('role')}` → `{t.get('stage')}`" + (f"（{t['label']}）" if t.get("label") else "")
            for t in targets
        )
    else:
        lines.append(
            "No autonomous hand-off target is available for this stage — escalate instead "
            "if you cannot proceed."
        )
    return "\n".join(lines)


def _resolve_handoff_target(ctx: StageContext, intent: dict[str, Any]) -> tuple[str | None, str]:
    """把模型声明的角色（或阶段）映射成本阶段可用的 plan 阶段名。

    返回 ``(stage, "")`` 或 ``(None, 拒绝原因)``。只做查表，真正的二次校验
    （契约白名单 / 预算 / HITL / done）仍在 graph._apply_handoff。
    """
    role = str(intent.get("role") or "").strip()
    stage = str(intent.get("stage") or "").strip()
    targets = list(ctx.next_targets or [])
    for target in targets:
        if role and str(target.get("role") or "") == role:
            return str(target.get("stage") or ""), ""
        if stage and str(target.get("stage") or "") == stage:
            return str(target.get("stage") or ""), ""
    allowed = ", ".join(f"{t.get('role')}→{t.get('stage')}" for t in targets) or "(none)"
    return None, f"`{role or stage or '?'}` is not in the allowed targets: {allowed}"


def _parse_subroom_intent(text: str) -> dict[str, Any] | None:
    """从回复文本解析一次小群讨论意图。

    无围栏块 / JSON 损坏 / 既无 goal 又无 questions 时返回 None，
    调用方据此保持原有单发行为（零回归）；同类意图多份时取第一份。
    """
    for block in _SUBROOM_BLOCK_RE.findall(text or ""):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(data, dict) and (data.get("goal") or data.get("questions")):
            return data
    return None


def _strip_subroom_block(text: str) -> str:
    """去掉协议块本身，避免意图 JSON 污染阶段产物与后续上下文。"""
    return _SUBROOM_BLOCK_RE.sub("", text or "").strip()

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

    async def run(self, ctx: StageContext, host: Any = None) -> StageResult:
        from sortie_deck.harness_providers.base import ProbeStatus, get_harness_registry
        from sortie_deck.settings import settings

        harness = (ctx.harness or "").strip() or settings.default_harness
        registry = get_harness_registry()
        probe = ProbeStatus(status="missing", detail=f"harness `{harness}` is not registered")
        if registry.has(harness):
            probe = registry.probe(harness)
        if not probe.ok:
            if settings.harness_fallback:
                return await self._fallback_run(ctx, harness, probe, host=host)
            return StageResult(
                status="fail",
                message=f"harness `{harness}` unavailable: {probe.detail}",
                meta={"harness": harness, "probe": probe.as_dict(), "fallback": False},
            )
        return await self._run_harness(ctx, harness, host=host)

    async def run_session(self, ctx: StageContext, host: Any) -> StageResult:
        """会话入口：给真实 harness 的 agent loop 补上小群讨论能力。

        ``run()`` 保持单发语义（无 host 时不注入协议、不做二次调用），
        只有编排层判定本次执行可以拉群（subroom_enabled 且服务就绪）时才走这里。
        """
        return await self.run(ctx, host=host)

    # --- degradation ---------------------------------------------------------

    async def _fallback_run(
        self, ctx: StageContext, harness: str, probe: Any, *, host: Any = None
    ) -> StageResult:
        from sortie_deck.registry import get_registry, run_executor

        legacy_name = _legacy_executor_name(ctx)
        executor = get_registry().get(legacy_name)
        result = await run_executor(executor, ctx, host)
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

    async def _run_harness(
        self, ctx: StageContext, harness: str, *, host: Any = None
    ) -> StageResult:
        from sortie_deck.harness_providers.base import get_harness_registry
        from sortie_deck.mission_memory import build_stage_summary, get_mission_memory
        from sortie_deck.plugins.coding import (
            _count_impl_files,
            clone_repos,
            ensure_worktree,
        )
        from sortie_deck.settings import settings

        # 目录优先取编排器注入值（ctx），只有旧调用方/直接单测才回退全局 settings：
        # 否则测试与多实例场景会把任务沙盒写进进程级真实数据目录
        data_dir = Path(ctx.data_dir) if ctx.data_dir else settings.data_dir
        worktrees_dir = Path(ctx.worktrees_dir) if ctx.worktrees_dir else settings.worktrees_dir
        workspace = ensure_worktree(data_dir.parent, ctx.initiative_id, worktrees_dir)
        repo_section, repo_log = await clone_repos(ctx.repos, workspace, ctx.initiative_id)

        pack = await self._build_context_pack(ctx)
        prompt = self._build_prompt(
            ctx, workspace, pack, repo_section, with_subroom=host is not None
        )
        provider = get_harness_registry().get(harness)
        session = await provider.start_session(
            initiative_id=ctx.initiative_id,
            stage=ctx.stage,
            meta={"role": str(getattr(ctx.role, "value", ctx.role))},
        )
        reply = await provider.send(session, prompt, cwd=str(workspace), context_pack=pack)
        # 模型声明小群讨论意图时，立即拉群并把结论回注成第二轮收尾
        reply = await self._maybe_consult_subroom(
            ctx, provider, session, reply, pack, host, workspace
        )

        # 控制平面意图：handoff 交给图的条件边跳转，escalate 先 Manager 后真人。
        # 协议块只承载路由意图，兑现后从正文剥离，不进产物与记忆。
        control = _parse_control_intent(getattr(reply, "text", "") or "")
        control_meta: dict[str, Any] = {}
        proposed_next: str | None = None
        if control:
            escalate = control.get("escalate")
            handoff = control.get("handoff")
            if isinstance(escalate, dict):
                question = str(escalate.get("question") or "").strip()
                # 已有人类答复时不再升级：否则真人回答后重入会再次挂起
                if question and not (ctx.human_instruction or "").strip():
                    answer = ""
                    if host is not None and hasattr(host, "escalate"):
                        answer = await host.escalate(question)
                    if answer.startswith("manager:"):
                        resolution = answer[len("manager:") :].strip()
                        control_meta["escalation"] = {"level": "manager", "resolution": resolution}
                        try:
                            follow = await provider.send(
                                session,
                                (
                                    "任务管理器已对你升级的问题给出裁决：\n\n"
                                    f"{resolution}\n\n"
                                    "请据此完成本阶段的最终产出（结尾保留 `## Result`）；"
                                    "不要再声明 escalate。"
                                ),
                                cwd=str(workspace),
                                context_pack=pack,
                            )
                        except Exception:  # noqa: BLE001 — 二次调用失败保留首轮结果
                            logger.warning(
                                "Manager 裁决回注失败(忽略): initiative=%s stage=%s",
                                ctx.initiative_id,
                                ctx.stage,
                                exc_info=True,
                            )
                        else:
                            merged = "\n\n".join(
                                part
                                for part in (
                                    _strip_control_block(getattr(reply, "text", "") or ""),
                                    str(getattr(follow, "text", "") or "").strip(),
                                )
                                if part
                            )
                            reply = reply.model_copy(
                                update={
                                    "text": merged,
                                    "status": (
                                        "ok"
                                        if str(getattr(follow, "status", "")) == "ok"
                                        else reply.status
                                    ),
                                    "native_id": (
                                        str(getattr(follow, "native_id", "") or "")
                                        or str(getattr(reply, "native_id", "") or "")
                                    ),
                                }
                            )
                    else:
                        control_meta["escalation"] = {"level": "human", "channel": answer}
                        stripped = _strip_control_block(getattr(reply, "text", "") or "")
                        logger.info(
                            "agent 升级真人: initiative=%s stage=%s question=%s",
                            ctx.initiative_id,
                            ctx.stage,
                            question[:200],
                        )
                        return StageResult(
                            status="needs_hitl",
                            message=f"agent escalation: {question}",
                            artifacts={"reply.md": stripped} if stripped else {},
                            meta={
                                "kind": "escalation",
                                "question": question,
                                "options": [str(o) for o in (escalate.get("options") or [])],
                                **control_meta,
                            },
                        )
            elif isinstance(handoff, dict):
                target, reason = _resolve_handoff_target(ctx, handoff)
                if target:
                    proposed_next = target
                    control_meta["handoff"] = {
                        "accepted": target,
                        "role": str(handoff.get("role") or ""),
                        "reason": str(handoff.get("reason") or "")[:300],
                    }
                    logger.info(
                        "agent handoff 接受: initiative=%s stage=%s -> %s",
                        ctx.initiative_id,
                        ctx.stage,
                        target,
                    )
                else:
                    control_meta["handoff"] = {"rejected": reason}
                    logger.warning(
                        "agent handoff 被拒: initiative=%s stage=%s reason=%s",
                        ctx.initiative_id,
                        ctx.stage,
                        reason,
                    )
            reply = reply.model_copy(
                update={"text": _strip_control_block(getattr(reply, "text", "") or "")}
            )

        # QA 结论：图的门禁靠它决定放行还是打回；块本身不进产物
        qa_passed = _parse_qa_verdict(getattr(reply, "text", "") or "")
        if qa_passed is not None:
            reply = reply.model_copy(
                update={"text": _strip_qa_block(getattr(reply, "text", "") or "")}
            )

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
        # 模型没声明交接时保留编码角色的既有默认落点（qa_verify）
        fallback_next = (
            "qa_verify"
            if ok and str(getattr(ctx.role, "value", ctx.role)) in CODING_ROLE_VALUES
            else None
        )
        return StageResult(
            status="ok" if ok else "fail",
            message=f"harness `{harness}` stage finished" if ok else (
                f"harness `{harness}` 未产出有效回复（见 reply.md / implementation.md）"
            ),
            artifacts=artifacts,
            proposed_next=proposed_next or fallback_next,
            qa_passed=qa_passed,
            meta={**meta, **({"control_intent": control_meta} if control_meta else {})},
        )

    async def _maybe_consult_subroom(
        self,
        ctx: StageContext,
        provider: Any,
        session: Any,
        reply: Any,
        pack: dict[str, Any],
        host: Any,
        workspace: Path,
    ) -> Any:
        """模型声明小群讨论意图时拉群，并把结论回注成第二轮回复。

        输入：首轮 ``HarnessReply``（text 里可能含 ```subroom 围栏块）。
        输出：合并后的 ``HarnessReply``；无 host / 无意向 / 讨论被拒 / 二次调用失败
        时原样返回首轮结果——协作是增强项，任何一步失败都不能打挂阶段。
        """
        if host is None:
            return reply
        intent = _parse_subroom_intent(getattr(reply, "text", "") or "")
        if intent is None:
            return reply
        goal = str(intent.get("goal") or "").strip()
        questions = [str(q).strip() for q in (intent.get("questions") or []) if str(q).strip()]
        participants = [
            str(p).strip() for p in (intent.get("participants") or []) if str(p).strip()
        ]
        try:
            conclusion = await host.open_subroom(
                goal=goal,
                questions=questions or None,
                participants=participants or None,
            )
        except Exception:  # noqa: BLE001 — 讨论失败只降级，不影响本阶段产出
            logger.warning(
                "小群讨论调用失败(忽略): initiative=%s stage=%s",
                ctx.initiative_id,
                ctx.stage,
                exc_info=True,
            )
            return reply
        if not str(conclusion or "").strip():
            logger.info(
                "小群讨论被拒或无结论，保持单发结果: initiative=%s stage=%s goal=%s",
                ctx.initiative_id,
                ctx.stage,
                goal[:80],
            )
            return reply

        # 续接同一 provider 会话，第二轮才能看到首轮的上下文
        native_id = str(getattr(reply, "native_id", "") or "")
        if native_id:
            session.native_id = native_id
        try:
            follow = await provider.send(
                session,
                (
                    "你申请的小组讨论已经完成，结论如下：\n\n"
                    f"{conclusion.strip()}\n\n"
                    "请据此完成本阶段的最终产出（结尾保留 `## Result` 小结），不要再申请新的讨论。"
                ),
                cwd=str(workspace),
                context_pack=pack,
            )
        except Exception:  # noqa: BLE001 — 二次调用失败时保留首轮结果
            logger.warning(
                "小群讨论结论回注失败(忽略): initiative=%s stage=%s",
                ctx.initiative_id,
                ctx.stage,
                exc_info=True,
            )
            return reply

        merged_text = "\n\n".join(
            part
            for part in (
                _strip_subroom_block(getattr(reply, "text", "") or ""),
                f"## 小组讨论结论\n{conclusion.strip()}",
                str(getattr(follow, "text", "") or "").strip(),
            )
            if part
        )
        if not merged_text.strip():
            return reply
        logger.info(
            "小群讨论已回注: initiative=%s stage=%s participants=%s",
            ctx.initiative_id,
            ctx.stage,
            ",".join(participants) or "(default)",
        )
        return reply.model_copy(
            update={
                "text": merged_text,
                "status": "ok" if str(getattr(follow, "status", "")) == "ok" else reply.status,
                "native_id": str(getattr(follow, "native_id", "") or "") or native_id,
            }
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
            "rag_brief": await self._rag_brief(ctx),
        }
        return out

    async def _rag_brief(self, ctx: StageContext) -> str:
        """阶段入口知识召回（增强项）。

        放到线程池并设超时上限：WeKnora graph 模式单次检索实测可达 35s 且是同步 HTTP，
        直接在事件循环里调用会卡住整个 API 进程；超时/失败一律降级为空 brief。
        """
        from sortie_deck.knowledge_gateway import gateway_from_settings
        from sortie_deck.settings import settings

        limit = float(getattr(settings, "harness_rag_timeout_seconds", 5.0) or 5.0)
        try:
            gateway = gateway_from_settings(settings)
            # Mission namespace first (this requirement's knowledge), then global.
            hits = await asyncio.wait_for(
                asyncio.to_thread(
                    gateway.retrieve,
                    f"{ctx.brief} {ctx.stage}",
                    limit=6,
                    initiative_id=ctx.initiative_id,
                ),
                timeout=limit,
            )
        except TimeoutError:
            logger.warning(
                "RAG 召回超时已降级: initiative=%s stage=%s limit=%.1fs",
                ctx.initiative_id,
                ctx.stage,
                limit,
            )
            return ""
        except Exception:  # noqa: BLE001 — RAG is an enhancement
            logger.warning(
                "RAG 召回失败已降级: initiative=%s stage=%s",
                ctx.initiative_id,
                ctx.stage,
                exc_info=True,
            )
            return ""
        if not hits:
            return ""
        lines = []
        for h in hits:
            marker = "mission" if (h.meta or {}).get("mission") else h.source
            lines.append(f"- [{marker}] {h.title}: {h.content[:300]}")
        return "Knowledge brief:\n" + "\n".join(lines)

    def _build_prompt(
        self,
        ctx: StageContext,
        workspace: Path,
        pack: dict[str, Any],
        repo_section: str,
        *,
        with_subroom: bool = False,
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
        # 小群讨论协议只在编排层确实提供了 host 时注入（否则模型声明了也兑现不了）
        if with_subroom:
            sections.append(_SUBROOM_PROTOCOL)
        # 控制平面协议始终注入：即使没有可交接目标，升级真人仍是 agent 的兜底能力
        sections.append(_control_protocol(ctx))
        # QA 阶段额外要求结论块：否则图无法区分"测试通过"与"需要打回"
        if _is_qa_stage(ctx.stage):
            sections.append(_QA_PROTOCOL)
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
            repo_root = (Path(ctx.data_dir) if ctx.data_dir else settings.data_dir).parent
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
