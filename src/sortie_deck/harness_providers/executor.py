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
import inspect
import json
import logging
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from sortie_deck.models import CODING_ROLE_VALUES, ENG_BUILD_STAGES, StageContext, StageResult


async def _emit_progress(ctx: StageContext, payload: dict[str, Any]) -> None:
    """把执行器内的实时进度交给编排层（→ stage_progress SSE）。

    ``progress_cb`` 由 graph 注入；直连单测/CLI 调用没有回调，静默跳过。
    进度是观测增强项，回调失败绝不能反过来打断阶段。
    """
    cb = getattr(ctx, "progress_cb", None)
    if cb is None:
        return
    try:
        result = cb(
            {
                **payload,
                "stage": ctx.stage,
                "role": str(getattr(ctx.role, "value", ctx.role)),
            }
        )
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 — 观测失败不影响执行
        logger.debug("阶段进度回调失败: stage=%s phase=%s", ctx.stage, payload.get("phase"))


# 自审最小可用轮预算 / 阶段收尾预留：低于这个额度就别审了，纯浪费阶段预算
_REVIEW_MIN_ROUND_SECONDS = 90.0
_REVIEW_TAIL_RESERVE_SECONDS = 60.0


def _review_timeout_within_budget(
    ctx: StageContext, started: float, timeout: float, rounds: int
) -> float | None:
    """把单轮自审超时压进阶段剩余预算；压不下来返回 None（整段跳过自审）。

    实测（2026-11-03 HK，claude_code）：主体回合 324s + 第 1 轮超时 305s +
    第 2 轮跑到一半，把 900s 阶段预算吃光，阶段被判超时失败——已经产出的
    代码只能走产物回收。自审是增强项，绝不能反过来打死阶段。
    """
    total = float(getattr(ctx, "stage_timeout_seconds", 0.0) or 0.0)
    # started<=0：老调用方没传起点，无法计算已耗时，不干预
    if total <= 0 or timeout <= 0 or started <= 0:
        return timeout
    remaining = total - (time.monotonic() - started) - _REVIEW_TAIL_RESERVE_SECONDS
    if remaining <= _REVIEW_MIN_ROUND_SECONDS:
        return None
    per_round = min(timeout, remaining / max(1, rounds))
    # 压到自适应值一半以下：这一轮大概率还是超时，不如跳过把预算留给阶段收尾
    if per_round < max(_REVIEW_MIN_ROUND_SECONDS, timeout * 0.5):
        return None
    return per_round

