from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from sortie_deck import observability as obs
from sortie_deck.agent_contract import ContractError, validate_handoff
from sortie_deck.artifacts import ArtifactStore
from sortie_deck.contracts import (
    REQUIRED,
    StageContractError,
    validate_stage_artifacts,
)
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
            actor_user_id=ctx.actor_user_id,
        )
    except Exception as exc:  # noqa: BLE001 — 回写失败不影响阶段结果
        return {"git_publish_errors": [f"publish_repos 异常: {exc}"]}
    if not results:
        return None
    return publish_summary_meta(results)


# 超时/失败兜底扫描时跳过的目录：依赖目录与 repos/（业务仓库里的同名文件不是阶段产物）
_SALVAGE_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "dist", "build", "repos"})
_SALVAGE_MAX_DEPTH = 2

# 过程文件：回复全文 / 上下游沟通记录 / 工具审计。属于排查材料而非交付物，
# 落到 missions/<id>/logs/<stage>/，不进对用户展示的产物目录。
_PROCESS_ARTIFACT_NAMES = frozenset(
    {"reply.md", "consult_log.md", "toolkit_meta.json", "skill_runs.json", "mcp_calls.json"}
)


def _mission_inputs(cfg: Any, initiative_id: str) -> list[dict[str, Any]]:
    """把任务目录的 ``input/manifest.json`` 投影成 ``StageContext.inputs``。

    只做投影不做读取内容：prompt 里给路径，agent 按需 Read。任何异常都返回
    空列表——输入索引是增强项，不能因为磁盘问题让阶段起不来。
    """
    data_dir = str(getattr(cfg, "data_dir", "") or "")
    if not data_dir:
        return []
    try:
        from sortie_deck import mission_input

        return [
            {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "kind": str(row.get("kind") or "other"),
                "size": int(row.get("size") or 0),
                "pinned": bool(row.get("pinned")),
                "stored_path": str(row.get("stored_path") or ""),
                "text_path": str(row.get("text_path") or ""),
                "extract_status": str(row.get("extract_status") or "pending"),
            }
            for row in mission_input.list_input_rows(Path(data_dir), initiative_id)
        ]
    except Exception:  # noqa: BLE001 — 索引失败不影响阶段执行
        logger.warning("mission input 索引读取失败: %s", initiative_id, exc_info=True)
        return []


def _stage_logs_dir(store: ArtifactStore, initiative_id: str, stage: str) -> Path | None:
    """过程文件目录（仅本地存储有；S3 后端无本地目录，返回 None）。"""
    root = getattr(store, "root", None)
    if not root:
        return None
    return Path(str(root)) / initiative_id / "logs" / stage


def _write_process_artifact(target_dir: Path, filename: str, content: str) -> None:
    """写过程文件；失败只告警（审计材料缺失不能影响阶段判定）。"""
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / filename).write_text(content, encoding="utf-8")
    except OSError:
        logger.warning("过程文件写入失败: %s", target_dir / filename, exc_info=True)


def _persist_stage_artifacts(
    store: ArtifactStore,
    initiative_id: str,
    stage: str,
    artifacts: dict[str, str],
) -> dict[str, str]:
    """阶段收尾落盘分流：交付物进产物库，过程文件进 ``logs/<stage>/``。

    返回交付物的相对路径映射；过程文件不返回，因此也不会进 ``upstream``，
    下游阶段拿不到回复全文/审计日志这类噪音。
    """
    written: dict[str, str] = {}
    logs_dir = _stage_logs_dir(store, initiative_id, stage)
    for filename, content in artifacts.items():
        if filename in _PROCESS_ARTIFACT_NAMES and logs_dir is not None:
            _write_process_artifact(logs_dir, filename, content)
            continue
        ref = store.write_text(initiative_id, stage, filename, content)
        written[filename] = ref.path
    return written


def _absolute_artifact_path(store: ArtifactStore, relative_path: str) -> str:
    """把产物引用解析成本地绝对路径，供 prompt 里「按需读取」指路。

    本地存储走 ``store.absolute()``；远端存储（S3）没有本地路径，退化为原引用，
    agent 读不到也不会让阶段失败——prompt 同段落仍给了文件名与字符数。
    """
    absolute = getattr(store, "absolute", None)
    if callable(absolute):
        try:
            return str(absolute(relative_path))
        except Exception:  # noqa: BLE001 — 路径解析属于展示增强，失败不影响执行
            logger.warning("产物绝对路径解析失败: %s", relative_path, exc_info=True)
    root = getattr(store, "root", None)
    if root:
        return str(Path(str(root)) / relative_path)
    return relative_path


def _workspace_candidates(root: Path, name: str) -> list[Path]:
    """按「顶层优先、深度 ≤2、跳过依赖/仓库目录」列出候选文件。"""
    direct = root / name
    if direct.is_file():
        return [direct]
    out: list[Path] = []
    for depth in range(1, _SALVAGE_MAX_DEPTH + 1):
        for path in sorted(root.glob("/".join(["*"] * depth + [name]))):
            rel_parts = path.relative_to(root).parts
            if any(part in _SALVAGE_SKIP_DIRS for part in rel_parts):
                continue
            if path.is_file():
                out.append(path)
    return out


