from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from sortie_deck.agent_contract import ContractError, validate_handoff
from sortie_deck.artifacts import ArtifactStore
from sortie_deck.contracts import validate_stage_artifacts
from sortie_deck.loadout import (
    PICK_BUDGET_PER_STAGE,
    SEARCH_TOP_K,
    SKILL_OUTPUT_FLOAT_LIMIT,
    SKILL_RUN_BUDGET_PER_STAGE,
    pick_candidate,
    probe_item,
    recall,
    summarize_candidates,
    unfreeze_item,
)
from sortie_deck.manager import BranchStatus, MissionManager
from sortie_deck.models import (
    CODING_ROLE_VALUES,
    ENG_BUILD_STAGES,
    HitlAction,
    HitlDecision,
    RepoConfig,
    RoleId,
    StageContext,
    StageResult,
    role_key,
)
from sortie_deck.parallel import collapse_to_waves, wave_node_name
from sortie_deck.plugins.queue import ENG_QUEUE
from sortie_deck.registry import ExecutorRegistry, run_executor
from sortie_deck.templates import load_agent_contract, load_persona, load_pipeline_template

logger = logging.getLogger(__name__)


async def _maybe_publish(
    state: dict[str, Any],
    ctx: StageContext,
    result: StageResult,
    stage: str,
) -> dict[str, Any] | None:
    """编码阶段成功后的 git 回写：commit + push 任务分支 + GitLab MR（软降级）。

    触发条件（TDT_GIT_PUSH_MODE）：
    - ``on_stage_success``（默认）：编码阶段 status=ok 且该角色绑定了 mission repos；
    - ``on_approval``：同上，但仅在该阶段经 HITL 权限门禁放行重入时（state.publish_pending）；
    - ``off``：不回写。
    失败不抛错——回写结果只进阶段 meta / 事件流，不污染编码结果判定。
    """
    from sortie_deck.git_publish import publish_repos, publish_summary_meta
    from sortie_deck.settings import settings

    mode = (settings.git_push_mode or "off").strip().lower()
    if mode == "off" or not ctx.repos or result.status != "ok":
        return None
    if mode == "on_approval" and not state.get("publish_pending"):
        return None
    if not ctx.workspace_dir:
        return None
    try:
        results = await publish_repos(
            Path(ctx.workspace_dir),
            ctx.initiative_id,
            ctx.repos,
            commit_message=f"sortie: {ctx.initiative_id} stage={stage}",
        )
    except Exception as exc:  # noqa: BLE001 — 回写失败不影响阶段结果
        return {"git_publish_errors": [f"publish_repos 异常: {exc}"]}
    if not results:
        return None
    return publish_summary_meta(results)


def _coding_perm_from_instruction(instruction: str | None) -> dict[str, str]:
    """Parse Sortie Deck retry tag from human_instruction into StageContext.permissions."""
    text = instruction or ""
    markers = (
        "[sortie:coding_permission_mode=",
        "[rally:coding_permission_mode=",  # legacy
    )
    mode = ""
    for marker in markers:
        if marker not in text:
            continue
        try:
            rest = text.split(marker, 1)[1]
            mode = rest.split("]", 1)[0].strip()
        except Exception:
            mode = ""
        if mode:
            break
    if not mode:
        return {}
    return {"coding_permission_mode": mode}