_TOOLKIT_META_PROTOCOL = """Toolkit meta-tool (in-loop capability requests):
If you are missing a capability (tool / MCP / skill), you may request it from the
mission toolkit library instead of inventing one:
1. Ask for candidates: {"toolkit_search": {"query": "<free text>", "kinds": ["tool", "mcp", "skill"]}}
2. The orchestrator replies with candidate summaries in your toolkit brief.
3. To use one, declare: {"toolkit_pick": {"item_id": "<id from candidates>"}}
   Validated items are floated into your context (budget: 8 picks per stage).
   MCP servers picked now are mounted from the NEXT stage on — a running session
   cannot attach new MCP servers mid-flight, so do not block waiting for them.
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

# --- 阶段产物直写（运行期）------------------------------------------------------
# 文档型阶段的契约产物就是文档本身，把 cwd 直接指向 artifacts/<stage>/，写入即进
# 任务档案；workspace 只保留代码与仓库副本（见
# docs/plans/2026-10-30-artifact-direct-write.md）。
_DOC_STAGE_NAMES = frozenset({"intake", "product_prd", "qa_cases", "design_ui"})
_UPSTREAM_DIRNAME = ".upstream"
_RUNS_DIRNAME = ".runs"
_WORKSPACE_LINK = "workspace"
# 跨阶段遗留文件的归档目录（放在 workspace 内，随沙盒一起回收）
_ARCHIVE_DIRNAME = ".archive"
# 本阶段越界产物的归档目录（放在阶段产物目录内，不参与产物索引）
_STRAY_DIRNAME = ".stray"


def _is_doc_stage(stage: str) -> bool:
    """是否文档型阶段（产物为 md/json 文档，无代码改动）。

    intake 也算文档型：它的唯一契约是 `intake.md`，cwd 指到 artifacts/intake
    后，即便模型想多写 PRD 也只会落在自己的产物目录，不会污染 workspace。

    办公/研究阶段（``office_*`` / ``research_*``）的产物同样是 md 文档，
    cwd 直接指向产物目录，写入即进任务档案。
    """
    return (
        stage in _DOC_STAGE_NAMES
        or stage.endswith("_tech_design")
        or stage.startswith("office_")
        or stage.startswith("research_")
    )


def _all_contract_names() -> set[str]:
    """全部阶段的契约文件名并集（diff.patch 由 git 生成，不算 agent 直写产物）。"""
    from sortie_deck.contracts import REQUIRED

    names = {name for names in REQUIRED.values() for name in names}
    names.discard("diff.patch")
    return names


def _mirror_upstream(target_dir: Path, upstream: dict[str, str]) -> dict[str, str]:
    """把上游契约产物镜像到阶段可读目录，返回 ``name → 镜像后路径``。

    为什么必须镜像：harness 只能稳定读 cwd 内的文件，cwd 之外的上游路径
    （``artifacts/<prev>/``）在 acceptEdits 下会被 Read 权限拒绝。实测
    ini_937080976c38 的开发阶段因此读不到 product_prd 的 prd.md v1.1，
    退回去读了 workspace 里 intake 遗留的 v1.0（43 条），实现口径直接错版。
    """
    local: dict[str, str] = {}
    if not upstream:
        return local
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, content in upstream.items():
        if not str(content or "").strip():
            continue
        target = target_dir / Path(name).name
        try:
            target.write_text(content, encoding="utf-8")
        except OSError:
            logger.warning("上游产物镜像失败: %s", target)
            continue
        local[name] = str(target)
    return local


def _strict_contract_stage(stage: str) -> bool:
    """该阶段缺契约文件是否直接判失败。

    历史行为是把缺失的契约文件包成「回复说明」继续往下走（mock harness 与
    老任务依赖这个兼容性）；但 intake 一旦缺 `intake.md`，包装件会掩盖「越界
    写了 PRD、正式契约反而没写」的事实，所以集结阶段改为硬失败。
    """
    from sortie_deck.settings import settings

    stages = {
        part.strip().lower()
        for part in str(getattr(settings, "strict_contract_stages", "") or "").split(",")
        if part.strip()
    }
    return str(stage or "").strip().lower() in stages


def _sweep_stale_contracts(
    workspace: Path, stage: str, artifact_dir: Path | None = None
) -> list[str]:
    """把其他阶段遗留在 workspace 根层的契约文件移进 ``.archive/``。

    编码类阶段 cwd 就是 workspace，agent 会优先读 cwd 里的同名文件；上一阶段
    留下的旧版 ``prd.md`` 会盖住 ``.upstream/`` 里的正式版（这正是 43 条被当成
    54 条依据的原因）。只处理契约文件名、只看根层、只归档不删除，保留追溯。
    文档型阶段在 ``_prepare_stage_output`` 之后调用，草稿已经先镜像进
    ``.upstream/``，所以这里搬走不会丢失审校入口。
    """
    stale = _all_contract_names() - set(_contract_names(stage))
    if not stale:
        return []
    archive = workspace / _ARCHIVE_DIRNAME / f"{stage}-{time.strftime('%Y%m%d-%H%M%S')}"
    moved: list[str] = []
    for name in sorted(stale):
        path = workspace / name
        if not path.is_file():
            continue
        try:
            archive.mkdir(parents=True, exist_ok=True)
            path.replace(archive / name)
        except OSError:
            logger.warning("跨阶段遗留清理失败: %s", path, exc_info=True)
            continue
        moved.append(name)
    if moved:
        logger.warning(
            "已清理跨阶段遗留契约文件: stage=%s files=%s -> %s",
            stage,
            ",".join(moved),
            archive,
        )
    return moved


def _sweep_stray_contracts(
    *, stage: str, artifact_dir: Path | None, workspace: Path
) -> dict[str, list[str]]:
    """阶段收尾清理：归档本阶段越界写出的契约文件，返回 ``{来源: [文件名]}``。

    为什么需要：``_sweep_stale_contracts`` 只在阶段启动时清「上一阶段」留在
    workspace 根层的文件，清不到本阶段运行中自己写进去的越界产物。实测
    ini_7463287a3371：intake 把 ``prd.md`` / ``acceptance.json`` 当「副本供下游
    读取」写进 workspace，product_prd 启动时按历史草稿把它镜像进 ``.upstream/``，
    于是正式阶段读到的是集结阶段的旧口径。

    规则：只处理契约文件名、只看根层、只归档不删除。
    - 产物目录：只搬「不属于本阶段」的契约名，本阶段契约原地保留；
    - workspace 根层：契约名一律搬走（含本阶段同名副本）——正式版已在
      artifact_dir 与产物索引里，根层副本只会被下一阶段当草稿误读。
    """
    own = set(_contract_names(stage))
    every = _all_contract_names()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved: dict[str, list[str]] = {}

    def _move(root: Path, names: set[str], archive: Path) -> list[str]:
        got: list[str] = []
        for name in sorted(names):
            path = root / name
            if not path.is_file():
                continue
            try:
                archive.mkdir(parents=True, exist_ok=True)
                path.replace(archive / name)
            except OSError:
                logger.warning("越界产物归档失败: %s", path, exc_info=True)
                continue
            got.append(name)
        return got

    if artifact_dir is not None:
        got = _move(artifact_dir, every - own, artifact_dir / _STRAY_DIRNAME / stamp)
        if got:
            moved["artifact_dir"] = got
    got = _move(workspace, every, workspace / _ARCHIVE_DIRNAME / f"{stage}-stray-{stamp}")
    if got:
        moved["workspace"] = got
    if moved:
        logger.warning(
            "已归档本阶段越界产物: stage=%s files=%s",
            stage,
            "; ".join(f"{source}={','.join(names)}" for source, names in moved.items()),
        )
    return moved


_INTAKE_SPEC = """Intake deliverable (THIS stage only — do not do later stages' work):
Write exactly ONE file, `intake.md`, in the output dir, with these sections:
1. `需求复述` — restate the brief in <=10 lines.
2. `范围内 / 范围外` — what this mission does and explicitly does not do.
3. `关键假设` — assumptions you had to make (one line each).
4. `待确认` — open questions for the PRD stage.
5. `交接要点` — what `product_prd` must produce next.

Hard limits:
- Never write `prd.md`, `acceptance.json`, design specs or test cases here; those belong to later stages and are contract-checked there.
- Do not expand open questions into a PRD — record them under `待确认` and stop.
- Write `intake.md` early (within the first few tool calls), then refine in place. Target: finish within 10 tool turns.
- `intake.md` is the only artifact checked for this stage: if it is missing or empty, the stage fails."""

_SCOPE_GUARD = """Scope guard (hard limits):
- Write ONLY this stage's contract artifacts ({own}). Never create files that belong to other stages ({others}) — a stray copy becomes stale input that the next stage reads instead of the authoritative upstream.
- Upstream truth lives in `.upstream/` (read-only mirror). Files with the same name elsewhere in the workspace are leftovers — never use them as a basis.
- Do not read or grep the orchestrator source tree, `~/.claude`, or other missions' directories. If the workspace is empty (no repos bound), one look is enough — no repeated `ls`/`find`/`tree` sweeps.
- Do not load skills unrelated to this stage.
- Finish with a `## Result` section."""

_CHECKLIST_PLAN_SECTION = """Implementation plan artifact (write `checklist.md`):
Alongside `tech_design.md`, produce `checklist.md` — an ordered, executable checklist the build
stage will tick off one item at a time:
- [ ] C1 <one concrete change> — files: `<path>` — verify: `<command>` (expect exit=0)
- [ ] C2 ...
Rules: one change per item; every item carries a runnable verification command; no vague items
("implement the feature" is invalid). Keep it to 5-15 items."""

_BUILD_CHECKLIST_RULES = """Implementation checklist (execute it, do not re-plan):
{items}

Rules:
- Work through the checklist ONE item at a time. After an item passes its verification command,
  write `checklist.md` back with that line changed from `- [ ]` to `- [x]`.
- Run each item's verification command for real, then append one record per item to
  `selftest_log.json`: [{{"item": "C1", "command": "...", "exit_code": 0, "tail": "<last lines>"}}].
- Never run `git commit` / `git push` — the orchestrator commits and opens the MR.
- If a command still fails after your fix attempt, leave the item unchecked and state it under
  `## Result`; do not silently mark it done."""

_SELFTEST_CHECKLIST_RULES = """Verification checklist (re-run, do not extend):
{items}

Rules:
- Re-run every verification command from the checklist exactly as written; do not add new cases here.
- Record each run in `selftest_log.json`: [{{"item": "C1", "command": "...", "exit_code": 0,
  "tail": "<last lines>"}}], and summarise pass/fail in `selftest.md` (contract artifact).
- A failing command must be reported as failing — never rewrite the command to make it pass."""

# 注入 runner 之后的硬约束：环境生命周期交给脚本，模型只负责「跑 → 读结果 → 修失败项」。
# 实测（2026-11-05 贪吃蛇任务）自测两阶段 241 步里 99 步是起服务/杀进程/等待/查进程。
_SELFTEST_RUNNER_RULES = """Verification runner (mandatory — do NOT manage the server yourself):
- `scripts/selftest_runner.sh` is injected for this stage. Run it ONCE:
  `bash scripts/selftest_runner.sh`.
- It starts the service, waits for the health URL, runs every case, writes
  `selftest_result.json`, then tears the service down.
- Read `selftest_result.json`; fix ONLY the failing cases it lists, then re-run the same script.
- Commit each fix separately on the task branch (`git add -A && git commit -m "<case id> <symptom>"`),
  but never `git push` — the orchestrator pushes and opens the MR.
- Never start/stop services by hand and never use pkill/kill/sleep/ps/lsof — the runner owns the
  lifecycle; extra manual attempts only burn turns and leave stale ports.
- If `selftest.config.json` is missing, fill it from the project's own e2e entry
  (package.json scripts / Makefile / scripts/*.sh). Do not invent ports or commands.
- Summarise pass/fail in `selftest.md` (contract artifact) and append run records to
  `selftest_log.json`.

Coverage checklist (reference only — the runner executes them in one pass):
{items}"""


# QA 浏览器复验：把「起服务 / 找浏览器 / 手写 CDP 脚本 / 清理」从模型手里拿走。
# 实测（2026-11-05 贪吃蛇任务）QA 52 步里 38 次是 Bash，CDP 脚本在 /tmp 里改了 3 次才跑通。
_QA_BROWSER_RUNNER = """Browser re-verification (mandatory — do NOT hand-write CDP/Playwright scripts):
- `scripts/qa_browser_runner.mjs` is injected for this stage. Run it ONCE:
  `node scripts/qa_browser_runner.mjs`.
- It starts the service from `qa.config.json`, waits for the health URL, drives headless Chrome
  through every scenario, writes `qa_result.json` + `qa_screenshots/`, then tears everything down.
- Read `qa_result.json`; fix ONLY the failing scenarios (usually the app, sometimes a selector),
  then re-run the same script.
- Commit each fix separately on the task branch (`git add -A && git commit -m "<case id> <symptom>"`),
  but never `git push` — the orchestrator pushes and opens the MR. This stage may commit; the build
  stage must not.
- Never start/stop services by hand, never use pkill/kill/sleep/ps/lsof, and never write your own
  CDP/Playwright script — add scenarios to `qa.config.json` instead.
- Scenario steps: `{"path": "/"}`, `{"wait_ms": 500}`, `{"click": "<css>"}`,
  `{"type": {"selector": "<css>", "text": "..."}}`, `{"key": "ArrowUp"}`,
  `{"expect_visible": "<css>"}`, `{"expect_hidden": "<css>"}`,
  `{"expect_text": {"selector": "<css>", "contains": "..."}}`, `{"screenshot": "<name>.png"}`.
- `qa.config.json` missing: fill `start_command` / `base_url` / `health_url` from the project's own
  entry (README / package.json scripts / Makefile). Do not invent ports.
- `qa_result.json` status `browser_unavailable`: fall back to HTTP-level checks with curl and say so
  in the report; never fabricate browser results.
"""


# 用例边界（硬约束）：QA 只能执行 test_cases.md 里定义过的用例。
# 没有这条，agent 会「发现一个疑点 → 加一条用例」，把复验拖成无限（2026-11-05 实测：
# 返工阶段手写 23 个一次性 CDP 脚本、跑了 33 分钟，其中 26 分钟在自造场景）。
_QA_CASE_BOUNDARY = """Test-case boundary (hard rule — do not expand the case list):
- `test_cases.md` is the only authority: execute the cases listed there and nothing else.
- Every browser scenario must carry `"case_id": "TC-..."` that exists in `test_cases.md`; the QA
  runner refuses scenarios whose id is not in that file.
- Anything suspicious that no case covers goes into the report as one line under
  "Observations (not gating)". Do not write a script for it, do not add a case, and do not let it
  change the verdict.
- Keep the case list bounded: group equivalent boundary values into one case (2/21 chars in one row,
  not two), and do not add a case per input permutation.
- Verification budget: at most 5 runner runs (tracked in `.qa_runner_state.json`). When the runner
  exits 5 and writes `qa_budget.json`, stop testing and report the existing `qa_result.json` as-is.
"""

# 返工边界：只回归 QA 明确判失败的用例，禁止顺手全量重测 / 探索性测试。
_REWORK_BOUNDARY = """Rework scope (hard rule — this round fixes a QA failure):
- Fix and re-verify ONLY the case ids that failed in the upstream `test_report.json`
  (e.g. `TC-X-01`); other cases are out of scope for this round.
- If `scripts/qa_browser_runner.mjs` exists, re-run just those cases with
  `node scripts/qa_browser_runner.mjs --cases TC-X-01,TC-X-02`; otherwise use the project's own
  test entry for them.
- Do NOT run exploratory tests, do NOT hand-write browser/CDP scripts, and do NOT add cases or
  edge scenarios the report did not list. Anything else you notice goes into the reply as one line.
- Verification budget: at most 5 runner runs (`.qa_runner_state.json`); exit code 5 means the budget
  is spent — stop and report the existing results.
- Do NOT run `git commit` / `git push`: this rework round is a build stage, so the changes must stay in
  the working tree for `diff.patch` and the self-review scope.
- If the fix requires a design change beyond the failing cases, escalate instead of expanding scope.
"""


def _ensure_qa_runner(workspace: Path, stage: str) -> None:
    """QA 阶段注入固定的浏览器复验 runner；项目已有同名脚本时不动它。

    注入失败只告警：prompt 会检测脚本是否存在并回退到手工复验，不影响任务。
    """
    if not _is_qa_stage(stage):
        return
    target = workspace / "scripts" / "qa_browser_runner.mjs"
    if target.exists():
        return
    from sortie_deck.templates import TEMPLATES_DIR

    template = TEMPLATES_DIR / "qa" / "browser_runner.mjs"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o755)
    except OSError as exc:
        logger.warning("QA 浏览器 runner 注入失败（回退手工复验）: %s", exc)


def _ensure_selftest_runner(workspace: Path, stage: str) -> None:
    """自测阶段注入固定 e2e runner；项目已有同名脚本时不动它。

    注入失败只告警：prompt 会检测脚本是否存在并回退到逐条验证，不影响任务。
    """
    if not stage.endswith("_selftest"):
        return
    target = workspace / "scripts" / "selftest_runner.sh"
    if target.exists():
        return
    from sortie_deck.templates import TEMPLATES_DIR

    template = TEMPLATES_DIR / "selftest" / "runner.sh"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o755)
    except OSError as exc:
        logger.warning("自测 runner 注入失败（回退逐条验证）: %s", exc)

# 没有上游清单（如 express 轨没有技术方案阶段）时的兜底：先立清单再执行，
# 否则「逐项 + 可执行验证」只对有 tech_design 的航线生效。
_BUILD_CHECKLIST_FALLBACK = """Implementation checklist (no upstream checklist — create one first):
Write `checklist.md` in the workspace with 3-8 items before coding:
- [ ] C1 <one concrete change> — files: `<path>` — verify: `<command>` (expect exit=0)
Then execute it ONE item at a time: tick `[x]` when its command passes, append
{{"item","command","exit_code","tail"}} to `selftest_log.json`, and never run `git commit` / `git push`."""


def _is_tech_design(stage: str) -> bool:
    return stage.endswith("_tech_design")


def _checklist_plan_section(stage: str) -> str:
    """技术方案阶段：要求同时产出可执行实现清单（计划锚点）。"""
    return _CHECKLIST_PLAN_SECTION if _is_tech_design(stage) else ""


def _checklist_exec_section(ctx: StageContext, cwd: Path) -> str:
    """编码/自测阶段：把实现清单变成「逐项执行 + 真跑验证命令」的指令。

    清单来源优先级：cwd 下的 checklist.md（上一轮写回的进度）→ 上游产物。
    找不到清单时不注入——空清单比没有清单更容易让模型编造条目。
    """
    is_build = ctx.stage in ENG_BUILD_STAGES
    is_selftest = ctx.stage.endswith("_selftest")
    if not (is_build or is_selftest):
        return ""
    items = ""
    local = cwd / "checklist.md"
    try:
        if local.is_file():
            items = local.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        logger.warning("实现清单读取失败: %s", local)
    if not items:
        items = str(ctx.upstream_artifacts.get("checklist.md") or "").strip()
    runner = cwd / "scripts" / "selftest_runner.sh"
    if not items:
        # 自测阶段没有清单：有 runner 仍要求一次跑完全部用例；编码阶段仍要求先立清单
        if is_build:
            return _BUILD_CHECKLIST_FALLBACK
        if is_selftest and runner.is_file():
            return _SELFTEST_RUNNER_RULES.format(
                items="(no checklist upstream — cover the full suite)"
            )
        # 自测阶段没有清单就只按 selftest.md 契约走
        return ""
    if is_selftest and runner.is_file():
        return _SELFTEST_RUNNER_RULES.format(items=items[:4000])
    template = _SELFTEST_CHECKLIST_RULES if is_selftest else _BUILD_CHECKLIST_RULES
    return template.format(items=items[:6000])


def _contract_names(stage: str) -> list[str]:
    """本阶段契约文件名（不含 diff.patch：它由 git 生成，不能由 agent 直写）。"""
    from sortie_deck.contracts import REQUIRED

    return [name for name in REQUIRED.get(stage, []) if name != "diff.patch"]


def _snapshot_contract_files(
    dirs: list[Path | None], names: list[str]
) -> dict[str, tuple[float, int]]:
    """记录阶段开始时契约文件的 (mtime, size)，用于识别「本轮是否真的写了」。

    没有快照就无法区分「agent 刚写的」与「上一轮/上游留下的」，失败阶段会拿旧文件
    通过契约校验——这是直写产物目录后最需要防的一类脏数据。
    """
    snapshot: dict[str, tuple[float, int]] = {}
    for root in dirs:
        if root is None:
            continue
        for name in names:
            path = root / name
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path)] = (stat.st_mtime, stat.st_size)
    return snapshot


def _is_fresh(path: Path, snapshot: dict[str, tuple[float, int]]) -> bool:
    """文件是否属于本阶段产出：阶段开始时不存在，或 mtime/size 已变化。"""
    try:
        stat = path.stat()
    except OSError:
        return False
    previous = snapshot.get(str(path))
    if previous is None:
        return True
    return (stat.st_mtime, stat.st_size) != previous


def _supports_extra_dirs(provider: Any) -> bool:
    """provider 是否接受 ``extra_dirs``（第三方/测试 provider 可能还是旧签名）。"""
    import inspect

    try:
        params = inspect.signature(provider.send).parameters
    except (TypeError, ValueError):
        return False
    if "extra_dirs" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _supports_extra_write_dirs(provider: Any) -> bool:
    """provider 是否能把 ``extra_dirs`` 变成真实可写目录。

    DSH 的沙箱策略只允许一个 workspace 根（extra writable roots 不在策略内），
    传了也只是误导；能力位默认为 True，旧 provider 行为不变。
    """
    return bool(getattr(provider, "supports_extra_write_dirs", True))


def _supports_on_activity(provider: Any) -> bool:
    """provider 是否接受 ``on_activity``（工具级进度回调，旧签名 provider 不支持）。"""
    import inspect

    try:
        params = inspect.signature(provider.send).parameters
    except (TypeError, ValueError):
        return False
    if "on_activity" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


# 工具级进度节流：agent 一轮里可能连读 5 个文件，全部推 SSE 只会刷屏，
# 前端只需要「现在正在做什么」这一个稳定信号。
_ACTIVITY_MIN_INTERVAL = 0.8


def _activity_callback(ctx: StageContext) -> Any:
    """把 provider 的工具调用转成 ``stage_progress`` 的 activity 事件。

    payload 只带工具名与短目标（文件名 / 命令 / 检索词），文案由前端按语言拼，
    避免中文写死在服务端。进度是观测增强项，任何失败都不能打断阶段。
    """
    last = {"at": 0.0}

    async def cb(tool: str, detail: str) -> None:
        now = time.monotonic()
        if now - last["at"] < _ACTIVITY_MIN_INTERVAL:
            return
        last["at"] = now
        await _emit_progress(
            ctx,
            {
                "phase": "activity",
                "tool": str(tool or "")[:40],
                "detail": str(detail or "")[:80],
            },
        )

    return cb


async def _send_turn(
    provider: Any,
    session: Any,
    prompt: str,
    *,
    cwd: Path,
    pack: dict[str, Any],
    extra_dirs: list[str] | None = None,
    on_activity: Any = None,
) -> Any:
    """统一的 provider 调用：按能力决定是否传额外参数（旧签名 provider 不传）。"""
    kwargs: dict[str, Any] = {"cwd": str(cwd), "context_pack": pack}
    if extra_dirs and _supports_extra_dirs(provider) and _supports_extra_write_dirs(provider):
        kwargs["extra_dirs"] = list(extra_dirs)
    if on_activity is not None and _supports_on_activity(provider):
        kwargs["on_activity"] = on_activity
    return await provider.send(session, prompt, **kwargs)


def _review_disallowed_tools() -> list[str]:
    """自审轮要禁用的工具（逗号分隔）；空串表示不禁用。"""
    from sortie_deck.settings import settings

    raw = str(getattr(settings, "review_disallowed_tools", "") or "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _merge_disallowed(pack: dict[str, Any], extra: list[str]) -> dict[str, Any]:
    """把自审禁用工具并进阶段 pack：去重、不改原 dict（主体回合的 pack 要复用）。"""
    if not extra:
        return pack
    merged = list(dict.fromkeys([*list(pack.get("disallowed_tools") or []), *extra]))
    return {**pack, "disallowed_tools": merged}


def _read_contract_text(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text if text.strip() else None


def _archive_previous_artifacts(artifact_dir: Path, names: list[str]) -> Path | None:
    """重跑前把上一轮契约文件挪进 ``.runs/<时间戳>/``，保留审计但不冒充本轮结果。"""
    import time

    existing = [name for name in names if (artifact_dir / name).is_file()]
    if not existing:
        return None
    target = artifact_dir / _RUNS_DIRNAME / time.strftime("%Y%m%dT%H%M%S")
    target.mkdir(parents=True, exist_ok=True)
    for name in existing:
        try:
            (artifact_dir / name).replace(target / name)
        except OSError:
            logger.warning("旧产物归档失败: %s", artifact_dir / name)
    return target


def _prepare_stage_output(
    *,
    stage: str,
    artifact_dir: Path,
    workspace: Path,
    upstream: dict[str, str],
) -> tuple[dict[str, str], dict[str, tuple[float, int]]]:
    """准备文档型阶段的产物目录（cwd）。

    做三件事：
    1. 上一轮契约文件移入 ``.runs/``，避免旧文件让本轮失败阶段通过校验；
    2. 上游产物复制到 ``.upstream/``（agent 只能读 cwd 内的文件，直接给档案路径
       在多数 harness 下会被权限拒绝）；
    3. 建 ``workspace`` 软链（仓库 clone 与历史遗留草稿只读可达）。

    返回 ``(上游 name → 阶段内路径, 阶段开始时契约文件快照)``。
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    names = _contract_names(stage)
    _archive_previous_artifacts(artifact_dir, names)
    snapshot = _snapshot_contract_files([artifact_dir, workspace], names)

    upstream_dir = artifact_dir / _UPSTREAM_DIRNAME
    local_upstream = _mirror_upstream(upstream_dir, upstream)

    # 上一阶段可能把同名契约文件直接留在 workspace（如 intake 的 prd.md v1.0）：
    # 这些文件不在 artifact 索引里，但正是本阶段需要审校的草稿，一并镜像进 .upstream/
    for name in names:
        if name in local_upstream:
            continue
        draft = workspace / name
        if not draft.is_file():
            continue
        try:
            upstream_dir.mkdir(parents=True, exist_ok=True)
            target = upstream_dir / name
            target.write_text(draft.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        except OSError:
            logger.warning("历史草稿镜像失败: %s", draft)
            continue
        local_upstream[name] = str(target)

    link = artifact_dir / _WORKSPACE_LINK
    if not link.is_symlink() and not link.exists():
        try:
            link.symlink_to(workspace, target_is_directory=True)
        except OSError:
            logger.warning("workspace 软链创建失败: %s", link)
    return local_upstream, snapshot


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
        # stage_start 由 graph 的阶段入口统一发（覆盖 harness / mock / coding CLI），
        # 这里只补阶段结束状态，避免同一阶段发两条开始事件。
        try:
            if not probe.ok:
                if settings.harness_fallback:
                    result = await self._fallback_run(ctx, harness, probe, host=host)
                else:
                    result = StageResult(
                        status="fail",
                        message=f"harness `{harness}` unavailable: {probe.detail}",
                        meta={"harness": harness, "probe": probe.as_dict(), "fallback": False},
                    )
            else:
                result = await self._run_harness(ctx, harness, host=host)
        except BaseException:
            await _emit_progress(ctx, {"phase": "stage_end", "harness": harness, "status": "error"})
            raise
        await _emit_progress(
            ctx,
            {
                "phase": "stage_end",
                "harness": harness,
                "status": result.status,
                "message": str(result.message or "")[:200],
            },
        )
        return result

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

        # 自审预算从「阶段开始」起算，主体回合耗时必须先扣掉
        stage_started = time.monotonic()
        # 目录优先取编排器注入值（ctx），只有旧调用方/直接单测才回退全局 settings：
        # 否则测试与多实例场景会把任务沙盒写进进程级真实数据目录
        data_dir = Path(ctx.data_dir) if ctx.data_dir else settings.data_dir
        worktrees_dir = Path(ctx.worktrees_dir) if ctx.worktrees_dir else settings.worktrees_dir
        workspace = ensure_worktree(data_dir.parent, ctx.initiative_id, worktrees_dir)
        # 自测阶段注入固定 runner：把起服务/健康检查/清理从模型手里拿走
        _ensure_selftest_runner(workspace, ctx.stage)
        # QA 阶段注入浏览器复验 runner：场景写在 qa.config.json，模型只跑脚本 + 修失败项
        _ensure_qa_runner(workspace, ctx.stage)
        repo_section, repo_log = await clone_repos(
            ctx.repos,
            workspace,
            ctx.initiative_id,
            actor_user_id=ctx.actor_user_id,
            # 编码阶段禁止提交（否则 diff.patch / 自审范围采不到代码），自测与 QA 允许
            allow_commit=ctx.stage not in ENG_BUILD_STAGES,
        )

        # 产物目录：文档型阶段 cwd 直接指向 artifacts/<stage>（运行期可实时看到真产物），
        # 编码型阶段 cwd 仍是 workspace，产物目录只作为额外可写目录 / 收尾归档目标。
        artifact_dir = Path(ctx.artifact_dir) if ctx.artifact_dir else None
        direct_write = bool(getattr(settings, "harness_artifact_direct_write", True))
        doc_stage = bool(direct_write and artifact_dir is not None and _is_doc_stage(ctx.stage))
        snapshot: dict[str, tuple[float, int]] = {}
        extra_dirs: list[str] = []
        stale_moved: list[str] = []
        stray_moved: dict[str, list[str]] = {}
        if doc_stage and artifact_dir is not None:
            local_upstream, snapshot = _prepare_stage_output(
                stage=ctx.stage,
                artifact_dir=artifact_dir,
                workspace=workspace,
                upstream=ctx.upstream_artifacts,
            )
            if local_upstream:
                # prompt 里的「按需读取」路径必须落在 cwd 内，否则多数 harness 读不了
                ctx = ctx.model_copy(update={"upstream_paths": local_upstream})
            # 历史草稿已镜像进 .upstream，再清掉 workspace 里的其余阶段遗留副本
            stale_moved = _sweep_stale_contracts(workspace, ctx.stage, artifact_dir)
            cwd = artifact_dir
            # workspace 作为额外目录：claude/codex 可读写，其余 harness 忽略
            extra_dirs = [str(workspace)]
            if repo_section:
                # clone_repos 生成的是 workspace 下的绝对路径，改写为阶段内的只读软链
                repo_section = repo_section.replace(
                    str(workspace), str(artifact_dir / _WORKSPACE_LINK)
                )
        else:
            cwd = workspace
            # 编码类阶段没有 .upstream 机制：上游在 artifacts/<prev>/，harness 读
            # cwd 之外的路径会被权限拒绝，agent 只能退回去读 workspace 里的旧草稿。
            # 先清遗留再把正式上游镜像进 workspace/.upstream，prompt 指向镜像路径。
            stale_moved = _sweep_stale_contracts(workspace, ctx.stage, artifact_dir)
            local_upstream = _mirror_upstream(
                workspace / _UPSTREAM_DIRNAME, ctx.upstream_artifacts
            )
            if local_upstream:
                ctx = ctx.model_copy(update={"upstream_paths": local_upstream})
            snapshot = _snapshot_contract_files(
                [workspace, artifact_dir], _contract_names(ctx.stage)
            )
            if direct_write and artifact_dir is not None:
                artifact_dir.mkdir(parents=True, exist_ok=True)
                extra_dirs = [str(artifact_dir)]

        pack = await self._build_context_pack(ctx)
        prompt = self._build_prompt(
            ctx,
            cwd,
            pack,
            repo_section,
            with_subroom=host is not None,
            artifact_dir=artifact_dir,
            doc_stage=doc_stage,
        )
        provider = get_harness_registry().get(harness)
        session = await provider.start_session(
            initiative_id=ctx.initiative_id,
            stage=ctx.stage,
            meta={"role": str(getattr(ctx.role, "value", ctx.role))},
        )
        reply = await _send_turn(
            provider,
            session,
            prompt,
            cwd=cwd,
            pack=pack,
            extra_dirs=extra_dirs,
            on_activity=_activity_callback(ctx),
        )
        # 模型声明小群讨论意图时，立即拉群并把结论回注成第二轮收尾
        reply = await self._maybe_consult_subroom(
            ctx, provider, session, reply, pack, host, cwd, extra_dirs
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
                            follow = await _send_turn(
                                provider,
                                session,
                                (
                                    "任务管理器已对你升级的问题给出裁决：\n\n"
                                    f"{resolution}\n\n"
                                    "请据此完成本阶段的最终产出（结尾保留 `## Result`）；"
                                    "不要再声明 escalate。"
                                ),
                                cwd=cwd,
                                pack=pack,
                                extra_dirs=extra_dirs,
                                on_activity=_activity_callback(ctx),
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

        # 多镜头自审：编码阶段收尾时在同一会话续跑 N 轮（自测阶段不再自审）。
        # diff 同时供自审范围与 diff.patch 产物复用，只跑一次 git。
        from sortie_deck.contracts import REQUIRED as _REQUIRED
        from sortie_deck.plugins.coding import _git_diff
        from sortie_deck.review_lens import load_review_config, stage_in_review_scope

        # 超时按 harness 自适应：各 CLI 的 agent loop 单回合耗时差一个量级
        review_cfg = load_review_config(harness)
        wants_review = bool(review_cfg.enabled) and stage_in_review_scope(ctx.stage, review_cfg)
        needs_diff = "diff.patch" in _REQUIRED.get(ctx.stage, [])
        repo_root = (Path(ctx.data_dir) if ctx.data_dir else settings.data_dir).parent
        stage_diff = ""
        if wants_review or needs_diff:
            stage_diff = await _git_diff(workspace, repo_root, ctx.repos)
        review_log = None
        if wants_review:
            review_log = await self._run_review(
                ctx,
                reply,
                provider,
                session,
                pack,
                cwd=cwd,
                extra_dirs=extra_dirs,
                harness=harness,
                diff=stage_diff,
                data_dir=data_dir,
                workspace=workspace,
                cfg=review_cfg,
                started=stage_started,
            )
            # 自审轮次可能就地修过文件：diff.patch 必须反映最终状态，
            # 不能沿用自审开始前的快照（否则 QA 看到的是旧改动）
            if review_log.executed and needs_diff:
                stage_diff = await _git_diff(workspace, repo_root, ctx.repos)

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
        artifacts, diff, artifact_meta = await self._artifacts(
            ctx,
            workspace,
            reply,
            log_lines,
            harness=harness,
            artifact_dir=artifact_dir,
            snapshot=snapshot,
            diff=stage_diff or None,
        )
        # 契约缺件不能靠回复包装蒙混过关：包装件会让阶段"看起来有产物"，下游读到的
        # 却是一段说明（intake 越界写 PRD、正式契约反而不存在，就是这么被放过的）。
        # 只对 strict_contract_stages 列出的阶段硬失败，其余保持历史降级行为。
        missing_contracts = list(artifact_meta.get("artifact_degraded") or [])
        strict_missing = bool(missing_contracts) and _strict_contract_stage(ctx.stage)
        if strict_missing:
            ok = False
        # 收尾清理本阶段越界产物：必须排在 _artifacts 之后（workspace 里的真产物
        # 已归档进 artifact_dir）。needs_hitl 时跳过：阶段会重试，不是终态。
        if doc_stage and not needs_hitl:
            stray_moved = _sweep_stray_contracts(
                stage=ctx.stage, artifact_dir=artifact_dir, workspace=workspace
            )
        meta = {
            "harness": harness,
            "session_id": session.native_id or session.id,
            "provider_reply_status": reply.status,
            "workspace": str(workspace),
            "artifact_dir": str(artifact_dir) if artifact_dir else "",
            "artifact_direct_write": doc_stage,
            "files_written": _count_impl_files(workspace, has_repos=bool(ctx.repos)),
            "context_tokens": int(pack.get("token_estimate") or 0),
            # 跨阶段遗留清理记录：工作台/排障时用于确认「本阶段读到的是正式版」
            "stale_moved": stale_moved,
            # 本阶段越界产物归档记录：非空说明模型写了别的阶段的契约文件
            "stray_moved": stray_moved,
            **artifact_meta,
        }
        # 自审报告：真正跑过轮次才进产物（skipped/prompted 只留日志，避免假报告），
        # 精简结论进 meta 供工作台/门禁展示（完整记录在 logs/<stage>/review.json）
        if review_log is not None:
            if review_log.executed:
                from sortie_deck.review_lens import render_review_markdown

                artifacts["review.md"] = render_review_markdown(review_log)
            meta["review"] = review_log.summary()
        # provider 侧的会话观测（轮次 / 压缩边界 / 成本）：工作台据此判断
        # 「这一阶段是否被压缩过、跑了多少轮」，不必翻 SDK 日志
        reply_meta = getattr(reply, "meta", None)
        if isinstance(reply_meta, dict) and reply_meta.get("context"):
            meta["context"] = reply_meta["context"]
        if pack.get("mcp_notes"):
            meta["mcp"] = {
                "servers": sorted((pack.get("mcp_servers") or {}).keys()),
                "notes": list(pack.get("mcp_notes") or []),
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
        # 自审仍有 blocking：按策略转人工门禁或直接判失败。
        # 默认 hitl——能改的 agent 在轮内已经改过了，剩下的就是人该拍板的。
        if review_log is not None and review_log.verdict == "blocking":
            policy = review_cfg.blocking_policy
            if policy in {"hitl", "fail"}:
                message = (
                    f"多镜头自审发现 {review_log.blocking_count} 个 blocking 问题"
                    f"（见 review.md / {review_log.log_path or 'logs'}）。"
                )
                return StageResult(
                    status="needs_hitl" if policy == "hitl" else "fail",
                    message=message + ("请在门禁裁决后重试。" if policy == "hitl" else ""),
                    artifacts=artifacts,
                    proposed_next=ctx.stage,
                    meta={**meta, "review_blocking": True},
                )
        # 模型没声明交接时保留编码角色的既有默认落点（qa_verify）
        fallback_next = (
            "qa_verify"
            if ok and str(getattr(ctx.role, "value", ctx.role)) in CODING_ROLE_VALUES
            else None
        )
        return StageResult(
            status="ok" if ok else "fail",
            message=(
                f"harness `{harness}` stage finished"
                if ok
                else (
                    f"harness `{harness}` 未写出契约产物：{', '.join(missing_contracts)}"
                    "（见 reply.md）"
                    if strict_missing
                    else f"harness `{harness}` 未产出有效回复（见 reply.md / implementation.md）"
                )
            ),
            artifacts=artifacts,
            proposed_next=proposed_next or fallback_next,
            qa_passed=qa_passed,
            meta={**meta, **({"control_intent": control_meta} if control_meta else {})},
        )

    async def _run_review(
        self,
        ctx: StageContext,
        reply: Any,
        provider: Any,
        session: Any,
        pack: dict[str, Any],
        *,
        cwd: Path,
        extra_dirs: list[str] | None,
        harness: str,
        diff: str,
        data_dir: Path,
        workspace: Path,
        cfg: Any,
        started: float = 0.0,
    ) -> Any:
        """编码阶段的收尾自审：同一会话跑 N 轮，返回 ReviewLog。

        跳过条件是「审了也没意义」的场景（mock harness / 首轮就失败 / 阶段剩余
        预算不足），跳过也会留一条 ``skipped`` 记录，避免日志里出现
        「这一阶段没有 review」却不知道是没开还是被跳过。
        """
        from sortie_deck.review_lens import (
            build_scope,
            build_skipped_log,
            clip_diff,
            run_review_lenses,
        )
        from sortie_deck.settings import settings

        checklist = ""
        local_checklist = workspace / "checklist.md"
        if local_checklist.is_file():
            try:
                checklist = local_checklist.read_text(encoding="utf-8", errors="replace")
            except OSError:
                logger.warning("实现清单读取失败: %s", local_checklist)
        elif ctx.upstream_artifacts.get("checklist.md"):
            checklist = ctx.upstream_artifacts["checklist.md"]
        # workspace 用于「无 git 差异」时的改动面兜底（没绑仓库的任务很常见）。
        # 统计用完整 diff，prompt 只带裁剪后的正文：否则 24 文件从 0 开发的 diff
        # 会挤掉审查指令，或被完全省略导致模型退回逐个 Read。
        scope = build_scope(
            ctx,
            diff,
            checklist,
            workspace=workspace,
            diff_excerpt=clip_diff(
                diff,
                max_chars=int(getattr(settings, "review_diff_max_chars", 60_000)),
                max_file_lines=int(getattr(settings, "review_diff_max_file_lines", 2_000)),
                max_file_chars=int(getattr(settings, "shadow_git_max_file_bytes", 1_048_576)),
            ),
        )
        # 编排层规则打分：不额外调模型，判定依据写进 review.json，事后可复盘调阈值
        from sortie_deck.complexity import estimate_from_scope

        complexity = estimate_from_scope(scope, brief=ctx.brief, checklist=checklist)
        complexity_meta = complexity.as_dict()

        if harness == "mock":
            return build_skipped_log(
                ctx,
                scope,
                reason="mock harness 不产生真实改动，跳过自审",
                harness=harness,
                data_dir=data_dir,
                complexity=complexity_meta,
            )
        if str(getattr(reply, "status", "")) != "ok" or not str(
            getattr(reply, "text", "") or ""
        ).strip():
            return build_skipped_log(
                ctx,
                scope,
                reason="首轮未成功产出，跳过自审",
                harness=harness,
                data_dir=data_dir,
                complexity=complexity_meta,
            )

        # 自适应档位：简单任务直接走老逻辑（0 轮），复杂任务按配置加轮次
        if bool(getattr(cfg, "adaptive", True)):
            round_budget = cfg.rounds_for(complexity.level)
            if round_budget <= 0:
                reason = (
                    f"复杂度判定 {complexity.level}（{complexity.summary()}），"
                    "按老逻辑跳过自审"
                )
                logger.info(
                    "自审按复杂度跳过: initiative=%s stage=%s level=%s signals=%s",
                    ctx.initiative_id,
                    ctx.stage,
                    complexity.level,
                    complexity.signals,
                )
                await _emit_progress(
                    ctx,
                    {
                        "phase": "review_skip",
                        "total": 0,
                        "status": "skipped",
                        "title": reason,
                    },
                )
                return build_skipped_log(
                    ctx,
                    scope,
                    reason=reason,
                    harness=harness,
                    data_dir=data_dir,
                    complexity=complexity_meta,
                )
            if round_budget != cfg.rounds:
                cfg = replace(cfg, rounds=round_budget)
                logger.info(
                    "自审轮次按复杂度调整: initiative=%s stage=%s level=%s rounds=%d",
                    ctx.initiative_id,
                    ctx.stage,
                    complexity.level,
                    round_budget,
                )

        # 预算压缩：主体回合可能已经吃掉大半阶段预算，自审必须给收尾留余量。
        # 压不下来就整段跳过——不能让增强项把阶段顶成超时失败。
        original_timeout = float(getattr(cfg, "timeout", 0.0) or 0.0)
        budgeted = _review_timeout_within_budget(ctx, started, original_timeout, cfg.rounds)
        if budgeted is None:
            logger.warning(
                "自审跳过（阶段剩余预算不足）: initiative=%s stage=%s elapsed=%.0fs budget=%.0fs",
                ctx.initiative_id,
                ctx.stage,
                0.0 if not started else time.monotonic() - started,
                float(getattr(ctx, "stage_timeout_seconds", 0.0) or 0.0),
            )
            await _emit_progress(
                ctx,
                {
                    "phase": "review_skip",
                    "total": cfg.rounds,
                    "status": "skipped",
                    "title": "阶段剩余预算不足，跳过自审",
                },
            )
            return build_skipped_log(
                ctx,
                scope,
                reason="阶段剩余预算不足，跳过自审",
                harness=harness,
                data_dir=data_dir,
                complexity=complexity_meta,
            )
        if budgeted != original_timeout:
            cfg = replace(cfg, timeout=budgeted)
            logger.info(
                "自审单轮超时按阶段预算压缩: initiative=%s stage=%s %.0fs -> %.0fs",
                ctx.initiative_id,
                ctx.stage,
                original_timeout,
                budgeted,
            )
        # 首轮实现 → 自审的切换点：前端据此把转圈从「实现中」切到「自审第 1/N 轮」
        await _emit_progress(
            ctx,
            {"phase": "review_begin", "total": cfg.rounds, "timeout": cfg.timeout},
        )

        # 自审轮禁掉逐文件 Read/Bash：diff 已在 prompt 里，读文件只会把单轮拖到 100s+
        review_banned = _review_disallowed_tools()
        review_pack = _merge_disallowed(pack, review_banned)
        if review_banned:
            logger.info(
                "自审禁用工具: initiative=%s stage=%s tools=%s",
                ctx.initiative_id,
                ctx.stage,
                ",".join(review_banned),
            )

        async def send(prompt: str) -> Any:
            # 续接同一 provider 会话：第 2..N 轮必须看到首轮实现上下文
            return await _send_turn(
                provider,
                session,
                prompt,
                cwd=cwd,
                pack=review_pack,
                extra_dirs=extra_dirs,
                on_activity=_activity_callback(ctx),
            )

        return await run_review_lenses(
            ctx,
            scope,
            send,
            rounds=cfg.rounds,
            timeout=cfg.timeout,
            harness=harness,
            session_id=session.native_id or session.id,
            data_dir=data_dir,
            on_progress=lambda payload: _emit_progress(ctx, payload),
            circuit_breaker=bool(getattr(cfg, "circuit_breaker", True)),
            complexity=complexity_meta,
            disallowed_tools=review_banned,
        )

    async def _maybe_consult_subroom(
        self,
        ctx: StageContext,
        provider: Any,
        session: Any,
        reply: Any,
        pack: dict[str, Any],
        host: Any,
        cwd: Path,
        extra_dirs: list[str] | None = None,
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
            follow = await _send_turn(
                provider,
                session,
                (
                    "你申请的小组讨论已经完成，结论如下：\n\n"
                    f"{conclusion.strip()}\n\n"
                    "请据此完成本阶段的最终产出（结尾保留 `## Result` 小结），不要再申请新的讨论。"
                ),
                cwd=cwd,
                pack=pack,
                extra_dirs=extra_dirs,
                on_activity=_activity_callback(ctx),
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
        briefs = await self._artifact_briefs(ctx, mm)
        mcp_servers, mcp_notes = self._mcp_servers(ctx)
        out: dict[str, Any] = {
            "memory_md": pack.memory_md if pack else "",
            "context_files": dict(pack.context_files) if pack else {},
            "artifact_index": list(pack.artifact_index) if pack else [],
            "token_estimate": pack.token_estimate if pack else 0,
            "rag_brief": await self._rag_brief(ctx),
            # 上游产物「摘要 + 路径」清单（空 dict 表示走旧的全量注入分支）
            "briefs": briefs,
            # 本阶段实际挂载的 MCP（strict 关闭时为空，由 SDK 按 setting_sources 全量加载）
            "mcp_servers": mcp_servers,
            "mcp_notes": mcp_notes,
            # 本阶段禁用工具：provider 据此设置底层 agent 的工具白名单（claude_code
            # 映射到 SDK disallowed_tools）。放在 pack 里而不是改 send 签名，
            # 旧 provider 忽略该键即可，不会因签名不匹配报错。
            "disallowed_tools": list(ctx.disallowed_tools or []),
        }
        return out

    async def _artifact_briefs(
        self, ctx: StageContext, mm: Any
    ) -> dict[str, Any]:
        """构建上游产物摘要；开关关闭或构建失败时返回空 dict（回落全文注入）。

        摘要本身是增强项：LLM 失败、缓存不可写都已在 artifact_summary 内部降级，
        这里再兜一层，确保任何异常都不会让阶段失败。
        """
        from sortie_deck.artifact_summary import build_artifact_briefs
        from sortie_deck.settings import settings

        if not settings.prompt_artifact_briefs or not ctx.upstream_artifacts:
            return {}
        try:
            return await build_artifact_briefs(
                ctx.upstream_artifacts,
                ctx.upstream_paths,
                mission_id=ctx.initiative_id,
                max_tokens=settings.prompt_artifact_summary_max_tokens,
                inline_chars=settings.prompt_inline_artifact_chars,
                use_llm=settings.prompt_artifact_summary_llm,
                timeout=settings.prompt_artifact_summary_timeout,
                memory_dir=mm.memory_dir(ctx.initiative_id),
            )
        except Exception:  # noqa: BLE001 — 摘要失败回落全文，绝不能打挂阶段
            logger.warning(
                "产物摘要构建失败，本阶段回落全文注入: initiative=%s stage=%s",
                ctx.initiative_id,
                ctx.stage,
                exc_info=True,
            )
            return {}

    def _mcp_servers(self, ctx: StageContext) -> tuple[dict[str, Any], list[str]]:
        """解析本阶段应挂载的 MCP。

        ``harness_mcp_strict`` 关闭时返回空 dict —— provider 不传 ``mcp_servers``，
        SDK 按 ``setting_sources`` 全量加载本机 mcpServers（旧行为，用于回退比对）。
        """
        from sortie_deck.settings import settings

        if not settings.harness_mcp_strict:
            return {}, ["strict 关闭：沿用本机全量 MCP"]

        from sortie_deck.harness_providers.mcp_binding import mcp_servers_from_toolkit

        try:
            from sortie_deck.orchestrator import _toolkit_store
        except Exception:  # noqa: BLE001 — 单测/早期启动时工具箱可能未就绪
            return {}, ["工具箱未就绪：本阶段不挂载 MCP"]

        return mcp_servers_from_toolkit(_toolkit_store, list(ctx.toolkit_ids))

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
        artifact_dir: Path | None = None,
        doc_stage: bool = False,
    ) -> str:
        prd = ctx.upstream_artifacts.get("prd.md", ctx.brief)
        briefs = pack.get("briefs") or {}
        contract_hint = "、".join(f"`{n}`" for n in _contract_names(ctx.stage))
        if doc_stage:
            # 文档型阶段的 cwd 就是产物目录：契约文件必须写在这里，才能在运行中
            # 实时出现在任务档案里；workspace 只作为只读输入入口。
            location_line = (
                f"Output dir (cwd): `{workspace}` — write the contract artifacts HERE "
                f"({contract_hint or 'no contract file'}).\n"
                f"Read-only inputs: `{workspace / _UPSTREAM_DIRNAME}` (upstream artifacts "
                "and previous drafts) and "
                f"`{workspace / _WORKSPACE_LINK}` (repo checkouts). "
                "Do not write contract files into those directories."
            )
        else:
            location_line = f"Workspace (cwd): `{workspace}` — you may create/edit files here."
            if artifact_dir is not None:
                # 无契约阶段不再回退成 "the contract files"：含糊指令会让模型自己找
                # 规格（实测 intake 因此反手写了整份 PRD + 验收用例，越界且留脏文件）
                write_hint = (
                    f"write {contract_hint} there too; it is the mission archive."
                    if contract_hint
                    else "this stage has no contract artifact — do not write stage documents there."
                )
                location_line += f"\nContract artifacts dir: `{artifact_dir}` — {write_hint}"
        sections: list[str] = [
            ctx.persona or f"You are the {ctx.role} agent in Sortie Deck.",
            (
                "You are running inside a Sortie Deck mission. Complete THIS stage and "
                "produce the stage artifact. Do not ask the user questions — decide and act."
            ),
            f"Stage: `{ctx.stage}` — Role: `{getattr(ctx.role, 'value', ctx.role)}`",
            location_line,
            f"Human instruction: {ctx.human_instruction or 'none'}",
            f"Mission brief:\n{ctx.brief}",
        ]
        if ctx.disallowed_tools:
            # 显式告知工具禁用：否则 agent 会先尝试 Bash 撞一次拒绝再放弃，白烧回合。
            sections.append(
                "Tool policy for this stage: "
                f"{', '.join(ctx.disallowed_tools)} is NOT available. "
                "Do not verify code by executing it here — this stage only produces the "
                "design document; implementation and verification belong to later stages."
            )
        if ctx.stage == "intake":
            # 集结只做需求收敛：这段写死唯一产出与轮次目标，防止它去产 PRD
            sections.append(_INTAKE_SPEC)
        if briefs:
            from sortie_deck.artifact_summary import render_artifact_briefs

            sections.append(
                "Upstream artifacts（权威副本已镜像到阶段目录内，只给摘要与路径；"
                "需要细节时用 Read 打开对应路径，不要凭文件名猜内容；"
                "workspace 其他位置的同名文件是遗留物，禁止作为依据）:\n"
                + render_artifact_briefs(briefs)
                + "\n\n读取建议：只读与本阶段产出直接相关的 1-2 份，其余按需再取，"
                "不要为了完整性通读全部产物。"
            )
        else:
            # 回退分支：摘要开关关闭 / 无上游产物 / 摘要构建失败时保持旧行为
            sections.append(f"Upstream PRD:\n{prd}")
        memory_md = str(pack.get("memory_md") or "")
        if memory_md.strip():
            sections.append(f"Rolling mission memory (from bound files):\n{memory_md.strip()}")
        context_files = pack.get("context_files") or {}
        for name, content in context_files.items():
            sections.append(f"Bound file `{name}`:\n{content.strip()[:6000]}")
        # 任务输入快照：只列路径与元数据，正文让 agent 按需 Read（动态加载）
        inputs = list(getattr(ctx, "inputs", []) or [])
        if inputs:
            from sortie_deck.mission_input import render_input_index

            index_block = render_input_index(inputs)
            if index_block:
                sections.append(index_block)
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
        # 计划 → 清单 → 逐项验证：技术方案阶段产清单，编码/自测阶段执行清单
        plan_section = _checklist_plan_section(ctx.stage)
        if plan_section:
            sections.append(plan_section)
        exec_section = _checklist_exec_section(ctx, workspace)
        if exec_section:
            sections.append(exec_section)
        # 控制平面协议始终注入：即使没有可交接目标，升级真人仍是 agent 的兜底能力
        sections.append(_control_protocol(ctx))
        # QA 阶段额外要求结论块：否则图无法区分"测试通过"与"需要打回"
        if _is_qa_stage(ctx.stage):
            sections.append(_QA_PROTOCOL)
            # 用例边界与 runner 是否存在无关：没有 runner 也不许自造用例
            sections.append(_QA_CASE_BOUNDARY)
            # runner 注入成功才给硬约束，否则模型以为有脚本可跑
            if (workspace / "scripts" / "qa_browser_runner.mjs").is_file():
                sections.append(_QA_BROWSER_RUNNER)
        # 返工阶段（上游已有 QA 报告）：只许回归失败用例，禁止顺手全量重测
        if ctx.stage in ENG_BUILD_STAGES and "test_report.json" in (ctx.upstream_artifacts or {}):
            sections.append(_REWORK_BOUNDARY)
        if repo_section:
            sections.append(repo_section.strip())
        artifact_index = list(pack.get("artifact_index") or [])
        # briefs 已覆盖上游产物时不再列全量索引：索引对「找产物」有用，
        # 但在已有摘要+路径的阶段属于重复信息，白白占预算
        if artifact_index and not briefs:
            sections.append(
                "Mission artifact index:\n" + "\n".join(f"- `{p}`" for p in artifact_index[:20])
            )
        # 边界硬限制放最后：模型对结尾指令最敏感，越界产他阶段文件是最贵的错误
        own_contract = _contract_names(ctx.stage)
        sections.append(
            _SCOPE_GUARD.format(
                own="、".join(f"`{n}`" for n in own_contract) or "(none)",
                others="、".join(
                    f"`{n}`" for n in sorted(_all_contract_names() - set(own_contract))
                )
                or "(none)",
            )
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
        artifact_dir: Path | None = None,
        snapshot: dict[str, tuple[float, int]] | None = None,
        diff: str | None = None,
    ) -> tuple[dict[str, str], str, dict[str, Any]]:
        """组装阶段产物：优先取磁盘真文件，读不到才回退回复包装。

        解析顺序（三级）：
        1. ``artifacts/<stage>/`` 里本次新写的契约文件（文档型阶段的正常路径）；
        2. ``workspace/`` 里的同名文件（编码型阶段 / 不听话的 agent），复制进产物目录；
        3. 回复文本包装（历史兜底），并标记 ``artifact_degraded`` 便于发现。

        ``snapshot`` 是阶段开始时的 (mtime, size) 快照：旧文件（快照里存在且未变化）
        不算本阶段产物，避免失败阶段拿上一轮结果通过校验。
        ``diff`` 由调用方预先算好（自审范围复用同一次 git 结果），缺省时按契约现算。
        """
        from sortie_deck.contracts import REQUIRED, optional_artifacts
        from sortie_deck.plugins.coding import _count_impl_files, _git_diff
        from sortie_deck.settings import settings

        needs_diff = "diff.patch" in REQUIRED.get(ctx.stage, [])
        if diff is None:
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
        snap = snapshot or {}
        sources: dict[str, str] = {}
        degraded: list[str] = []
        for name in REQUIRED.get(ctx.stage, []):
            if name == "diff.patch":
                artifacts[name] = diff
                continue

            text: str | None = None
            for source, root in (("artifact_dir", artifact_dir), ("workspace", workspace)):
                if root is None:
                    continue
                path = root / name
                if not _is_fresh(path, snap):
                    continue
                text = _read_contract_text(path)
                if text is None:
                    continue
                sources[name] = source
                break

            if text is not None:
                artifacts[name] = text
                # 来自 workspace 的真文件顺手归档进产物目录，任务档案不再是包装
                if sources.get(name) == "workspace" and artifact_dir is not None:
                    try:
                        artifact_dir.mkdir(parents=True, exist_ok=True)
                        (artifact_dir / name).write_text(text, encoding="utf-8")
                    except OSError:
                        logger.warning("产物归档失败: %s", artifact_dir / name)
                continue

            degraded.append(name)
            if name.endswith(".json"):
                artifacts[name] = json.dumps(
                    {
                        "stage": ctx.stage,
                        "status": "ok" if reply.status == "ok" else "failed",
                        "summary": (reply.text or "")[:1500],
                        "harness": harness,
                        "workspace": str(workspace),
                        "files_written": _count_impl_files(workspace, has_repos=bool(ctx.repos)),
                        "degraded": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            else:
                artifacts[name] = (
                    f"# {name} — stage `{ctx.stage}` (harness `{harness}`)\n\n"
                    f"{reply.text}\n\n## Log\n{log[:2000]}\n"
                )

        artifact_meta: dict[str, Any] = {}
        if sources:
            artifact_meta["artifact_sources"] = sources
        if degraded:
            artifact_meta["artifact_degraded"] = degraded
            logger.warning(
                "阶段产物回退为回复包装: initiative=%s stage=%s files=%s",
                ctx.initiative_id,
                ctx.stage,
                ",".join(degraded),
            )
        # 可选产物（实现清单 / 验证命令日志）：存在就归档，缺失不算失败，
        # 因此不参与 validate_stage_artifacts，也不进 degraded 列表。
        optional_sources: dict[str, str] = {}
        for name in optional_artifacts(ctx.stage):
            if name in artifacts:
                continue
            for source, root in (("artifact_dir", artifact_dir), ("workspace", workspace)):
                if root is None:
                    continue
                path = root / name
                if not _is_fresh(path, snap):
                    continue
                text = _read_contract_text(path)
                if text is None:
                    continue
                artifacts[name] = text
                optional_sources[name] = source
                break
        if optional_sources:
            artifact_meta["optional_artifacts"] = optional_sources
        return artifacts, diff, artifact_meta