def _validate_or_explain(stage: str, result: StageResult) -> None:
    """校验阶段产物；失败时把执行器的真实失败原因并进异常。

    缺产物通常是次生现象（超时/执行器崩溃 → artifacts 为空）。只报
    "missing artifacts" 会让线上无法判断该扩超时还是改实现，因此执行器
    自己给了失败原因时一并抛出。
    """
    try:
        validate_stage_artifacts(stage, result.artifacts)
    except StageContractError as exc:
        if result.status == "fail" and result.message:
            raise StageContractError(
                stage, f"{exc} (executor failure: {result.message})"
            ) from exc
        raise


def _stage_rules(raw: str | None) -> list[tuple[str, str]]:
    """解析 `stage=value,stage2=value` 形式的阶段规则，保持书写顺序。"""
    rules: list[tuple[str, str]] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        stage, value = part.split("=", 1)
        stage, value = stage.strip(), value.strip()
        if stage and value:
            rules.append((stage, value))
    return rules


def _match_stage_rule(stage: str, raw: str | None) -> str | None:
    """按阶段名取规则值：精确匹配优先，其次 `*` 通配（如 `*_tech_design`）。

    精确优先是为了让「单阶段特例」不被通配抢走；同级按书写顺序取第一条。
    """
    rules = _stage_rules(raw)
    for pattern, value in rules:
        if "*" not in pattern and pattern == stage:
            return value
    for pattern, value in rules:
        if "*" not in pattern:
            continue
        prefix, _, suffix = pattern.partition("*")
        if (
            len(stage) >= len(prefix) + len(suffix)
            and stage.startswith(prefix)
            and stage.endswith(suffix)
        ):
            return value
    return None


def _resolve_stage_disallowed_tools(stage: str, app_cfg: Any = None) -> list[str]:
    """解析本阶段禁用的工具名（空列表 = 不限制）。

    结果由执行器转交 harness；claude_code 直接映射到 SDK 的
    ``disallowed_tools``，agent 连尝试调用的机会都没有。
    """
    from sortie_deck.settings import settings as _settings

    raw = getattr(app_cfg, "stage_disallowed_tools", None)
    if raw is None:
        raw = getattr(_settings, "stage_disallowed_tools", "")
    value = _match_stage_rule(stage, str(raw or ""))
    if not value:
        return []
    return [name.strip() for name in value.replace(";", "|").split("|") if name.strip()]


def _resolve_stage_timeout(
    stage: str,
    stage_cfg: dict[str, Any] | None,
    app_cfg: Any = None,
) -> float:
    """解析单个阶段的超时上限（秒）。

    优先级：阶段 spec 的 timeout_seconds（航线 YAML / 前端编辑阶段均可配）
    > 阶段级覆盖 stage_timeout_overrides（如 `*_tech_design=600`，支持 `*` 通配）
    > 编码类阶段默认（eng_stage_timeout_seconds=3600s）
    > 复验类阶段默认（qa_stage_timeout_seconds=1800s）
    > 全局默认（stage_timeout_seconds=1800s）。
    app_cfg 是编排器实例注入的 Settings，优先于进程级全局 settings，
    保证多实例/单测各用各的超时。
    """
    configured = (stage_cfg or {}).get("timeout_seconds")
    if configured:
        return float(configured)

    from sortie_deck.settings import settings as _settings

    # 阶段级覆盖：文档型阶段（tech_design）只该写方案，不该套编码阶段的量级
    raw_overrides = getattr(app_cfg, "stage_timeout_overrides", None)
    if raw_overrides is None:
        raw_overrides = getattr(_settings, "stage_timeout_overrides", "")
    override = _match_stage_rule(stage, str(raw_overrides or ""))
    if override:
        try:
            seconds = float(override)
        except ValueError:
            logger.warning(
                "阶段超时覆盖配置非法已忽略: stage=%s value=%s", stage, override
            )
        else:
            if seconds > 0:
                return seconds

    # 复验阶段单独取配置（默认与全局一致 30 分钟），便于按项目收紧
    if stage == "qa_verify" or stage.endswith("_verify"):
        fallback = 1800.0
        return float(
            getattr(app_cfg, "qa_stage_timeout_seconds", None)
            or getattr(_settings, "qa_stage_timeout_seconds", fallback)
            or fallback
        )

    is_build = stage in ENG_BUILD_STAGES
    attr = "eng_stage_timeout_seconds" if is_build else "stage_timeout_seconds"
    fallback = 3600.0 if is_build else 1800.0

    return float(
        getattr(app_cfg, attr, None)
        or getattr(_settings, attr, fallback)
        or fallback
    )