def _truncate_output(text: str, *, max_chars: int = 40000) -> str:
    """单次工具输出的硬截断（对齐 Claude Code ~25k token 上限，约 4 万字符）。

    防止单个工具的超大返回把阶段上下文撑爆；截断时显式标注，模型可感知。
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n… [output truncated: exceeded per-call limit]"


@dataclass
class ToolkitMetaOutcome:
    """in-loop 元工具意图的处理结果（run_toolkit_meta 的返回值）。

    brief_append 直接并入 toolkit_briefs[f"stage:{stage}"]；events 进事件流；
    records 落为阶段产物 toolkit_meta.json 供审计回放。
    """

    brief_append: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)


async def run_toolkit_meta(
    store: Any,
    *,
    stage: str,
    meta: dict[str, Any] | None,
    allowed_ids: set[str] | None = None,
    used_picks: int = 0,
    floated: list[str] | None = None,
    skill_used: int = 0,
    workspace_dir: str | None = None,
) -> ToolkitMetaOutcome:
    """处理 executor 声明的 in-loop 元工具意图（toolkit_search / toolkit_pick / skill_run）。

    executor 只在 StageResult.meta 里"声明意图"，检索/校验/上浮/执行全部在本函数
    （编排层）完成——授权与预算集中在一处，与 _run_mcp_calls 同一模式。

    意图结构（StageResult.meta）：
      {"toolkit_search": {"query": "...", "kinds": ["mcp", "tool"]}}  自由文本召回
      {"toolkit_pick": {"item_id": "tk_x"}}                           点名上浮
      {"skill_run": {"item_id": "tk_x", "script": "scripts/x.py",
                     "args": [], "confirmed": false}}                 执行脚本
                                                                     （须先 pick）

    pick 校验链：存在 → 启用 → 白名单 → 预算 → 探活；任一失败都把原因回传给
    模型（可换条目/换词重搜），永不抛异常、不中断阶段。
    """
    from sortie_deck.settings import settings as _settings

    outcome = ToolkitMetaOutcome()
    if not bool(getattr(_settings, "toolkit_search", False)):
        return outcome
    if not isinstance(meta, dict):
        return outcome

    search_intent = meta.get("toolkit_search")
    pick_intent = meta.get("toolkit_pick")
    run_intent = meta.get("skill_run")
    floated = list(floated or [])
    # 多个意图并存时 brief 增量按序拼接（单意图行为与旧版一致）
    notes: list[str] = []

    if isinstance(search_intent, dict) and str(search_intent.get("query") or "").strip():
        query = str(search_intent.get("query")).strip()
        kinds = search_intent.get("kinds") if isinstance(search_intent.get("kinds"), list) else None
        rows = recall(
            store,
            query=query,
            kinds=kinds,
            limit=SEARCH_TOP_K,
            allowed_ids=allowed_ids,
        )
        candidates = summarize_candidates(rows)
        if candidates:
            notes.append(
                "Toolkit search results (pick by item id when needed):\n"
                + json.dumps(candidates, ensure_ascii=False)
            )
        else:
            notes.append(f"Toolkit search for `{query}` found no matching capability.")
        outcome.events.append(
            {
                "stage": stage,
                "kind": "toolkit_meta",
                "message": f"toolkit_search: {query}",
                "data": {"candidates": candidates},
            }
        )
        outcome.records.append(
            {"op": "search", "stage": stage, "query": query, "candidates": candidates}
        )

    if isinstance(pick_intent, dict):
        item_id = str(pick_intent.get("item_id") or "").strip()
        if item_id:
            record: dict[str, Any] = {"op": "pick", "stage": stage, "item_id": item_id}
            if item_id in floated:
                record["reason"] = "already floated in this stage"
                notes.append(
                    f"Toolkit pick `{item_id}` was already floated earlier in this stage."
                )
            elif used_picks >= PICK_BUDGET_PER_STAGE:
                record["reason"] = "pick budget exhausted"
                notes.append(
                    f"Toolkit pick budget exhausted ({PICK_BUDGET_PER_STAGE} per stage) — "
                    "continue with what you have."
                )
            else:
                item, reason = await pick_candidate(store, item_id, allowed_ids=allowed_ids)
                if item is None:
                    record["reason"] = reason
                    notes.append(f"Toolkit pick `{item_id}` rejected: {reason}")
                else:
                    record["floated"] = True
                    # kind 供阶段节点回写 stage_toolkits 时过滤 mcp 条目
                    record["kind"] = item.kind
                    floated.append(item.id)
                    notes.append(unfreeze_item(item))
            outcome.events.append(
                {
                    "stage": stage,
                    "kind": "toolkit_meta",
                    "message": f"toolkit_pick: {item_id}",
                    "data": dict(record),
                }
            )
            outcome.records.append(record)

    if isinstance(run_intent, dict):
        await _handle_skill_run(
            store,
            stage=stage,
            intent=run_intent,
            allowed_ids=allowed_ids,
            floated=floated,
            skill_used=skill_used,
            workspace_dir=workspace_dir,
            outcome=outcome,
            notes=notes,
        )

    outcome.brief_append = "\n".join(n for n in notes if n)
    return outcome


def _record_memory_usage(initiative_id: str, mem_id: str) -> None:
    """memory_read 取用记录追加进任务 usage.log（任务详情「记忆」面板数据源）。"""
    try:
        from sortie_deck.mission_memory import get_mission_memory

        log = get_mission_memory().usage_log(initiative_id)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{mem_id}\n")
    except Exception:  # noqa: BLE001 — 使用记录只是增强
        pass


async def run_memory_meta(
    memory_store: Any,
    *,
    stage: str,
    meta: dict[str, Any] | None,
    initiative_id: str,
    created_by: str | None = None,
    used_reads: int = 0,
) -> ToolkitMetaOutcome:
    """处理 executor 声明的记忆元工具意图（memory_search / memory_read）。

    与 run_toolkit_meta 同一模式：executor 只在 StageResult.meta 里声明意图，
    检索 / 权限 / 预算全部在编排层兑现；结果经 brief_append 并入下阶段 brief，
    读取 ids 记入任务 usage.log（面板可见）。永不抛异常、不中断阶段。

    意图结构（StageResult.meta）：
      {"memory_search": {"query": "..."}}   自由文本检索记忆简介行
      {"memory_read": {"id": "mem_xxx"}}    读全文（截断），预算受限
    """
    from sortie_deck.settings import settings as _settings

    outcome = ToolkitMetaOutcome()
    if not bool(getattr(_settings, "memory_tools", False)):
        return outcome
    if not isinstance(meta, dict) or memory_store is None:
        return outcome
    notes: list[str] = []

    search_intent = meta.get("memory_search")
    if isinstance(search_intent, dict) and str(search_intent.get("query") or "").strip():
        query = str(search_intent.get("query")).strip()
        try:
            entries = memory_store.context_candidates(
                query=query, mission_id=initiative_id, limit=6
            )
        except Exception as exc:  # noqa: BLE001 — 检索失败回空，不中断阶段
            logger.warning("memory_search 失败(忽略): stage=%s: %s", stage, exc)
            entries = []
        lines = [f"- [{e.kind}|{e.id}] {e.display_summary()}" for e in entries]
        if lines:
            notes.append(
                "Memory search results (read by id when needed):\n" + "\n".join(lines)
            )
        else:
            notes.append(f"Memory search for `{query}` found nothing.")
        outcome.events.append(
            {
                "stage": stage,
                "kind": "memory_meta",
                "message": f"memory_search: {query}",
                "data": {"hits": [e.id for e in entries]},
            }
        )
        outcome.records.append(
            {"op": "memory_search", "stage": stage, "query": query, "hits": [e.id for e in entries]}
        )

    read_intent = meta.get("memory_read")
    if isinstance(read_intent, dict):
        mem_id = str(read_intent.get("id") or "").strip()
        if mem_id:
            record: dict[str, Any] = {"op": "memory_read", "stage": stage, "id": mem_id}
            budget = int(getattr(_settings, "memory_read_budget_per_stage", 4))
            max_chars = int(getattr(_settings, "memory_read_max_chars", 2000))
            if used_reads >= budget:
                record["reason"] = "read budget exhausted"
                notes.append(
                    f"Memory read budget exhausted ({budget} per stage) — continue with what you have."
                )
            else:
                try:
                    entry = memory_store.get(mem_id)
                except KeyError:
                    entry = None
                if entry is None:
                    record["reason"] = "not found"
                    notes.append(f"Memory `{mem_id}` not found.")
                elif (
                    entry.scope == "user"
                    and created_by
                    and entry.scope_id != created_by
                    and entry.author_id != created_by
                ):
                    # user 域隐私护栏：任务内只能读创建者本人的个人记忆
                    record["reason"] = "private memory denied"
                    notes.append(f"Memory `{mem_id}` is private and not readable in this mission.")
                else:
                    record["read"] = True
                    notes.append(
                        f"Memory `{mem_id}` full content:\n{entry.content[:max_chars]}"
                    )
                    _record_memory_usage(initiative_id, mem_id)
            outcome.events.append(
                {
                    "stage": stage,
                    "kind": "memory_meta",
                    "message": f"memory_read: {mem_id}",
                    "data": dict(record),
                }
            )
            outcome.records.append(record)

    outcome.brief_append = "\n".join(n for n in notes if n)
    return outcome


def merge_picked_toolkits(
    state: PipelineState, stage: str, records: list[dict[str, Any]]
) -> dict[str, list[str]]:
    """把本阶段 pick 上浮的 mcp 条目回写进 stage_toolkits（缺口闭环）。

    背景：_run_mcp_calls 只为 stage_toolkits 里的条目建会话；pick 此前只更新
    brief 文本，导致上浮的 mcp "看得见调不了"。本函数将 pick 成功（floated）
    的 mcp item_id 去重追加进 state["stage_toolkits"][stage]，使后续 mcp_calls
    可真实调用；tool/skill 不回写（前者已在 brief 有端点，后者走 skill_run）。
    返回新映射，无 pick 时原样返回（避免无谓覆盖 state）。
    """
    picked = [
        str(r.get("item_id"))
        for r in records
        if r.get("op") == "pick" and r.get("floated") and r.get("kind") == "mcp"
    ]
    if not picked:
        return dict(state.get("stage_toolkits") or {})
    stage_toolkits = dict(state.get("stage_toolkits") or {})
    existing = list(stage_toolkits.get(stage) or [])
    stage_toolkits[stage] = existing + [i for i in picked if i not in existing]
    return stage_toolkits


async def _handle_skill_run(
    store: Any,
    *,
    stage: str,
    intent: dict[str, Any],
    allowed_ids: set[str] | None,
    floated: list[str],
    skill_used: int,
    workspace_dir: str | None,
    outcome: ToolkitMetaOutcome,
    notes: list[str],
) -> None:
    """兑现 skill_run 意图：校验（预算/先 pick/白名单/探活/确认门）→ 沙箱执行。

    输出摘要回填 brief（模型可见执行结果，闭环）；完整（截断后）输出留在
    record 里，由调用方落 skill_runs.json 产物供审计回放。
    """
    from pathlib import Path

    from sortie_deck.mcp_runtime import tool_looks_like_write
    from sortie_deck.skill_runner import run_skill_script

    item_id = str(intent.get("item_id") or "").strip()
    script = str(intent.get("script") or "").strip()
    run_args = intent.get("args") if isinstance(intent.get("args"), list) else []
    confirmed = bool(intent.get("confirmed"))
    record: dict[str, Any] = {"op": "skill_run", "stage": stage, "item_id": item_id, "script": script}

    if not item_id or not script:
        record["reason"] = "item_id and script are required"
        notes.append(f"Skill run rejected: {record['reason']}.")
    elif skill_used >= SKILL_RUN_BUDGET_PER_STAGE:
        record["reason"] = "skill run budget exhausted"
        notes.append(
            f"Skill run budget exhausted ({SKILL_RUN_BUDGET_PER_STAGE} per stage)."
        )
    elif item_id not in floated:
        # 闭环语义：先 pick 上浮（拿到用法说明）才能执行其脚本
        record["reason"] = "item not picked in this stage — pick it first"
        notes.append(
            f"Skill run `{item_id}` rejected: pick the item first (toolkit_pick), "
            "then run its scripts."
        )
    else:
        item, reason = await pick_candidate(store, item_id, allowed_ids=allowed_ids)
        if item is None:
            record["reason"] = reason
            notes.append(f"Skill run `{item_id}` rejected: {reason}")
        elif tool_looks_like_write(Path(script).name) and not confirmed:
            record["reason"] = "script looks like a write — confirmation required"
            notes.append(
                f"Skill run `{script}` looks like a write — set confirmed=true to run it."
            )
        else:
            record["executed"] = True
            result = await run_skill_script(
                item, script, run_args, workspace_dir=workspace_dir
            )
            record.update(
                {k: result.get(k) for k in ("ok", "exit_code", "timed_out", "duration_ms")}
            )
            record["stdout"] = result.get("stdout") or ""
            record["stderr"] = result.get("stderr") or ""
            if result.get("ok"):
                notes.append(
                    f"Skill `{item.name}` ran `{script}` (exit {result.get('exit_code')}):\n"
                    + (result.get("stdout") or "").strip()[:SKILL_OUTPUT_FLOAT_LIMIT]
                )
            else:
                fail_reason = result.get("reason") or (
                    f"exit {result.get('exit_code')}"
                    + (" (timed out)" if result.get("timed_out") else "")
                )
                notes.append(
                    f"Skill `{item.name}` run `{script}` failed: {fail_reason}\n"
                    "stderr: " + (result.get("stderr") or "")[:500]
                )
    outcome.events.append(
        {
            "stage": stage,
            "kind": "toolkit_meta",
            "message": f"skill_run: {item_id}:{script}",
            "data": dict(record),
        }
    )
    outcome.records.append(record)


@dataclass
class StageRecallOutcome:
    """阶段入口主动召回的结果（run_stage_recall 的返回值）。

    brief_append 并入阶段 brief 与 toolkit_briefs["stage:{stage}"]；
    mcp_ids 回写 stage_toolkits（mcp_calls 可真实建会话调用）、skill_ids 并入
    toolkit_floats（满足 skill_run 的先 pick 校验）、tool_ids 仅并入 toolkit_ids
    （tool 无执行通道，端点文本已在 brief）。
    """

    brief_append: str = ""
    tool_ids: list[str] = field(default_factory=list)
    mcp_ids: list[str] = field(default_factory=list)
    skill_ids: list[str] = field(default_factory=list)

    @property
    def item_ids(self) -> list[str]:
        return [*self.tool_ids, *self.mcp_ids, *self.skill_ids]


async def run_stage_recall(
    store: Any,
    *,
    stage: str,
    queries: list[str],
    bound_ids: set[str] | None = None,
    floated_ids: set[str] | None = None,
    allowed_ids: set[str] | None = None,
    min_score: float = 0.2,
    switch: bool = True,
) -> StageRecallOutcome:
    """阶段入口主动召回：多路 recall → 合并取最高分 → 阈值 → 去重 → 探活 → 上浮。

    为什么要多路短查询而不是拼一条长查询：loadout._score 按查询词元数归一
    （分数 = 命中权重 / 查询词元数），长查询会稀释每个条目的得分——
    "角色+阶段+标签+brief" 拼一起后纯文本匹配只有 ~0.08，全部被阈值卡死。
    拆成聚焦短查询（角色+阶段 / 阶段标签 / brief 摘要各一路）后，单路词元少、
    分数有区分度；同条目多路命中取最高分。

    输入: store（工具箱存储）、queries（聚焦短查询列表，可为空），
          bound_ids（已绑定条目，重叠则用已有工具——跳过）、floated_ids（已上浮条目，
          跳过）、allowed_ids（org_policy 白名单硬过滤）、min_score（合并后分数低于即
          丢弃，"无需则不调用"的实现）、switch（总开关 loadout_stage_recall）。
    输出: StageRecallOutcome；开关关闭 / 无召回 / 全部低于阈值时各字段为空。
    """
    outcome = StageRecallOutcome()
    if not switch or store is None:
        return outcome
    merged: dict[str, tuple[Any, float]] = {}
    for query in queries:
        if not str(query or "").strip():
            continue
        try:
            rows = recall(store, query=query, limit=SEARCH_TOP_K, allowed_ids=allowed_ids)
        except Exception as exc:  # noqa: BLE001 — 单路召回失败降级跳过，不中断阶段
            logging.getLogger(__name__).warning(
                "stage recall failed (stage=%s query=%r): %s", stage, query[:80], exc
            )
            continue
        for item, score in rows:
            if item.id not in merged or score > merged[item.id][1]:
                merged[item.id] = (item, score)
    exclude = set(bound_ids or set()) | set(floated_ids or set())
    notes: list[str] = []
    for item, score in sorted(merged.values(), key=lambda pair: pair[1], reverse=True):
        if score < min_score:
            continue
        if item.id in exclude:
            # 已绑定/已上浮的条目优先保留既有形态，不重复注入
            continue
        status, detail = await probe_item(item)
        if status not in {"ok", "skipped"}:
            # 注入调不上的工具比不注入更糟（模型会浪费轮次去调），探活失败直接丢弃
            logging.getLogger(__name__).info(
                "stage recall: drop %s (%s) probe %s: %s", item.name, item.id, status, detail
            )
            continue
        if item.kind == "mcp":
            outcome.mcp_ids.append(item.id)
        elif item.kind == "skill":
            outcome.skill_ids.append(item.id)
        else:
            outcome.tool_ids.append(item.id)
        notes.append(unfreeze_item(item))
    if notes:
        outcome.brief_append = (
            "Proactively recalled toolkit items for this stage (role/stage match):\n"
            + "\n".join(notes)
        )
    return outcome


class PipelineState(TypedDict, total=False):
    initiative_id: str
    thread_id: str
    brief: str
    title: str
    stage: str
    human_instruction: str | None
    coding_executor: str
    # 任务级 agent harness：LangGraph 按本 schema 过滤输入，缺字段会让
    # orchestrator 传入的 harness 被静默丢弃，所有阶段退化为 mock 执行器
    harness: str
    harness_locked: bool
    deploy_executor: str
    artifact_root: str
    workspace_dir: str | None
    upstream: dict[str, str]
    last_result: dict[str, Any]
    qa_attempt: int
    hitl_decision: dict[str, Any] | None
    stopped: bool
    events: list[dict[str, Any]]
    status: str
    pipeline_track: str
    role_personas: dict[str, str]
    role_toolkits: dict[str, list[str]]
    # Mission-scoped git repos per role slot (slot -> list of repo dicts)
    role_repos: dict[str, list[dict[str, Any]]]
    stage_toolkits: dict[str, list[str]]
    toolkit_briefs: dict[str, str]
    # In-loop 元工具状态：每阶段 pick 预算计数 + 已上浮条目（去重）
    toolkit_pick_budget: dict[str, int]
    toolkit_floats: dict[str, list[str]]
    # 内置记忆元工具：每阶段 memory_read 预算计数
    memory_read_budget: dict[str, int]
    # skill_run 意图：每阶段脚本执行次数（预算 SKILL_RUN_BUDGET_PER_STAGE）
    skill_run_budget: dict[str, int]
    # 阶段入口主动召回幂等标记：每阶段只召回一次（重试/HITL 重入命中即跳过）
    stage_recall_done: dict[str, bool]
    # Frozen plan fingerprint — lets a resume detect an edited pipeline
    plan_digest: str
    # Node the current step routes to (agent handoff / manager join decision)
    handoff_target: str
    # 待真人裁决的升级问题。agent 声明 escalate 后由节点写值并自环，
    # 下次进入本节点时在 run_stage 之前 interrupt：LangGraph 恢复会重放
    # interrupt 之前的代码，先执行阶段再问人会导致回答丢失、阶段白跑一次。
    pending_escalation: dict[str, Any] | None
    # Autonomous handoff budget per target node (prevents A→B→A loops)
    handoff_counts: dict[str, int]
    # Mission-wide handoff budget (runaway protection)
    handoff_total: int
    # Manager-driven parallel wave retry budget per group
    wave_attempts: dict[str, int]
    # Stop the run without marking the initiative as `stopped`
    halt: bool


# Autonomous handoff / wave reroute safety caps (guardrails layer generalises this)
_MAX_HANDOFF_PER_TARGET = 3
_MAX_HANDOFF_TOTAL = 40
_MAX_WAVE_ATTEMPTS = 2


# Built-in role → mock executor fallback. Custom roles are resolved through
# ``sortie_deck.roles.RoleRegistry`` instead of being hard-coded here.
_BUILTIN_ROLE_EXECUTORS: dict[str, str] = {
    "product": "mock_product",
    "design": "mock_design",
    "eng": "mock_eng",
    "qa": "mock_qa",
    "deploy": "mock_deploy",
}


def _role_id(role: str) -> RoleId:
    """Role key → :class:`RoleId`.

    ``RoleId`` materialises unknown keys on first use, so registered custom
    roles (``parenting``, ``stocks``, …) survive here instead of silently
    collapsing to ``product``. Only empty / non-string input falls back.
    """
    try:
        return RoleId(role)
    except (ValueError, TypeError):
        return RoleId.PRODUCT


def _executor_name(stage: str, role: str, state: PipelineState, stage_cfg: dict[str, Any]) -> str:
    # Initiative-locked agent harness: when set, EVERY stage conversation runs
    # through the generic harness executor (mock/LLM stay as in-executor fallback).
    if str(state.get("harness") or "").strip():
        return "harness_stage"
    explicit = stage_cfg.get("executor")
    if explicit:
        return str(explicit)
    if stage == "intake":
        return "mock_intake"
    if stage == "product_prd":
        return "mock_product"
    if stage.endswith("_tech_design"):
        return "mock_tech_design"
    if stage == "design_ui":
        return "mock_design"
    if stage in ENG_BUILD_STAGES or stage in {"eng_selftest", "eng_integrate", "qa_handoff"} or stage.endswith(
        "_selftest"
    ):
        if stage in ENG_BUILD_STAGES:
            coding = state.get("coding_executor", "mock")
            return {
                "mock": "mock_eng",
                "claude_code": "claude_code",
                "cursor_cli": "cursor_cli",
            }.get(coding, "mock_eng")
        if stage == "eng_selftest" or stage.endswith("_selftest"):
            return "mock_selftest"
        if stage == "eng_integrate":
            return "mock_integrate"
        return "mock_handoff"
    if stage == "qa_cases":
        return "mock_qa_cases"
    if stage == "qa_verify":
        return "mock_qa"
    if stage == "deploy_preview":
        deploy = state.get("deploy_executor", "mock")
        return "deploy_shell" if deploy == "deploy_shell" else "mock_deploy"
    # fallback by role
    if role in CODING_ROLE_VALUES:
        return "mock_eng"
    try:
        from sortie_deck.roles import get_role_registry

        role_registry = get_role_registry()
        hint = role_registry.executor_hint(role)
        if hint:
            return hint
        if role not in _BUILTIN_ROLE_EXECUTORS and role_registry.is_coding(role):
            return "mock_eng"
    except Exception:  # noqa: BLE001 — registry unavailable must not break routing
        pass
    return _BUILTIN_ROLE_EXECUTORS.get(role, "mock_intake")


def _linear_next(stages: list[dict[str, Any]], index: int) -> str:
    """Next graph node after stages[index], collapsing parallel members into their wave."""
    if index + 1 >= len(stages):
        return "done"
    nxt = stages[index + 1]
    group = (nxt.get("parallel_group") or "").strip()
    if group and nxt.get("name") not in {"intake", "done"}:
        return wave_node_name(group)
    return nxt["name"]


def _resolve_goto(stages: list[dict[str, Any]], target: str | None) -> str:
    """Map a stage id to the graph node to enter (wave node if parallel member)."""
    if not target:
        return "done"
    if target.startswith("__wave__"):
        return target
    stage = next((s for s in stages if s.get("name") == target), None)
    if not stage:
        return target
    group = (stage.get("parallel_group") or "").strip()
    if group and target not in {"intake", "done"}:
        return wave_node_name(group)
    return target


def _handoff_next_targets(
    stages: list[dict[str, Any]],
    current_stage: str,
    contract_targets: list[str],
) -> list[dict[str, str]]:
    """本阶段可自主交接的落点（提示词下发 + 角色→阶段映射用）。

    规则（与设计文档一致）：落点必须同时满足「plan 已声明节点」+
    「源角色契约白名单」，且只取当前节点之后最近的同角色节点，避免回跳与歧义。
    并行成员返回空：并行环节的下一步由 Manager 汇合决策，agent 不自行交接；
    ``hitl_after`` 节点也不作为自主目标，防止 agent 绕过人工门禁。
    """
    allowed = {t.strip() for t in contract_targets if t and t.strip()}
    if not allowed:
        return []
    current = next((s for s in stages if s.get("name") == current_stage), None)
    if current is None or (current.get("parallel_group") or "").strip():
        return []
    idx = stages.index(current)
    seen_roles: set[str] = set()
    out: list[dict[str, str]] = []
    for stage in stages[idx + 1 :]:
        name = str(stage.get("name") or "")
        role = str(stage.get("role") or "")
        if not name or name == "done" or not role:
            continue
        if role not in allowed or role in seen_roles:
            continue
        if stage.get("hitl_after"):
            continue
        seen_roles.add(role)
        out.append(
            {
                "stage": name,
                "role": role,
                "label": str(stage.get("label") or stage.get("label_en") or name),
            }
        )
    return out


def _wave_next(stages: list[dict[str, Any]], last_member_name: str) -> str:
    """Next graph node after the last member of a parallel wave."""
    idx = next((i for i, s in enumerate(stages) if s["name"] == last_member_name), None)
    if idx is None:
        return "done"
    return _linear_next(stages, idx)


async def _mark_parallel_progress(
    initiative_id: str,
    *,
    stage: str,
    active: list[str] | None = None,
) -> None:
    try:
        from sortie_deck.orchestrator import get_orchestrator

        orch = get_orchestrator()
        ini = orch.get(initiative_id)
        if not ini:
            return
        ini.current_stage = stage
        meta = dict(ini.meta or {})
        if active:
            meta["active_parallel"] = list(active)
        else:
            meta.pop("active_parallel", None)
        ini.meta = meta
        orch.store.upsert(ini)
        await orch._emit(
            ini.id,
            {
                "type": "progress",
                "stage": stage,
                "initiative_id": ini.id,
                "active_parallel": list(active or []),
            },
        )
    except Exception:
        return


def build_graph_from_template(
    template: dict[str, Any],
    registry: ExecutorRegistry,
    artifact_store: ArtifactStore,
    *,
    cfg: Any = None,
):
    stages: list[dict[str, Any]] = list(template.get("stages") or [])
    if not stages:
        raise ValueError("pipeline template has no stages")

    stage_by_name = {s["name"]: s for s in stages}
    stage_index = {s["name"]: i for i, s in enumerate(stages)}

    # Narrow-scope manager: join arbitration / conflict / escalation only.
    mission_manager = MissionManager(stages, use_llm=True)

    def _stage_role(name: str) -> str:
        return str((stage_by_name.get(name) or {}).get("role") or "")

    def _deny_handoff(st: PipelineState, stage_name: str, target: str, reason: str) -> None:
        events = list(st.get("events") or [])
        events.append(
            {
                "stage": stage_name,
                "kind": "transition",
                "message": f"handoff to `{target}` denied — {reason}",
                "data": {"handoff": "denied", "target": target, "reason": reason},
            }
        )
        st["events"] = events

    def _apply_handoff(stage_name: str, st: PipelineState) -> None:
        """Record an agent-initiated handoff on the state, when it is plan-legal.

        Agent switching *is* a graph node jump: the agent's proposal is stored in
        ``handoff_target`` and consumed by the node's conditional edge. Nothing is
        written when the move is not explicitly allowed, so the frozen plan stays
        the floor and HITL gates are never bypassed by an agent.
        """
        cfg = stage_by_name.get(stage_name) or {}
        if cfg.get("hitl_after"):
            # a human gate must stay between this agent and the next stage
            return
        result = st.get("last_result") or {}
        if str(result.get("status") or "ok") != "ok":
            return
        target = str(result.get("proposed_next") or "").strip()
        if not target or target == stage_name or target not in stage_by_name:
            return
        if target == "done":
            # terminal node is reached through the frozen edge / gate, never a jump
            return
        src_role = _stage_role(stage_name)
        dst_role = _stage_role(target)
        try:
            validate_handoff(load_agent_contract(src_role), dst_role)
        except ContractError as exc:
            _deny_handoff(st, stage_name, target, str(exc))
            return
        counts = dict(st.get("handoff_counts") or {})
        if counts.get(target, 0) >= _MAX_HANDOFF_PER_TARGET:
            _deny_handoff(st, stage_name, target, "handoff budget exhausted")
            return
        total = int(st.get("handoff_total") or 0)
        if total >= _MAX_HANDOFF_TOTAL:
            _deny_handoff(st, stage_name, target, "mission handoff budget exhausted")
            return
        counts[target] = counts.get(target, 0) + 1
        events = list(st.get("events") or [])
        events.append(
            {
                "stage": stage_name,
                "kind": "transition",
                "message": f"`{src_role}` handed off to `{target}` (`{dst_role}`)",
                "data": {
                    "handoff": "accepted",
                    "from": src_role,
                    "to": dst_role,
                    "target_stage": target,
                },
            }
        )
        st["handoff_counts"] = counts
        st["handoff_total"] = total + 1
        st["events"] = events
        st["handoff_target"] = _resolve_goto(stages, target)

    async def _apply_join_decision(
        group: str,
        last_member: str,
        branches: list[dict[str, Any]],
        results: list[PipelineState],
        state: PipelineState,
        base: PipelineState,
        default_next: str,
    ) -> None:
        """Manager decides the next node after a parallel wave.

        Two (or more) agents finished at once and neither may arbitrate for the
        other — this is exactly the case the manager owns. The chosen target is
        written to ``handoff_target`` and consumed by the wave's conditional edge.
        """
        statuses: list[BranchStatus] = []
        for br, st in zip(branches, results, strict=True):
            member = br["members"][-1]["name"]
            res = st.get("last_result") or {}
            statuses.append(
                BranchStatus(
                    stage=member,
                    role=_stage_role(member),
                    status=str(res.get("status") or "ok"),
                    message=str(res.get("message") or "")[:200],
                )
            )
        decision = await mission_manager.decide_join(
            group=group,
            last_member=last_member,
            branches=statuses,
            brief=str(state.get("brief") or ""),
            upstream_keys=list((base.get("upstream") or {}).keys()),
        )
        events: list[dict[str, Any]] = list(base.get("events") or [])
        base["events"] = events
        if not decision.target_stage:
            return
        goto = _resolve_goto(stages, decision.target_stage)
        events.append(
            {
                "stage": last_member,
                "kind": "transition",
                "message": (
                    f"manager join decision → `{decision.target_stage}` "
                    f"({decision.action}, confidence {decision.confidence:.2f})"
                ),
                "data": decision.model_dump(mode="json"),
            }
        )
        if goto in {default_next, last_member}:
            # identical to the frozen plan — the default conditional edge wins
            return
        if goto == wave_node_name(group):
            attempts = dict(state.get("wave_attempts") or {})
            used = attempts.get(group, 0)
            if used >= _MAX_WAVE_ATTEMPTS:
                events.append(
                    {
                        "stage": last_member,
                        "kind": "info",
                        "message": f"wave `{group}` reroute suppressed — retry budget exhausted",
                        "data": {"parallel_group": group, "wave_attempts": used},
                    }
                )
                return
            attempts[group] = used + 1
            base["wave_attempts"] = attempts
        base["handoff_target"] = goto

    async def _run_mcp_calls(
        state: PipelineState,
        stage: str,
        toolkit_ids: list[str],
        calls: Any,
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """Execute tool calls an executor *requested* (whitelist + confirm gate).

        Executors declare intent via ``StageResult.meta["mcp_calls"]``; the
        orchestration layer performs the actual call so authorisation lives in
        one place instead of inside every plugin.
        返回 (产物字典, 调用结果列表)——结果列表供 toolkit_usage 事件使用。
        """
        from sortie_deck.settings import settings as _settings

        if not bool(getattr(_settings, "mcp_call_enabled", False)):
            return {}, []
        if not isinstance(calls, list) or not calls:
            return {}, []
        try:
            from sortie_deck.mcp_runtime import McpToolInvoker, resolve_mcp_items
            from sortie_deck.orchestrator import _toolkit_store
        except Exception:  # noqa: BLE001 — MCP path is optional
            return {}, []
        items = resolve_mcp_items(_toolkit_store, list(toolkit_ids))
        invoker = McpToolInvoker(enabled=True)
        results: list[dict[str, Any]] = []
        try:
            # task_id 传 initiative_id：池开启时按任务借出/复用实例，close 只归还不关闭
            await invoker.open(items, task_id=str(state.get("initiative_id") or ""))
            for raw in calls[:10]:
                if not isinstance(raw, dict):
                    continue
                tool = str(raw.get("tool") or "").strip()
                if not tool:
                    continue
                arguments = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
                outcome = await invoker.call(
                    tool, arguments, confirmed=bool(raw.get("confirmed"))
                )
                results.append({"stage": stage, "tool": tool, "arguments": arguments, **outcome})
        except Exception as exc:  # noqa: BLE001 — tool failure must not break the stage
            results.append({"stage": stage, "tool": "*", "ok": False, "error": str(exc)})
        finally:
            await invoker.close()
        if not results:
            return {}, []
        return (
            {"mcp_calls.json": _truncate_output(json.dumps(results, ensure_ascii=False, indent=2))},
            results,
        )

    def _build_session_host(
        state: PipelineState,
        stage: str,
        role: RoleId,
        subroom_events: list[dict[str, Any]],
    ) -> Any | None:
        """Host handed to session-capable executors so they can open a sub-room."""
        from sortie_deck.settings import settings as _settings

        if not bool(getattr(_settings, "subroom_enabled", False)):
            return None
        try:
            from sortie_deck.orchestrator import get_orchestrator
            from sortie_deck.subroom import StageSessionHost, get_subroom_service

            service = get_subroom_service()
            if service is None:
                return None
            orch = get_orchestrator()
            ini = orch.get(state["initiative_id"])
            if ini is None or not ini.roles:
                return None
            roster = [role_key(r.role) for r in ini.roles if role_key(r.role) != role.value]

            async def _ask(role_id: str, prompt: str) -> str:
                target = next((r for r in ini.roles if role_key(r.role) == role_id), None)
                if target is None:
                    return ""
                return await orch._discussion_reply(ini, target, prompt, role.value)

            def _on_event(payload: dict[str, Any]) -> None:
                subroom_events.append({"stage": stage, "role": role.value, **payload})

            async def _escalate(question: str) -> str:
                """升级链：Manager 先裁决，裁决不了才升级真人。

                返回值约定（执行器据此区分两级）：
                - ``manager: <裁决>`` → agent 在本阶段内继续，不挂起；
                - ``escalated to a human — decision ...`` → 图挂起等真人回复；
                - 其余（含空串）→ 无可用通道，执行器降级为工作台门禁。
                """
                try:
                    advice = await mission_manager.advise(
                        stage=stage,
                        question=question,
                        brief=str(state.get("brief") or ""),
                    )
                except Exception:  # noqa: BLE001 — Manager 异常等价于裁决不了
                    advice = None
                if advice is not None and advice.resolution:
                    return f"manager: {advice.resolution}"
                decision = await orch.escalate_to_human(
                    ini.id, question, stage=stage, requested_by=role.value
                )
                if decision is None:
                    return "escalation unavailable (IM is not configured)"
                return f"escalated to a human — decision `{decision.decision_id}`"

            return StageSessionHost(
                initiative_id=ini.id,
                stage=stage,
                role=role.value,
                participants=roster,
                reply=_ask,
                service=service,
                on_event=_on_event,
                escalate_fn=_escalate,
            )
        except Exception:  # noqa: BLE001 — absent host simply means a single-shot executor
            return None

    async def _stage_proactive_recall(
        state: PipelineState, stage: str, role: RoleId, cfg: dict[str, Any]
    ) -> StageRecallOutcome:
        """阶段入口主动召回（模式 1.5）：编排层直接 recall 并注入，不经模型。

        查询 = 角色 + 阶段名 + 阶段标签 + 任务 brief 摘要；召回结果经分数阈值、
        去重（已有绑定/上浮优先）、探活后注入。幂等由 state["stage_recall_done"]
        保证：每阶段只召回一次，重试/HITL 重入命中即跳过。
        fail-soft：开关关闭 / store 缺失 / 任何异常都返回空结果，不中断阶段。
        """
        from sortie_deck.settings import settings as _settings

        if bool((state.get("stage_recall_done") or {}).get(stage)):
            return StageRecallOutcome()
        try:
            from sortie_deck.orchestrator import _toolkit_store, get_orchestrator

            store = _toolkit_store
            policy = None
            try:
                policy = get_orchestrator().org_policy
            except Exception:  # noqa: BLE001 — 无策略时不过滤
                policy = None
            allowed = (
                set(policy.allowed_toolkit_ids)
                if policy and policy.allowed_toolkit_ids
                else None
            )
            role_toolkits = state.get("role_toolkits") or {}
            stage_toolkits_map = state.get("stage_toolkits") or {}
            floats_map = state.get("toolkit_floats") or {}
            bound = set(stage_toolkits_map.get(stage) or []) | set(
                role_toolkits.get(role.value) or []
            )
            floated = set(floats_map.get(stage) or [])
            # 多路聚焦短查询：角色+阶段 / 阶段标签 / 任务 brief 摘要各一路——
            # 长查询会稀释 _score 的归一化得分（见 run_stage_recall docstring）
            brief_snippet = (state.get("brief") or "").strip()[:300]
            queries = [
                " ".join(p for p in (role.value, stage) if p),
                " ".join(
                    p
                    for p in (str(cfg.get("label") or ""), str(cfg.get("label_en") or ""))
                    if p
                ),
                brief_snippet,
            ]
            return await run_stage_recall(
                store,
                stage=stage,
                queries=queries,
                bound_ids=bound,
                floated_ids=floated,
                allowed_ids=allowed,
                min_score=float(getattr(_settings, "loadout_recall_min_score", 0.2)),
                switch=bool(getattr(_settings, "loadout_stage_recall", False)),
            )
        except Exception as exc:  # noqa: BLE001 — 主动召回失败不影响阶段
            logging.getLogger(__name__).warning("stage recall failed (stage=%s): %s", stage, exc)
            return StageRecallOutcome()

    async def run_stage(state: PipelineState, stage: str) -> PipelineState:
        if state.get("stopped"):
            return {**state, "status": "stopped", "stage": stage}

        cfg = stage_by_name.get(stage, {"name": stage, "role": "product"})
        role = _role_id(str(cfg.get("role", "product")))
        executor_name = _executor_name(stage, role.value, state, cfg)
        executor = registry.get(executor_name)
        artifact_dir = str(artifact_store.initiative_dir(state["initiative_id"]) / stage)

        upstream_paths = state.get("upstream", {})
        upstream_content: dict[str, str] = {}
        for kind, rel in upstream_paths.items():
            try:
                upstream_content[kind] = artifact_store.read_text(rel)
            except FileNotFoundError:
                upstream_content[kind] = ""

        role_personas = state.get("role_personas") or {}
        role_toolkits = state.get("role_toolkits") or {}
        role_repos_state = state.get("role_repos") or {}
        stage_toolkits = state.get("stage_toolkits") or {}
        toolkit_briefs = state.get("toolkit_briefs") or {}
        persona = role_personas.get(role.value) or load_persona(role.value)
        toolkit_ids = list(stage_toolkits.get(stage) or role_toolkits.get(role.value) or [])
        toolkit_brief = (
            toolkit_briefs.get(f"stage:{stage}")
            or toolkit_briefs.get(f"role:{role.value}")
            or ""
        )

        # 阶段入口主动召回（模式 1.5）：编排层直接 recall，结果并入绑定集与 brief。
        # 重叠去重在 run_stage_recall 内完成（已有绑定/上浮的条目跳过，用已有工具）。
        recall_out = await _stage_proactive_recall(state, stage, role, cfg)
        if recall_out.brief_append:
            toolkit_brief = f"{toolkit_brief}\n\n{recall_out.brief_append}".strip()
        if toolkit_brief and toolkit_brief not in persona:
            persona = f"{persona}\n\n{toolkit_brief}"
        if recall_out.item_ids:
            toolkit_ids = toolkit_ids + [
                i for i in recall_out.item_ids if i not in toolkit_ids
            ]

        # Agent-driven upstream/downstream consult before executing the node
        consult_artifacts: dict[str, str] = {}
        try:
            from sortie_deck.models import ActorKind
            from sortie_deck.orchestrator import get_orchestrator
            from sortie_deck.stage_consult import run_stage_consult

            orch = get_orchestrator()
            ini = orch.get(state["initiative_id"])
            if ini and stage not in {"intake", "done"}:
                role_titles = {
                    (r.role.value if hasattr(r.role, "value") else str(r.role)): r.title
                    for r in ini.roles
                }

                async def _post(
                    initiative_id: str,
                    *,
                    text: str,
                    actor_kind: str,
                    actor_name: str,
                    role: str | None = None,
                    msg_type: str = "chat",
                    meta: dict | None = None,
                ):
                    kind = (
                        ActorKind.AGENT
                        if actor_kind == "agent"
                        else ActorKind.SYSTEM
                        if actor_kind == "system"
                        else ActorKind.HUMAN
                    )
                    return await orch._post_room(
                        initiative_id,
                        text=text,
                        actor_kind=kind,
                        actor_name=actor_name,
                        role=role,
                        msg_type=msg_type,
                        meta=meta,
                    )

                async def _reply(to_role: str, question: str) -> str:
                    target = next(
                        (
                            r
                            for r in ini.roles
                            if (r.role.value if hasattr(r.role, "value") else str(r.role)) == to_role
                        ),
                        None,
                    )
                    if not target:
                        return "（编制中无此角色，跳过）"
                    return await orch._discussion_reply(
                        ini, target, question, role_titles.get(role.value, role.value)
                    )

                consult_artifacts = await run_stage_consult(
                    ini=ini,
                    initiative_id=ini.id,
                    stage=stage,
                    role=role.value,
                    brief=state["brief"],
                    upstream=upstream_content,
                    role_titles=role_titles,
                    post_room=_post,
                    reply_as_role=_reply,
                )
                if consult_artifacts:
                    from sortie_deck.models import TimelineEvent

                    events_pre = list(state.get("events") or [])
                    events_pre.append(
                        {
                            "stage": stage,
                            "role": role.value,
                            "kind": "consult",
                            "message": "Upstream/downstream consult completed",
                            "data": {"files": list(consult_artifacts.keys())},
                        }
                    )
                    state = {**state, "events": events_pre}
                    orch._append_timeline(
                        ini,
                        TimelineEvent(
                            stage=stage,
                            role=role.value,
                            kind="consult",
                            message="上下游确认沟通已完成",
                            data={"files": list(consult_artifacts.keys())},
                        ),
                    )
                    orch.store.upsert(ini)
        except Exception:
            consult_artifacts = {}

        # Layered memory + contract hints for this stage (incl. sub-room conclusions
        # scoped to this exact stage).
        memory_brief = ""
        contract_targets: list[str] = []
        try:
            from sortie_deck.orchestrator import mission_memory_brief

            memory_brief = mission_memory_brief(
                initiative_id=state["initiative_id"],
                stage=stage,
                role=str(getattr(role, "value", role)),
            )
        except Exception:  # noqa: BLE001 — memory is an enhancement, not a dependency
            memory_brief = ""
        contract = load_agent_contract(role.value)
        if contract is not None:
            contract_targets = list(contract.handoff_targets)
        # 提示词里让模型用「角色」表达交接意图；这里预先把角色解析成 plan 阶段，
        # 执行器只做查表，避免模型编造阶段名（二次校验仍在 _apply_handoff）。
        next_targets = _handoff_next_targets(stages, stage, contract_targets)

        ctx = StageContext(
            initiative_id=state["initiative_id"],
            thread_id=state["thread_id"],
            stage=stage,
            role=role,
            brief=state["brief"],
            human_instruction=state.get("human_instruction"),
            artifact_dir=artifact_dir,
            workspace_dir=state.get("workspace_dir"),
            upstream_artifacts=upstream_content,
            persona=persona,
            permissions={
                "qa_attempt": state.get("qa_attempt", 1),
                **_coding_perm_from_instruction(state.get("human_instruction")),
            },
            toolkit_ids=toolkit_ids,
            toolkit_brief=toolkit_brief,
            repos=[
                RepoConfig.model_validate(r)
                for r in (role_repos_state.get(role.value) or [])
            ],
            memory_brief=memory_brief,
            harness=str(state.get("harness") or ""),
            harness_locked=bool(state.get("harness_locked")),
            handoff_targets=contract_targets,
            next_targets=next_targets,
            # 编排器配置注入：执行器据此定位任务沙盒，不再读进程级全局 settings
            data_dir=str(getattr(cfg, "data_dir", "") or ""),
            worktrees_dir=str(getattr(cfg, "worktrees_dir", "") or ""),
        )

        # Session-capable executors get a host so they can pull a sub-room
        # mid-step, wait for the conclusion, then continue.
        subroom_events: list[dict[str, Any]] = []
        session_host = _build_session_host(state, stage, role, subroom_events)

        async def _execute() -> StageResult:
            return await run_executor(executor, ctx, session_host)

        from sortie_deck.settings import settings as _settings

        timeout = float(getattr(_settings, "stage_timeout_seconds", 900) or 900)

        async def _execute_with_timeout() -> StageResult:
            try:
                return await asyncio.wait_for(_execute(), timeout=timeout)
            except TimeoutError:
                return StageResult(
                    status="fail",
                    message=f"stage `{stage}` timed out after {timeout:.0f}s",
                    artifacts={},
                    proposed_next=stage,
                    meta={"timeout": True, "timeout_seconds": timeout},
                )

        if stage in ENG_BUILD_STAGES:
            result = await ENG_QUEUE.run(_execute_with_timeout)
        else:
            result = await _execute_with_timeout()

        # In-loop 元工具意图：executor 声明 toolkit_search / toolkit_pick，编排层兑现。
        # 先于 _run_mcp_calls 执行：本回合 pick 的 mcp 条目立刻并入调用集合，
        # 模型"点名 → 调用"可以在同一段回复里闭环。
        async def _stage_toolkit_meta() -> ToolkitMetaOutcome:
            try:
                from sortie_deck.orchestrator import _toolkit_store, get_orchestrator

                store = _toolkit_store
                policy = None
                try:
                    policy = get_orchestrator().org_policy
                except Exception:  # noqa: BLE001 — 无策略时不过滤
                    policy = None
                allowed = (
                    set(policy.allowed_toolkit_ids)
                    if policy and policy.allowed_toolkit_ids
                    else None
                )
            except Exception:  # noqa: BLE001 — 元工具路径失败不影响阶段
                return ToolkitMetaOutcome()
            budget = state.get("toolkit_pick_budget") or {}
            floats_map = state.get("toolkit_floats") or {}
            skill_budget = state.get("skill_run_budget") or {}
            return await run_toolkit_meta(
                store,
                stage=stage,
                meta=result.meta,
                allowed_ids=allowed,
                used_picks=int(budget.get(stage, 0)),
                floated=list(floats_map.get(stage) or []),
                skill_used=int(skill_budget.get(stage, 0)),
                workspace_dir=state.get("workspace_dir"),
            )

        toolkit_meta_out = await _stage_toolkit_meta()

        # 内置记忆元工具（memory_search / memory_read）：与 toolkit 元工具同一
        # 声明-兑现模式；读取 ids 记入任务 usage.log（面板可见），预算按阶段累计
        memory_read_used = int((state.get("memory_read_budget") or {}).get(stage, 0))

        async def _stage_memory_meta() -> ToolkitMetaOutcome:
            try:
                from sortie_deck.orchestrator import get_memory_store, get_orchestrator

                mem_store = get_memory_store()
                created_by = None
                try:
                    ini = get_orchestrator().get(state["initiative_id"])
                    created_by = getattr(ini, "created_by", None)
                except Exception:  # noqa: BLE001 — 无任务上下文时按无归属处理
                    created_by = None
            except Exception:  # noqa: BLE001 — 记忆元工具失败不影响阶段
                return ToolkitMetaOutcome()
            return await run_memory_meta(
                mem_store,
                stage=stage,
                meta=result.meta,
                initiative_id=state["initiative_id"],
                created_by=created_by,
                used_reads=memory_read_used,
            )

        memory_meta_out = await _stage_memory_meta()
        if memory_meta_out.records:
            # 并入 toolkit 元工具审计流，随 toolkit_meta.json 一起落产物
            toolkit_meta_out.records.extend(memory_meta_out.records)
        # pick 上浮的 mcp 条目并入本回合调用集合 + 回写 stage_toolkits（方案 3.2 闭环：
        # 上浮后经 McpToolInvoker 可真实调用，后续阶段也持续可用）
        picked_mcp_ids = [
            str(r.get("item_id"))
            for r in toolkit_meta_out.records
            if r.get("op") == "pick" and r.get("floated") and r.get("kind") == "mcp"
        ]
        mcp_toolkit_ids = list(toolkit_ids) + [
            i for i in picked_mcp_ids if i not in toolkit_ids
        ]
        mcp_artifacts, mcp_results = await _run_mcp_calls(
            state, stage, mcp_toolkit_ids, (result.meta or {}).get("mcp_calls")
        )
        stage_toolkits = merge_picked_toolkits(state, stage, toolkit_meta_out.records)
        meta_artifacts: dict[str, str] = {}
        if toolkit_meta_out.records:
            meta_artifacts["toolkit_meta.json"] = json.dumps(
                toolkit_meta_out.records, ensure_ascii=False, indent=2
            )
        skill_run_records = [
            r for r in toolkit_meta_out.records if r.get("op") == "skill_run"
        ]
        if skill_run_records:
            # 脚本执行完整输出（已按 8000 字符截断）单独落产物，供审计回放
            meta_artifacts["skill_runs.json"] = json.dumps(
                skill_run_records, ensure_ascii=False, indent=2
            )

        written: dict[str, str] = dict(upstream_paths)
        for filename, content in {
            **consult_artifacts,
            **result.artifacts,
            **mcp_artifacts,
            **meta_artifacts,
        }.items():
            ref = artifact_store.write_text(state["initiative_id"], stage, filename, content)
            written[filename] = ref.path

        validate_stage_artifacts(stage, result.artifacts)

        # git 回写（任务分支 commit/push + MR）——只影响 meta，不影响阶段判定
        publish_meta = await _maybe_publish(state, ctx, result, stage)
        if publish_meta:
            result.meta = {**(result.meta or {}), **publish_meta}

        events = list(state.get("events") or [])
        events.append(
            {
                "stage": stage,
                "role": role.value,
                "kind": "info",
                "message": result.message,
                "executor": executor_name,
                "status": result.status,
                "qa_attempt": state.get("qa_attempt"),
                "data": {
                    "executor": executor_name,
                    "status": result.status,
                    "qa_attempt": state.get("qa_attempt"),
                    "meta": result.meta or {},
                },
            }
        )
        # Sub-room conclusions are surfaced for observability; the transcript is not.
        events.extend(subroom_events)
        events.extend(toolkit_meta_out.events)
        events.extend(memory_meta_out.events)

        # 工具使用事件：timeline ← events 同步后，任务详情前端按阶段实时展示
        # "本阶段绑定了哪些 tool/mcp/skill、实际调用了哪些 MCP 工具、检索/上浮了什么"。
        if toolkit_ids or mcp_results or toolkit_meta_out.records:
            try:
                from sortie_deck.orchestrator import _toolkit_store as _tk_store

                def _tk_brief_meta(tid: str) -> dict[str, str]:
                    try:
                        it = _tk_store.get(tid)
                        return {"id": tid, "name": it.name, "kind": it.kind}
                    except Exception:  # noqa: BLE001 — 条目被删时展示短 id
                        return {"id": tid, "name": tid[:12], "kind": "tool"}
            except Exception:  # noqa: BLE001 — 无工具箱时退化为裸 id
                def _tk_brief_meta(tid: str) -> dict[str, str]:
                    return {"id": tid, "name": tid[:12], "kind": "tool"}

            def _pick_meta(record: dict[str, Any]) -> dict[str, Any]:
                meta_row = dict(record)
                if record.get("floated"):
                    item_meta = _tk_brief_meta(str(record.get("item_id") or ""))
                    meta_row.update(item_meta)
                return meta_row

            usage = {
                "toolkits": [_tk_brief_meta(tid) for tid in toolkit_ids],
                "mcp_calls": [
                    {
                        "tool": str(r.get("tool") or ""),
                        "ok": bool(r.get("ok")),
                        "blocked": bool(r.get("blocked")),
                    }
                    for r in mcp_results
                ],
                "searches": [
                    {
                        "query": str(r.get("query") or ""),
                        "candidates": len(r.get("candidates") or []),
                    }
                    for r in toolkit_meta_out.records
                    if r.get("op") == "search"
                ],
                "picks": [
                    _pick_meta(r)
                    for r in toolkit_meta_out.records
                    if r.get("op") == "pick"
                ],
            }
            call_names = [c["tool"] for c in usage["mcp_calls"] if c["tool"]]
            events.append(
                {
                    "stage": stage,
                    "role": role.value,
                    "kind": "toolkit_usage",
                    "message": (
                        f"工具使用 · MCP 调用 {len(call_names)} 次"
                        + (f"（{', '.join(call_names[:5])}）" if call_names else "")
                        + f" · 检索 {len(usage['searches'])} 次 · 上浮 {len(usage['picks'])} 项"
                    ),
                    "data": usage,
                }
            )

        # 元工具状态推进：pick 预算按阶段累计；上浮条目去重集合按阶段推进；
        # skill_run 预算按"实际执行过"的次数累计（校验拒绝不消耗预算）
        pick_budget = dict(state.get("toolkit_pick_budget") or {})
        floats_map = dict(state.get("toolkit_floats") or {})
        skill_budget = dict(state.get("skill_run_budget") or {})
        # memory_read 预算回写：按本阶段实际处理的读取次数累计（含被拒的，
        # 防止模型刷预算）
        memory_budget = dict(state.get("memory_read_budget") or {})
        memory_reads_done = sum(
            1 for r in memory_meta_out.records if r.get("op") == "memory_read"
        )
        if memory_reads_done:
            memory_budget[stage] = memory_read_used + memory_reads_done
        if any(r.get("op") == "pick" and r.get("floated") for r in toolkit_meta_out.records):
            pick_budget[stage] = int(pick_budget.get(stage, 0)) + 1
        new_floats = [
            r["item_id"]
            for r in toolkit_meta_out.records
            if r.get("op") == "pick" and r.get("floated")
        ]
        if new_floats:
            floats_map[stage] = list(floats_map.get(stage) or []) + new_floats
        executed_runs = sum(
            1 for r in toolkit_meta_out.records if r.get("op") == "skill_run" and r.get("executed")
        )
        if executed_runs:
            skill_budget[stage] = int(skill_budget.get(stage, 0)) + executed_runs

        # 阶段入口主动召回状态推进：skill 并入 floats（skill_run 免 pick）、
        # mcp 回写 stage_toolkits（mcp_calls 可真实调用）、幂等标记置位、
        # 上浮文本并入 toolkit_briefs["stage:{stage}"]（重入时 brief 不丢失）
        recall_done = dict(state.get("stage_recall_done") or {})
        recall_done[stage] = True
        if recall_out.skill_ids:
            existing_floats = list(floats_map.get(stage) or [])
            floats_map[stage] = existing_floats + [
                i for i in recall_out.skill_ids if i not in existing_floats
            ]
        if recall_out.mcp_ids:
            existing_st = list(stage_toolkits.get(stage) or [])
            stage_toolkits = {
                **stage_toolkits,
                stage: existing_st + [i for i in recall_out.mcp_ids if i not in existing_st],
            }
        if recall_out.item_ids:
            events.append(
                {
                    "stage": stage,
                    "role": role.value,
                    "kind": "toolkit_meta",
                    "message": (
                        f"stage recall: {len(recall_out.item_ids)} item(s) proactively loaded"
                    ),
                    "data": {
                        "tool": recall_out.tool_ids,
                        "mcp": recall_out.mcp_ids,
                        "skill": recall_out.skill_ids,
                    },
                }
            )

        return {
            **state,
            "stage": stage,
            "last_result": result.model_dump(mode="json"),
            "upstream": written,
            "events": events,
            "status": "running",
            "human_instruction": None,
            "toolkit_briefs": (
                # 仅当本阶段发生了上浮才更新 briefs，避免每步无谓覆盖
                dict(state.get("toolkit_briefs") or {}) | (
                    {
                        f"stage:{stage}": "\n\n".join(
                            part
                            for part in (
                                (dict(state.get("toolkit_briefs") or {}).get(f"stage:{stage}") or ""),
                                recall_out.brief_append,
                                toolkit_meta_out.brief_append,
                                memory_meta_out.brief_append,
                            )
                            if part
                        )
                    }
                    if (
                        recall_out.brief_append
                        or toolkit_meta_out.brief_append
                        or memory_meta_out.brief_append
                    )
                    else {}
                )
            ),
            "toolkit_pick_budget": pick_budget,
            "toolkit_floats": floats_map,
            "skill_run_budget": skill_budget,
            "memory_read_budget": memory_budget,
            "stage_recall_done": recall_done,
            # pick 上浮的 mcp 回写：后续阶段的 mcp_calls 才能真实建会话调用
            "stage_toolkits": stage_toolkits,
            # consumed by this node's conditional edge; cleared on every entry
            "handoff_target": None,
            # on_approval 回写标记：仅当 HITL 放行重入时置 True，消费后即清
            "publish_pending": None,
        }

    def make_stage_node(stage_name: str):
        async def node(state: PipelineState) -> PipelineState:
            # 升级链最后一级：上一轮 agent 声明 escalate，先问人再执行本阶段。
            # interrupt 必须发生在 run_stage 之前——恢复时节点会重放，若先跑阶段
            # 再问人，重放会用空答复再跑一次并丢弃 human_instruction。
            pending = state.get("pending_escalation")
            if pending:
                payload = {
                    "stage": stage_name,
                    "prompt": str(pending.get("message") or "")
                    or f"`{stage_name}` escalated a question for a human decision.",
                    "message": str(pending.get("message") or ""),
                    "artifacts": list((state.get("upstream") or {}).keys()),
                    "allowed_actions": [a.value for a in HitlAction],
                    "kind": "escalation",
                    "question": str(pending.get("question") or ""),
                    "options": list(pending.get("options") or []),
                }
                decision_raw = interrupt(payload)
                if isinstance(decision_raw, dict):
                    raw = dict(decision_raw)
                    if hasattr(raw.get("action"), "value"):
                        raw["action"] = raw["action"].value
                    decision = HitlDecision.model_validate(raw)
                else:
                    decision = HitlDecision(action=HitlAction.APPROVE)
                events = list(state.get("events") or [])
                events.append(
                    {
                        "stage": stage_name,
                        "kind": "hitl",
                        "message": f"HITL {decision.action.value} (escalation)",
                        "data": decision.model_dump(mode="json"),
                    }
                )
                state = {**state, "events": events, "pending_escalation": None}
                if decision.action == HitlAction.STOP:
                    return {**state, "stopped": True, "status": "stopped"}
                if decision.action == HitlAction.REJECT:
                    return {**state, "halt": True}
                if decision.action == HitlAction.SWITCH_HARNESS:
                    target = (decision.harness or "").strip()
                    if target:
                        state = {**state, "harness": target}
                # 回答是业务指令，不是权限放行：不提权、不触发 publish。
                # 空回答给兜底文案，避免 agent 认为仍无人答复而再次升级。
                state = {
                    **state,
                    "human_instruction": (
                        decision.instruction or "（操作员已阅，无补充指令，请自行决断）"
                    ),
                }

            if stage_name == "qa_verify":
                attempt = int(state.get("qa_attempt", 1))
                st = await run_stage({**state, "qa_attempt": attempt}, stage_name)
                st["qa_attempt"] = attempt + 1
            else:
                st = await run_stage(state, stage_name)

            result = st.get("last_result") or {}
            if result.get("status") != "needs_hitl":
                # Serial step: the agent itself may hand off (contract-checked);
                # otherwise the frozen plan's default edge decides.
                _apply_handoff(stage_name, st)
                # Manager 不参与这条路由，只在事后标注"该步完成"（纯记录，零 LLM）。
                events = list(st.get("events") or [])
                events.append(
                    {
                        "stage": stage_name,
                        "kind": "info",
                        "message": mission_manager.completion_note(
                            stage=stage_name,
                            status=str(result.get("status") or "ok"),
                            role=_stage_role(stage_name),
                        ),
                        "data": {"manager": "completion"},
                    }
                )
                st["events"] = events
                return st

            # Coding / tool permission block → force HITL so Teams can prompt
            meta = result.get("meta") or {}
            if meta.get("kind") == "escalation":
                # 先记录问题并自环一次；下次进入本节点时在 run_stage 之前问人
                # （见函数开头的 pending 分支），避免 LangGraph 重放丢掉回答。
                events = list(st.get("events") or [])
                events.append(
                    {
                        "stage": stage_name,
                        "kind": "hitl",
                        "message": "escalation requested — waiting for a human",
                        "data": {
                            "kind": "escalation",
                            "question": str(meta.get("question") or ""),
                        },
                    }
                )
                st["events"] = events
                st["pending_escalation"] = {
                    "question": str(meta.get("question") or ""),
                    "options": [str(o) for o in (meta.get("options") or [])],
                    "message": result.get("message", ""),
                }
                st["handoff_target"] = stage_name
                st["status"] = "running"
                return st

            payload = {
                "stage": stage_name,
                "prompt": result.get("message")
                or f"`{stage_name}` needs operator approval (permissions).",
                "message": result.get("message", ""),
                "artifacts": list((st.get("upstream") or {}).keys()),
                "allowed_actions": [a.value for a in HitlAction],
                "kind": "permission" if meta.get("permission_block") else "needs_hitl",
                "retry_permission_mode": meta.get("retry_permission_mode") or "bypassPermissions",
            }
            decision_raw = interrupt(payload)
            if isinstance(decision_raw, dict):
                raw = dict(decision_raw)
                if hasattr(raw.get("action"), "value"):
                    raw["action"] = raw["action"].value
                decision = HitlDecision.model_validate(raw)
            else:
                decision = HitlDecision(action=HitlAction.APPROVE)

            events = list(st.get("events") or [])
            events.append(
                {
                    "stage": stage_name,
                    "kind": "hitl",
                    "message": f"HITL {decision.action.value} (permission)",
                    "data": decision.model_dump(mode="json"),
                }
            )
            update: PipelineState = {**st, "events": events, "status": "running"}

            if decision.action == HitlAction.STOP:
                update["stopped"] = True
                update["status"] = "stopped"
                return update

            if decision.action in {HitlAction.REJECT}:
                update["human_instruction"] = decision.instruction or "Rejected permission grant"
                update["halt"] = True
                return update

            if decision.action == HitlAction.SWITCH_HARNESS:
                # Human authorised a harness change: swap it into the state and
                # re-run this stage on the new harness.
                target = (decision.harness or "").strip()
                if target:
                    update["harness"] = target
                if decision.instruction:
                    update["human_instruction"] = decision.instruction
                update["handoff_target"] = stage_name
                return update

            # approve / rewrite → retry same stage with elevated permission mode
            retry_mode = payload.get("retry_permission_mode") or "bypassPermissions"
            instr = decision.instruction or ""
            update["human_instruction"] = (
                f"{instr}\n[sortie:coding_permission_mode={retry_mode}]".strip()
            )
            # on_approval 回写模式：放行重入编码阶段成功后触发 commit/push + MR
            update["publish_pending"] = True
            # self-loop through the conditional edge
            update["handoff_target"] = stage_name
            return update

        return node

    async def done_node(state: PipelineState) -> PipelineState:
        # 任务结束：回收该任务名下全部 MCP 池实例（含 stateful，防会话滞留）
        try:
            from sortie_deck.mcp_runtime import get_mcp_pool

            await get_mcp_pool().release_task(str(state.get("initiative_id") or ""))
        except Exception:  # noqa: BLE001 — 池回收失败不影响任务收尾
            pass
        events = list(state.get("events") or [])
        events.append({"stage": "done", "kind": "transition", "message": "Initiative completed"})
        return {**state, "stage": "done", "status": "done", "events": events}

    def make_hitl_gate(stage_name: str):
        cfg = stage_by_name[stage_name]
        group = (cfg.get("parallel_group") or "").strip()
        if group:
            members = [s for s in stages if (s.get("parallel_group") or "").strip() == group]
            last = members[-1]["name"] if members else stage_name
            approve_next = _wave_next(stages, last)
        else:
            idx = stage_index[stage_name]
            approve_next = _linear_next(stages, idx)
        on_reject = _resolve_goto(stages, cfg.get("on_reject") or stage_name)
        on_fail = _resolve_goto(stages, cfg.get("on_fail") or on_reject)

        async def hitl_gate(state: PipelineState) -> PipelineState:
            stage = state.get("stage", stage_name)
            result = state.get("last_result") or {}
            payload = {
                "stage": stage,
                "prompt": f"Review outputs of `{stage}` and choose an action.",
                "message": result.get("message", ""),
                "artifacts": list((state.get("upstream") or {}).keys()),
                "allowed_actions": [a.value for a in HitlAction],
            }
            decision_raw = interrupt(payload)
            if isinstance(decision_raw, dict):
                raw = dict(decision_raw)
                if hasattr(raw.get("action"), "value"):
                    raw["action"] = raw["action"].value
                decision = HitlDecision.model_validate(raw)
            else:
                decision = HitlDecision(action=HitlAction.APPROVE)

            events = list(state.get("events") or [])
            events.append(
                {
                    "stage": stage,
                    "kind": "hitl",
                    "message": f"HITL {decision.action.value}",
                    "data": decision.model_dump(mode="json"),
                }
            )
            update: PipelineState = {
                **state,
                "events": events,
                "hitl_decision": decision.model_dump(mode="json"),
                "status": "running",
                "handoff_target": None,
            }

            if decision.action == HitlAction.STOP:
                update["stopped"] = True
                update["status"] = "stopped"
                return update

            if decision.action == HitlAction.EDIT_INSTRUCTION:
                update["human_instruction"] = decision.instruction
                update["handoff_target"] = _resolve_goto(stages, stage)
                return update

            if decision.action == HitlAction.REJECT:
                update["human_instruction"] = decision.instruction or "Rejected — revise"
                update["handoff_target"] = on_reject
                return update

            if decision.action == HitlAction.REROUTE and decision.next_stage:
                update["handoff_target"] = _resolve_goto(stages, decision.next_stage)
                return update

            if decision.action == HitlAction.SWITCH_HARNESS:
                # Authorised harness change between stages: swap, then continue
                # with the normal approve routing.
                target = (decision.harness or "").strip()
                if target:
                    update["harness"] = target
                if decision.instruction:
                    update["human_instruction"] = decision.instruction

            # approve
            if stage == "qa_verify" and not bool(result.get("qa_passed")):
                update["handoff_target"] = on_fail
            else:
                update["handoff_target"] = approve_next
            return update

        return hitl_gate

    def make_wave_node(group: str, members: list[dict[str, Any]]):
        from sortie_deck.parallel import group_wave_branches

        member_names = [m["name"] for m in members]
        branches = group_wave_branches(members)

        async def wave(state: PipelineState) -> PipelineState:
            if state.get("stopped"):
                return {**state, "status": "stopped"}

            initiative_id = state["initiative_id"]
            await _mark_parallel_progress(initiative_id, stage=member_names[0], active=member_names)

            async def _run_branch(branch_members: list[dict[str, Any]]) -> PipelineState:
                st: PipelineState = dict(state)
                for m in branch_members:
                    name = m["name"]
                    await _mark_parallel_progress(initiative_id, stage=name, active=member_names)
                    st = await run_stage(dict(st), name)
                    if (st.get("last_result") or {}).get("status") == "needs_hitl":
                        return st
                    if st.get("stopped"):
                        return st
                return st

            results = await asyncio.gather(*[_run_branch(br["members"]) for br in branches])

            merged_upstream = dict(state.get("upstream") or {})
            merged_events = list(state.get("events") or [])
            last_result: dict[str, Any] = {}
            hitl_member: str | None = None
            hitl_state: PipelineState | None = None

            for br, st in zip(branches, results, strict=True):
                up = dict(st.get("upstream") or {})
                # Bare kind keys: last-wins (fine for trunk artifacts shared into later stages).
                for kind, rel in up.items():
                    if "::" in str(kind):
                        continue
                    merged_upstream[kind] = rel
                # Stage-scoped copies so parallel members don't clobber each other in the
                # initiative artifact index (implementation.md from eng_web vs eng_backend).
                for m in br["members"]:
                    name = m["name"]
                    for kind, rel in up.items():
                        if "::" in str(kind):
                            continue
                        rel_n = str(rel).replace("\\", "/")
                        if f"/{name}/" in f"/{rel_n}/":
                            merged_upstream[f"{name}::{kind}"] = rel
                for ev in st.get("events") or []:
                    if ev not in merged_events:
                        merged_events.append(ev)
                last_result = st.get("last_result") or last_result
                if (st.get("last_result") or {}).get("status") == "needs_hitl" and hitl_member is None:
                    hitl_member = st.get("stage") or br["members"][-1]["name"]
                    hitl_state = st

            tip = branches[-1]["members"][-1]["name"] if branches else member_names[-1]
            await _mark_parallel_progress(initiative_id, stage=tip, active=None)

            base: PipelineState = {
                **state,
                "stage": tip,
                "upstream": merged_upstream,
                "events": merged_events,
                "last_result": last_result,
                "status": "running",
                "human_instruction": None,
                # consumed by this wave's conditional edge
                "handoff_target": None,
            }
            branch_summary = " ∥ ".join(
                "→".join(m["name"] for m in br["members"]) for br in branches
            )
            base["events"] = list(merged_events) + [
                {
                    "stage": tip,
                    "kind": "transition",
                    "message": f"Parallel wave `{group}` finished: {branch_summary}",
                    "data": {
                        "parallel_group": group,
                        "members": member_names,
                        "branches": [
                            {"id": br["id"], "members": [m["name"] for m in br["members"]]}
                            for br in branches
                        ],
                    },
                }
            ]

            if hitl_member is None:
                # Parallel join: nobody can arbitrate for a sibling — the manager decides.
                await _apply_join_decision(
                    group,
                    tip,
                    branches,
                    results,
                    state,
                    base,
                    _wave_default_next(tip),
                )

            if hitl_member and hitl_state is not None:
                # Re-enter the blocked member for permission HITL (single-branch resume)
                result = hitl_state.get("last_result") or {}
                meta = result.get("meta") or {}
                payload = {
                    "stage": hitl_member,
                    "prompt": result.get("message")
                    or f"`{hitl_member}` needs operator approval (permissions).",
                    "message": result.get("message", ""),
                    "artifacts": list(merged_upstream.keys()),
                    "allowed_actions": [a.value for a in HitlAction],
                    "kind": "permission" if meta.get("permission_block") else "needs_hitl",
                    "retry_permission_mode": meta.get("retry_permission_mode") or "bypassPermissions",
                    "parallel_group": group,
                    "parallel_members": member_names,
                }
                decision_raw = interrupt(payload)
                if isinstance(decision_raw, dict):
                    raw = dict(decision_raw)
                    if hasattr(raw.get("action"), "value"):
                        raw["action"] = raw["action"].value
                    decision = HitlDecision.model_validate(raw)
                else:
                    decision = HitlDecision(action=HitlAction.APPROVE)
                if decision.action == HitlAction.STOP:
                    return {**base, "stopped": True, "status": "stopped"}
                if decision.action == HitlAction.REJECT:
                    return {
                        **base,
                        "human_instruction": decision.instruction or "Rejected permission grant",
                        "status": "stopped",
                        "stopped": True,
                    }
                resume_base: PipelineState = {**base}
                if decision.action == HitlAction.SWITCH_HARNESS:
                    # Authorised harness change inside a parallel wave: swap into
                    # state and re-enter the wave (member re-runs on new harness).
                    target = (decision.harness or "").strip()
                    if target:
                        resume_base["harness"] = target
                    if decision.instruction:
                        resume_base["human_instruction"] = decision.instruction
                    resume_base["handoff_target"] = wave_node_name(group)
                    return resume_base
                retry_mode = payload.get("retry_permission_mode") or "bypassPermissions"
                instr = decision.instruction or ""
                return {
                    **base,
                    "human_instruction": (
                        f"{instr}\n[sortie:coding_permission_mode={retry_mode}]".strip()
                    ),
                    # on_approval 回写模式：放行重入成功后触发 publish
                    "publish_pending": True,
                    # re-enter this same wave through the conditional edge
                    "handoff_target": wave_node_name(group),
                }

            return base

        return wave

    graph = StateGraph(PipelineState)
    waves = collapse_to_waves(stages)

    # Register nodes. Gates remember their plan-declared "approve" target so the
    # edge section can wire them with the same conditional-edge router.
    registered: set[str] = set()
    gates: list[tuple[str, str]] = []
    for wave in waves:
        if wave["kind"] == "single":
            s = wave["stage"]
            name = s["name"]
            if name == "done":
                graph.add_node("done", done_node)
                registered.add("done")
                continue
            graph.add_node(name, make_stage_node(name))
            registered.add(name)
            if s.get("hitl_after"):
                gate = f"hitl__{name}"
                graph.add_node(gate, make_hitl_gate(name))
                registered.add(gate)
                gates.append((gate, _linear_next(stages, stage_index[name])))
        else:
            group = wave["group"]
            members = wave["members"]
            wname = wave_node_name(group)
            graph.add_node(wname, make_wave_node(group, members))
            registered.add(wname)
            # Keep individual stage nodes for HITL / inspect resume targets
            for m in members:
                mname = m["name"]
                if mname not in registered:
                    graph.add_node(mname, make_stage_node(mname))
                    registered.add(mname)
            last = members[-1]["name"]
            if members[-1].get("hitl_after") and f"hitl__{last}" not in registered:
                gate = f"hitl__{last}"
                graph.add_node(gate, make_hitl_gate(last))
                registered.add(gate)
                gates.append((gate, _wave_next(stages, last)))

    node_names = set(registered)

    def _router(default_next: str):
        """Route on the node's own decision, falling back to the frozen plan.

        Agent switching *is* a graph node jump: every node announces its next hop
        in ``handoff_target`` and this conditional edge consumes it. A static edge
        would race the jump in LangGraph (both destinations run in one superstep
        and collide on the shared state channels), so there are no static edges
        between stages at all — the frozen plan is simply the router's default.
        """

        def route(state: PipelineState) -> str:
            if state.get("stopped") or state.get("halt"):
                return END
            target = str(state.get("handoff_target") or "").strip()
            if target and target in node_names:
                return target
            return default_next

        return route

    def _next_after(wi: int) -> str:
        if wi + 1 >= len(waves):
            return "done"
        nw = waves[wi + 1]
        return nw["stage"]["name"] if nw["kind"] == "single" else wave_node_name(nw["group"])

    def _wave_default_next(last_member: str) -> str:
        """Plan-declared successor of a parallel wave (gate first when present)."""
        if (stage_by_name.get(last_member) or {}).get("hitl_after"):
            return f"hitl__{last_member}"
        return _wave_next(stages, last_member)

    # Edges across waves. There are **no static edges between stages**: every hop
    # is a conditional edge whose default is the frozen plan, so an agent handoff,
    # a manager join decision and a HITL verdict all travel through one mechanism.
    first_wave = waves[0]
    if first_wave["kind"] == "single":
        graph.add_edge(START, first_wave["stage"]["name"])
    else:
        graph.add_edge(START, wave_node_name(first_wave["group"]))

    for wi, wave in enumerate(waves):
        if wave["kind"] == "single":
            s = wave["stage"]
            name = s["name"]
            if name == "done":
                graph.add_edge("done", END)
                continue
            default_next = f"hitl__{name}" if s.get("hitl_after") else _next_after(wi)
            graph.add_conditional_edges(name, _router(default_next))
        else:
            group = wave["group"]
            wname = wave_node_name(group)
            last = wave["members"][-1]["name"]
            # If the catalog still marks hitl on the last member, enter via its gate
            default_next = (
                f"hitl__{last}" if wave["members"][-1].get("hitl_after") else _next_after(wi)
            )
            graph.add_conditional_edges(wname, _router(default_next))

    # Gates route on the human verdict; the plan's approve target is the default.
    for gate, approve_next in gates:
        graph.add_conditional_edges(gate, _router(approve_next))

    return graph


def build_graph(
    registry: ExecutorRegistry,
    artifact_store: ArtifactStore,
    template_name: str = "default",
    *,
    cfg: Any = None,
):
    tpl = load_pipeline_template(template_name)
    return build_graph_from_template(tpl, registry, artifact_store, cfg=cfg)


async def compile_pipeline(
    registry: ExecutorRegistry,
    artifact_store: ArtifactStore,
    checkpoint_path: Path,
    postgres_uri: str | None = None,
    template_name: str = "default",
    *,
    cfg: Any = None,
):
    """Compile one graph; returns (compiled, resources)."""
    graphs, resources = await compile_pipelines(
        registry,
        artifact_store,
        checkpoint_path,
        postgres_uri=postgres_uri,
        tracks=[template_name],
        cfg=cfg,
    )
    return graphs[template_name], resources


async def compile_pipelines(
    registry: ExecutorRegistry,
    artifact_store: ArtifactStore,
    checkpoint_path: Path,
    postgres_uri: str | None = None,
    tracks: list[str] | None = None,
    *,
    cfg: Any = None,
) -> tuple[dict[str, Any], dict]:
    """Compile multiple track graphs sharing one checkpointer.

    ``cfg``（编排器配置）会透传到每个阶段节点，用于把 data_dir / 任务沙盒根
    注入 StageContext——执行器因此不需要读进程级全局 settings。
    """
    import aiosqlite

    if tracks is None:
        from sortie_deck.pipeline import TRACK_FILES

        track_list = list(TRACK_FILES)
    else:
        track_list = tracks
    resources: dict = {}
    saver = None

    if postgres_uri:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            cm = AsyncPostgresSaver.from_conn_string(postgres_uri)
            saver = await cm.__aenter__()
            await saver.setup()
            resources["cm"] = cm
            resources["saver"] = saver
        except Exception:
            saver = None

    if saver is None:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(checkpoint_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL;")
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        resources["conn"] = conn
        resources["saver"] = saver

    graphs: dict[str, Any] = {}
    for track in track_list:
        builder = build_graph(registry, artifact_store, track, cfg=cfg)
        graphs[track] = builder.compile(checkpointer=saver)
    return graphs, resources