def _salvage_stage_artifacts(
    stage: str,
    workspace: Path | None,
    extra_dirs: list[Path] | None = None,
) -> dict[str, str]:
    """从产物目录与 workspace 回收已写出的契约产物（超时/执行器失败的兜底）。

    执行器只在正常返回路径组装 ``result.artifacts``，一旦被 ``asyncio.wait_for``
    取消或直接抛错，磁盘上写好的文件就无人认领。文档型阶段把产物直接写在
    ``artifacts/<stage>/``，所以这里按「产物目录优先、workspace 兜底」扫描；
    只认 ``REQUIRED`` 里的文件名且要求内容非空，避免把半成品当成阶段产物。
    """
    required = REQUIRED.get(stage, [])
    if not required:
        return {}
    roots = [*(extra_dirs or []), workspace]
    found: dict[str, str] = {}
    for name in required:
        for root in roots:
            if root is None or not root.is_dir():
                continue
            for candidate in _workspace_candidates(root, name):
                try:
                    content = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if content.strip():
                    found[name] = content
                    break
            if name in found:
                break
    return found


def _stage_artifact_dir(
    store: ArtifactStore, initiative_id: str, stage: str
) -> Path | None:
    """本阶段产物目录（仅本地存储可用；S3 后端没有可写本地目录，返回 None）。"""
    initiative_dir = getattr(store, "initiative_dir", None)
    if not callable(initiative_dir):
        return None
    try:
        return Path(initiative_dir(initiative_id)) / stage
    except Exception:  # noqa: BLE001 — 产物目录属基础设施，解析失败按不可用处理
        logger.warning("产物目录解析失败: initiative=%s stage=%s", initiative_id, stage)
        return None


def _coding_perm_from_instruction(instruction: str | None) -> dict[str, str]:
    """把 HITL 指令里的提权标记解析成 ``StageContext.permissions`` 片段。

    解析规则收口在 ``permissions.parse_permission_override``，CLI / SDK 两条路径共用；
    非白名单值直接忽略，避免把任意字符串带进 SDK options。
    """
    from sortie_deck.permissions import parse_permission_override

    mode = parse_permission_override(instruction)
    return {"coding_permission_mode": mode} if mode else {}


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
    # 降级自检预算计数（按阶段）：status=fail 且自审未跑时重入本阶段的次数
    self_check_counts: dict[str, int]
    # 连续同一失败签名的次数：非环境类失败的兜底熔断，防止无限返工
    failure_signatures: dict[str, int]
    # Manager-driven parallel wave retry budget per group
    wave_attempts: dict[str, int]
    # Stop the run without marking the initiative as `stopped`
    halt: bool


# Autonomous handoff / wave reroute safety caps (guardrails layer generalises this)
_MAX_HANDOFF_PER_TARGET = 3
_MAX_HANDOFF_TOTAL = 40
_MAX_WAVE_ATTEMPTS = 2

# 降级自检：harness 异常收尾（status=fail）且自审没跑成时，先用这段指令重入本阶段。
# 不扩展需求范围，只要求补齐产物、对齐 diff、跑通自测、如实勾选 checklist。
_SELF_CHECK_INSTRUCTION = (
    "上一次运行异常中断（status=fail，自审未执行），产物可能不完整。请先自检再收尾，"
    "不要新增与 brief 无关的功能：\n"
    "1) 逐个核对 output contract 要求的文件是否存在、内容是否完整；\n"
    "2) 核对 diff.patch 与 workspace 实际改动是否一致，补齐遗漏/未跟踪文件；\n"
    "3) 跑一遍能跑的自测命令（单元测试/启动检查），把失败项修到通过；\n"
    "4) 更新 checklist.md，只勾选真正执行过的项。"
)
_SELF_CHECK_SKIP_PREFIXES = ("deploy",)

# 自审真跑过（有独立结论）才算“不需要自检”；skipped / failed / unparsed 都算没跑成。
_REVIEW_DONE_STATUSES = {"passed", "findings", "blocking"}

# 环境侧失败：重跑同一阶段不会改变结论，必须暂停而不是打回开发
_ENV_BLOCK_KINDS = {"environment_blocked", "auth_blocked"}


def _needs_self_check(stage_name: str, result: dict[str, Any]) -> bool:
    """阶段失败且自审没有任何结论时返回 True：应先自检再放行下游。

    部署类阶段不重跑（重复发布代价高），终态 done 也不适用。
    """
    if stage_name == "done" or stage_name.startswith(_SELF_CHECK_SKIP_PREFIXES):
        return False
    if str(result.get("status") or "ok") != "fail":
        return False
    # 已归因为环境/认证阻塞时不再自检重入：重跑同一阶段拿不到新证据，
    # 交给守卫暂停并由人修环境，避免把自检预算也烧掉。
    if (result.get("meta") or {}).get("failure_kind") in _ENV_BLOCK_KINDS:
        return False
    review = (result.get("meta") or {}).get("review") or {}
    statuses = review.get("statuses") or {}
    return not any(str(v) in _REVIEW_DONE_STATUSES for v in statuses.values())


def _failure_signature(stage: str, result: dict[str, Any]) -> str:
    """把一次阶段失败压成可比对签名，用于识别「同一失败反复重跑」。"""
    meta = result.get("meta") or {}
    reason = str(meta.get("failure_reason") or result.get("message") or "")[:120]
    kind = str(meta.get("failure_kind") or result.get("failure_kind") or "")
    return f"{stage}|{result.get('status')}|{kind}|{reason}"


def _apply_environment_block_guard(
    st: PipelineState, stage_name: str, result: dict[str, Any]
) -> PipelineState | None:
    """环境阻塞第 1 轮即暂停；同一失败连续 N 次兜底暂停。

    返回非 None 表示本次运行已终止（调用方直接返回该 state）；None 表示继续原流程。
    采用 ``stopped`` 终态而非 ``interrupt``：并非每个阶段都有 HITL 门禁，终态在
    任意阶段都可靠，人修好执行环境后重新发起任务即可恢复。
    """
    from sortie_deck.settings import settings

    meta = result.get("meta") or {}
    kind = str(meta.get("failure_kind") or result.get("failure_kind") or "")
    status = str(result.get("status") or "ok")
    if status == "ok" and not kind:
        st["failure_signatures"] = {}
        return None

    sig = _failure_signature(stage_name, result)
    # 只保留当前签名：计数语义是「连续」，换一种失败即重新计
    prior = st.get("failure_signatures") or {}
    count = int(prior.get(sig, 0)) + 1
    st["failure_signatures"] = {sig: count}

    reasons: list[str] = []
    if kind in _ENV_BLOCK_KINDS and settings.env_block_pause_enabled:
        reasons.append(f"环境阻塞（{kind}）：{meta.get('failure_reason') or '无运行时证据'}")
    limit = int(getattr(settings, "max_consecutive_same_failure", 0) or 0)
    if not reasons and limit > 0 and count >= limit:
        reasons.append(f"同一失败连续 {count} 次（{sig[:80]}）")
    if not reasons:
        return None

    why = "；".join(reasons)
    events = list(st.get("events") or [])
    events.append(
        {
            "stage": stage_name,
            "kind": "error",
            "message": f"已自动暂停，避免无限返工：{why}。修复执行环境后重新发起任务。",
            "data": {"failure_kind": kind or "unknown", "consecutive": count, "reason": why},
        }
    )
    logger.warning(
        "阶段失败触发自动暂停: initiative=%s stage=%s kind=%s consecutive=%s",
        st.get("initiative_id"),
        stage_name,
        kind or "unknown",
        count,
    )
    return {
        **st,
        "events": events,
        "stopped": True,
        "status": "stopped",
        "handoff_target": None,
        "failure_signatures": {sig: count},
        "last_result": {
            **result,
            "message": f"{result.get('message') or ''}（已自动暂停：{why}）",
        },
    }


def _gate_recommendation(
    stage: str, result: dict[str, Any], on_fail: str
) -> dict[str, Any]:
    """门禁推荐动作：QA 已判失败时直接给「返工」，避免人工误点「批准」。

    批准在 ``qa_passed is False`` 时同样按 ``on_fail`` 打回（见 hitl_gate），
    按钮若仍叫「放行」会让人以为跳过了返工，因此把推荐动作显式带进 payload。
    """
    if stage != "qa_verify":
        return {}
    passed = result.get("qa_passed")
    if passed is None:
        return {}
    if passed:
        return {"verdict": {"passed": True}, "recommended_action": {"action": "approve"}}
    blocked = (result.get("meta") or {}).get("failure_kind") in _ENV_BLOCK_KINDS
    return {
        "verdict": {"passed": False},
        # 守卫被显式关闭时也要让人看清楚：这是环境问题，不是代码没写好
        **(
            {
                "blocked_reason": (
                    "执行环境阻塞（命令审批 / 无运行时证据），返工不会改变结论；"
                    "请先修复执行环境再恢复任务。"
                )
            }
            if blocked
            else {}
        ),
        "recommended_action": {
            "action": "reroute",
            "next_stage": on_fail,
            "label": "返工",
            "hint": f"QA 判定未通过；批准同样会回到 `{on_fail}`，直接返工即可。",
        },
    }


# Built-in role → mock executor fallback. Custom roles are resolved through
# ``sortie_deck.roles.RoleRegistry`` instead of being hard-coded here.
_BUILTIN_ROLE_EXECUTORS: dict[str, str] = {
    "product": "mock_product",
    "design": "mock_design",
    "eng": "mock_eng",
    "qa": "mock_qa",
    "deploy": "mock_deploy",
}


def repos_for_role(
    role_repos: dict[str, list[dict[str, Any]]], role: str, stage: str = ""
) -> list[dict[str, Any]]:
    """取本阶段应 clone 的仓库：精确角色 → 阶段名 → 该角色下所有细分槽位。

    规划官会按 ``eng_web`` / ``eng_backend`` 这类细分槽位绑定仓库，但精简航线
    只有单个 ``eng`` 阶段；若只按 ``role.value`` 精确匹配，绑定会被静默丢掉。
    """
    rows = list(role_repos.get(role) or [])
    if rows:
        return rows
    if stage and role_repos.get(stage):
        return list(role_repos[stage])
    return [
        repo
        for key, items in role_repos.items()
        if key.startswith(f"{role}_")
        for repo in items
    ]


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


# 门禁动作的语义说明：给 agent 读，避免模型只看到一个 action 枚举就盲批
HITL_ACTION_HELP: dict[str, str] = {
    "approve": "认可本阶段产物，继续下一阶段",
    "reject": "不认可，回到本阶段重做",
    "edit_instruction": "带补充指令重做本阶段（instruction 必填）",
    "stop": "终止整个任务",
    "reroute": "换一条航线，跳到指定阶段",
    "switch_harness": "切换执行器（需要人工授权）",
}

# 命中这些前缀的阶段视为不可逆（对外发布/部署）
_IRREVERSIBLE_STAGE_TOKENS = ("deploy", "ship", "release", "publish")


def stage_is_irreversible(stage: str, cfg: dict[str, Any] | None = None) -> bool:
    """阶段放行后是否不可撤销；`irreversible: true` 可显式覆盖。"""
    if (cfg or {}).get("irreversible"):
        return True
    lowered = (stage or "").strip().lower()
    return any(lowered.startswith(token) for token in _IRREVERSIBLE_STAGE_TOKENS)


def describe_stage_risk(stage: str, cfg: dict[str, Any] | None = None) -> str:
    if stage_is_irreversible(stage, cfg):
        return "会对外发布/部署，执行后不可撤销"
    return "只读评审：产物可回看，驳回会回退到本阶段重做"


def summarize_artifacts(
    stage: str, upstream: dict[str, str] | None, result: dict[str, Any] | None = None
) -> str:
    """一句话产物摘要：文件名列表，给门禁 payload 用。

    优先用阶段自身产出的 artifacts（更贴近「这次要审什么」），
    缺失时退回 upstream 继承的产物。
    """
    names: list[str] = []
    produced = (result or {}).get("artifacts")
    if isinstance(produced, (dict, list)):
        names.extend(str(p).rsplit("/", 1)[-1] for p in produced)
    if not names:
        names.extend(str(p).rsplit("/", 1)[-1] for p in (upstream or {}))
    if not names:
        return ""
    # 去重保序，最多列 5 个，其余折叠成 +N
    seen: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    head = "、".join(seen[:5])
    extra = f" 等 {len(seen)} 个文件" if len(seen) > 5 else ""
    return f"`{stage}` 产出 {head}{extra}"


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

    # 编排器注入的配置（多实例/单测各用自己的 data_dir 与超时）。
    # 内部 `cfg` 变量被阶段配置复用，故另存一个名字避免遮蔽。
    app_cfg = cfg

    stage_by_name = {s["name"]: s for s in stages}
    stage_index = {s["name"]: i for i, s in enumerate(stages)}

    # Narrow-scope manager: join arbitration / conflict / escalation only.
    mission_manager = MissionManager(stages, use_llm=True)

    def _stage_role(name: str) -> str:
        return str((stage_by_name.get(name) or {}).get("role") or "")

    def _stage_workspace(st: PipelineState) -> Path | None:
        """阶段沙盒目录：state 注入值优先，缺省按 mission 布局推导。"""
        raw = st.get("workspace_dir")
        if raw:
            return Path(str(raw))
        initiative_id = str(st.get("initiative_id") or "")
        data_dir = getattr(app_cfg, "data_dir", None)
        if not initiative_id or not data_dir:
            return None
        from sortie_deck.layout import mission_workspace_dir

        return mission_workspace_dir(Path(data_dir), initiative_id)

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
                with obs.span(
                    "execute_tool",
                    **{
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": tool,
                        # 同一工具可能在本阶段被调多次，用序号区分，便于对齐调用结果
                        "gen_ai.tool.call.id": f"{stage}:{tool}:{len(results)}",
                        "sortie.tool.source": "mcp",
                        "sortie.stage": stage,
                    },
                ) as tool_span:
                    outcome = await invoker.call(
                        tool, arguments, confirmed=bool(raw.get("confirmed"))
                    )
                    # 工具失败被吞成 outcome.ok=False 返回，不会抛异常，需显式标错
                    if not outcome.get("ok", True):
                        obs.mark_failure(
                            tool_span, str(outcome.get("error") or "tool call failed")
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
        from sortie_deck.subroom import stage_allows_subroom

        if not bool(getattr(_settings, "subroom_enabled", False)):
            return None
        # 阶段级禁拉群（默认含 intake）：host 为 None 时执行器不会注入
        # subroom 协议，agent 即使声明了拉群意图也不会被兑现。
        if not stage_allows_subroom(stage):
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
            with obs.span(
                "retrieval.recall",
                **{
                    "gen_ai.operation.name": "retrieval",
                    "sortie.stage": stage,
                    "sortie.query_count": len(queries),
                },
            ) as recall_span:
                recall_outcome = await run_stage_recall(
                    store,
                    stage=stage,
                    queries=queries,
                    bound_ids=bound,
                    floated_ids=floated,
                    allowed_ids=allowed,
                    min_score=float(getattr(_settings, "loadout_recall_min_score", 0.2)),
                    switch=bool(getattr(_settings, "loadout_stage_recall", False)),
                )
                obs.set_attributes(
                    recall_span, {"sortie.hit_count": len(recall_outcome.item_ids)}
                )
                return recall_outcome
        except Exception as exc:  # noqa: BLE001 — 主动召回失败不影响阶段
            logging.getLogger(__name__).warning("stage recall failed (stage=%s): %s", stage, exc)
            return StageRecallOutcome()

    async def run_stage(state: PipelineState, stage: str) -> PipelineState:
        """阶段执行入口：包一层 stage span 后转交 ``_run_stage``。

        span 放在这里而不是 ``make_stage_node``：并行波次（``_run_branch``）直接
        调 ``run_stage``、绕过节点函数，只有这一层能同时覆盖串行 / 并行 / QA
        重试三条路径，且不用改任何执行器插件。
        """
        stage_cfg = stage_by_name.get(stage, {"name": stage, "role": "product"})
        stage_role = _role_id(str(stage_cfg.get("role", "product")))
        # 阶段入口立刻通知前端（harness / mock / coding CLI 共用）：
        # 只靠 current_stage 落库要等阶段结束，运行中界面会长时间没有反馈。
        initiative_id = str(state.get("initiative_id") or "")
        started_at = ""
        # 指标采集：墙钟起点用于甘特图定位，单调时钟用于耗时（不受系统时间调整影响）。
        # tool_calls 由 _stage_progress 在 phase=activity 时累加。
        metric_started = time.time()
        metric_started_mono = time.monotonic()
        metric_stats: dict[str, int] = {"tool_calls": 0}

        def _persist_metrics(result_state: PipelineState | None) -> None:
            """阶段收尾写指标（旁路）：失败只告警，绝不影响任务执行。"""
            try:
                from sortie_deck.orchestrator import get_orchestrator

                result = (result_state or {}).get("last_result") or {}
                result_meta = result.get("meta") or {}
                review = result_meta.get("review") or {}
                get_orchestrator().record_stage_metric(
                    initiative_id=initiative_id,
                    stage=stage,
                    role=stage_role.value,
                    harness=str(state.get("harness") or ""),
                    status=str(result.get("status") or ("error" if result_state is None else "")),
                    started_at=metric_started,
                    ended_at=time.time(),
                    tool_calls=int(metric_stats.get("tool_calls") or 0),
                    review_rounds=int(review.get("rounds") or 0),
                    review_verdict=str(review.get("verdict") or ""),
                    meta={
                        "timeout": bool(result_meta.get("timeout")),
                        "salvaged_artifacts": list(
                            result_meta.get("salvaged_artifacts") or []
                        ),
                        "qa_attempt": int(state.get("qa_attempt") or 1),
                        "duration_mono_ms": int(
                            (time.monotonic() - metric_started_mono) * 1000
                        ),
                    },
                )
            except Exception:  # noqa: BLE001 — 指标是旁路
                logging.getLogger(__name__).warning(
                    "阶段指标采集失败 initiative=%s stage=%s",
                    initiative_id,
                    stage,
                    exc_info=True,
                )

        try:
            from sortie_deck.orchestrator import get_orchestrator

            orch = get_orchestrator()
            # 先落库再推送：刷新页面后前端用这个时间戳续算已等待时长，
            # 只放 SSE 事件里会随刷新丢失，计时又从 0 开始。
            try:
                started_at = orch.mark_stage_started(initiative_id, stage)
            except Exception:  # noqa: BLE001 — 时间戳落库失败不影响阶段执行
                logging.getLogger(__name__).debug(
                    "stage_started_at 落库失败: initiative=%s stage=%s",
                    initiative_id,
                    stage,
                    exc_info=True,
                )
            await orch._emit(
                initiative_id,
                {
                    "type": "stage_progress",
                    "initiative_id": initiative_id,
                    "stage": stage,
                    "role": stage_role.value,
                    "phase": "stage_start",
                    "started_at": started_at,
                    "executor": _executor_name(stage, stage_role.value, state, stage_cfg),
                },
            )
        except Exception:  # noqa: BLE001 — 进度推送失败不影响阶段执行
            logging.getLogger(__name__).debug(
                "stage_start 推送失败: initiative=%s stage=%s",
                initiative_id,
                stage,
                exc_info=True,
            )
        with obs.span(
            "stage.run",
            **{
                "sortie.initiative_id": str(state.get("initiative_id") or ""),
                "sortie.stage": stage,
                "sortie.role": stage_role.value,
                "sortie.executor": _executor_name(stage, stage_role.value, state, stage_cfg),
                "sortie.qa_attempt": int(state.get("qa_attempt") or 1),
            },
        ) as stage_span:
            result_state: PipelineState | None = None
            try:
                result_state = await _run_stage(state, stage, metric_stats)
            finally:
                # 指标必须在阶段结束的第一时间落库：超时/异常也要留痕
                _persist_metrics(result_state)
            # 超时/契约失败/needs_hitl 都是「转成返回值的失败」，span() 抓不到异常，
            # 必须显式按 result.status 落 ERROR，否则 trace 全绿而任务实际停了。
            obs.mark_result(stage_span, result_state.get("last_result") or {})
            return result_state

    async def _run_stage(
        state: PipelineState, stage: str, stats: dict[str, int] | None = None
    ) -> PipelineState:
        if state.get("stopped"):
            return {**state, "status": "stopped", "stage": stage}

        cfg = stage_by_name.get(stage, {"name": stage, "role": "product"})
        role = _role_id(str(cfg.get("role", "product")))
        executor_name = _executor_name(stage, role.value, state, cfg)
        executor = registry.get(executor_name)
        # 产物目录（本地存储才有；S3 后端没有可写本地目录，执行器退回旧行为）
        stage_dir = _stage_artifact_dir(artifact_store, state["initiative_id"], stage)
        artifact_dir = str(stage_dir) if stage_dir else ""

        upstream_paths = state.get("upstream", {})
        upstream_content: dict[str, str] = {}
        upstream_abs: dict[str, str] = {}
        for kind, rel in upstream_paths.items():
            try:
                upstream_content[kind] = artifact_store.read_text(rel)
            except FileNotFoundError:
                upstream_content[kind] = ""
            upstream_abs[kind] = _absolute_artifact_path(artifact_store, rel)

        role_personas = state.get("role_personas") or {}
        role_toolkits = state.get("role_toolkits") or {}
        role_repos_state = state.get("role_repos") or {}
        stage_repos = repos_for_role(role_repos_state, role.value, stage)
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

        async def _stage_progress(payload: dict[str, Any]) -> None:
            """执行器内的实时进度 → ``stage_progress`` SSE。

            阶段主回合可能跑十几分钟、自审还要多轮，只靠最终的 update 事件，
            前端除了「出击中」看不到任何变化。这里把每个子步骤即时推出去。

            同时累计工具步数（只算 ``phase=activity``）：``stage_start`` /
            ``stage_end`` / ``review_*`` 是阶段控制事件，不是工具调用。
            """
            try:
                if stats is not None and payload.get("phase") == "activity":
                    stats["tool_calls"] = int(stats.get("tool_calls") or 0) + 1
                from sortie_deck.orchestrator import get_orchestrator

                await get_orchestrator()._emit(
                    state["initiative_id"],
                    {
                        "type": "stage_progress",
                        "initiative_id": state["initiative_id"],
                        "stage": stage,
                        "role": role.value,
                        **payload,
                    },
                )
            except Exception:  # noqa: BLE001 — 进度推送失败不能影响阶段执行
                logging.getLogger(__name__).debug(
                    "stage_progress 推送失败: initiative=%s stage=%s",
                    state.get("initiative_id"),
                    stage,
                    exc_info=True,
                )

        # 超时解析：阶段 spec 显式配置 > 编码类默认 3600s > 全局默认 1800s
        timeout = _resolve_stage_timeout(stage, stage_by_name.get(stage), app_cfg)

        ctx = StageContext(
            initiative_id=state["initiative_id"],
            actor_user_id=state.get("actor_user_id") or "",
            thread_id=state["thread_id"],
            stage=stage,
            role=role,
            brief=state["brief"],
            human_instruction=state.get("human_instruction"),
            artifact_dir=artifact_dir,
            workspace_dir=state.get("workspace_dir"),
            upstream_artifacts=upstream_content,
            upstream_paths=upstream_abs,
            persona=persona,
            permissions={
                "qa_attempt": state.get("qa_attempt", 1),
                **_coding_perm_from_instruction(state.get("human_instruction")),
            },
            toolkit_ids=toolkit_ids,
            toolkit_brief=toolkit_brief,
            inputs=_mission_inputs(app_cfg, state["initiative_id"]),
            repos=[RepoConfig.model_validate(r) for r in stage_repos],
            memory_brief=memory_brief,
            harness=str(state.get("harness") or ""),
            harness_locked=bool(state.get("harness_locked")),
            handoff_targets=contract_targets,
            next_targets=next_targets,
            # 编排器配置注入：执行器据此定位任务沙盒，不再读进程级全局 settings
            data_dir=str(getattr(app_cfg, "data_dir", "") or ""),
            worktrees_dir=str(getattr(app_cfg, "worktrees_dir", "") or ""),
            # 阶段内实时进度（起止 / 自审轮次）经此回调推 SSE
            progress_cb=_stage_progress,
            # 自审必须在这条总预算内收尾，不能反过来把阶段顶成超时
            stage_timeout_seconds=timeout,
            # 阶段级工具禁用（tech_design 禁 Bash）：执行器转交 harness 生效
            disallowed_tools=_resolve_stage_disallowed_tools(stage, app_cfg),
        )

        # Session-capable executors get a host so they can pull a sub-room
        # mid-step, wait for the conclusion, then continue.
        subroom_events: list[dict[str, Any]] = []
        session_host = _build_session_host(state, stage, role, subroom_events)

        async def _execute() -> StageResult:
            return await run_executor(executor, ctx, session_host)

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

        # 失败/超时兜底：执行器被取消时不会组装 artifacts（harness 由回复文本合成），
        # 但 agent 已写进 workspace 的契约文件仍然有效。回收后可继续推进，
        # 避免 15 分钟的产出只因超时被整体丢弃。
        if result.status == "fail":
            # 产物目录优先（文档型阶段直写），workspace 兜底（编码型阶段）
            salvage_extra = [
                p
                for p in (_stage_artifact_dir(artifact_store, state["initiative_id"], stage),)
                if p is not None
            ]
            salvaged = _salvage_stage_artifacts(
                stage, _stage_workspace(state), salvage_extra
            )
            if salvaged:
                failure_message = result.message
                result.artifacts = {**salvaged, **result.artifacts}
                result.status = "ok"
                result.message = (
                    f"{failure_message} (recovered artifacts from workspace: "
                    f"{', '.join(sorted(salvaged))})"
                )
                result.meta = {
                    **(result.meta or {}),
                    "failure_message": failure_message,
                    "salvaged_artifacts": sorted(salvaged),
                }
                logger.warning(
                    "阶段未正常返回，已从 workspace 回收产物: initiative=%s stage=%s files=%s reason=%s",
                    state.get("initiative_id"),
                    stage,
                    sorted(salvaged),
                    failure_message,
                )

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
        # 分流落盘：交付文档进产物库，过程文件（回复全文/沟通记录/工具审计）进 logs/
        written.update(
            _persist_stage_artifacts(
                artifact_store,
                state["initiative_id"],
                stage,
                {
                    **consult_artifacts,
                    **result.artifacts,
                    **mcp_artifacts,
                    **meta_artifacts,
                },
            )
        )

        # needs_hitl 是「停在这里等人」，不是「本阶段已交付」：升级/权限拦截的结果
        # 只带 reply.md 之类过程文件，按交付契约校验会把一次正常的人工升级判成
        # 流水线失败（2026-11-04 qa_verify 事故：决策已生成、状态已置 waiting_hitl，
        # 随后被这里抛出的 StageContractError 覆盖成 failed，人工答复无处可恢复）。
        if result.status != "needs_hitl":
            _validate_or_explain(stage, result)

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
        # 兜底回收要在时间线上留下痕迹：否则"超时后仍成功"会被误当成正常完成
        salvaged_files = list((result.meta or {}).get("salvaged_artifacts") or [])
        if salvaged_files:
            events.append(
                {
                    "stage": stage,
                    "role": role.value,
                    "kind": "info",
                    "message": (
                        "⚠️ 阶段未正常返回（"
                        f"{(result.meta or {}).get('failure_message') or 'executor failure'}），"
                        f"已从 workspace 回收产物：{', '.join(salvaged_files)}"
                    ),
                    "data": {
                        "salvaged_artifacts": salvaged_files,
                        "failure_message": (result.meta or {}).get("failure_message"),
                    },
                }
            )
        # 跨阶段遗留清理也要留痕：否则「这一阶段为什么没读到上一版 PRD」无从追溯
        stale_moved = list((result.meta or {}).get("stale_moved") or [])
        if stale_moved:
            events.append(
                {
                    "stage": stage,
                    "role": role.value,
                    "kind": "info",
                    "message": (
                        "已清理 workspace 里的跨阶段遗留契约文件："
                        f"{', '.join(stale_moved)}（归档在 .archive/）"
                    ),
                    "data": {"stale_moved": stale_moved},
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
            # 环境阻塞 / 连续同一失败：在任何返工、自检、门禁之前先止血，
            # 否则会被 qa_passed=False 打回开发，形成多轮无效重跑。
            guard = _apply_environment_block_guard(st, stage_name, result)
            if guard is not None:
                return guard
            # 降级自检：上一次运行失败、自审又没跑成时，先把产物补齐再放行下游，
            # 否则 QA 只能对着一份残缺交付判 fail，白跑一整轮（9 分钟量级）。
            if _needs_self_check(stage_name, result):
                from sortie_deck.settings import settings as _settings

                used = int((st.get("self_check_counts") or {}).get(stage_name) or 0)
                budget = int(getattr(_settings, "stage_self_check_attempts", 0) or 0)
                if used < budget:
                    counts = dict(st.get("self_check_counts") or {})
                    counts[stage_name] = used + 1
                    events = list(st.get("events") or [])
                    events.append(
                        {
                            "stage": stage_name,
                            "kind": "error",
                            "message": (
                                "阶段以 fail 收尾且自审未执行，先自检再放行"
                                f"（{used + 1}/{budget}）"
                            ),
                            "data": {
                                "self_check": True,
                                "attempt": used + 1,
                                "status": str(result.get("status") or ""),
                            },
                        }
                    )
                    logger.warning(
                        "阶段降级，触发自检: initiative=%s stage=%s attempt=%s/%s",
                        state.get("initiative_id"),
                        stage_name,
                        used + 1,
                        budget,
                    )
                    return {
                        **st,
                        "events": events,
                        "self_check_counts": counts,
                        "human_instruction": _SELF_CHECK_INSTRUCTION,
                        "handoff_target": stage_name,
                        "status": "running",
                    }

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
                # 结构化门禁信息：CLI / MCP / 前端都用同一份，避免 agent 盲批
                "artifact_summary": summarize_artifacts(
                    stage, state.get("upstream"), result
                ),
                "risk": describe_stage_risk(stage, cfg),
                "irreversible": stage_is_irreversible(stage, cfg),
                "action_help": dict(HITL_ACTION_HELP),
                # QA 已判失败时把「返工」作为推荐动作，避免按钮写成「放行」
                **_gate_recommendation(stage, result, on_fail),
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
            # 只有 QA 明确宣告失败才打回；未宣告（真实 harness 没给结论 / 旧执行器）
            # 交由人工门禁决定，否则"批准了却永远回到开发"会形成死循环
            if stage == "qa_verify" and result.get("qa_passed") is False:
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
