"""多镜头自审（multi-lens self-review）——把 autopilot-harness 的「审查视角轮换」
内化到编排层，不依赖任何宿主 hook。

为什么放在编排层：宿主 hook 循环在 claude_code / codex / cursor / opencode 上语义
不一致（有的宿主还有 Stop 次数上限），而编排层可以直接复用 provider 会话续跑，
对所有 harness 行为一致。设计取舍见
``docs/plans/2026-11-02-多镜头自审方案与改动说明.md``。

一轮 lens = 同一 provider 会话上的下一次 ``send()``。本模块负责三件事：

1. 组装「审查范围」（本阶段改动文件 / 契约产物 / 上游产物），回答**审查了什么**；
2. 解析回复里的 ```review 围栏块，拿到 status / findings，回答**审查结果**；
3. 逐轮追加 ``logs/<stage>/review.jsonl``、收尾写 ``review.json``，
   并把 ``review.md`` 交给执行器放进阶段产物，回答**审查状态与审计**。

模型没有按协议回块时记为 ``unparsed``（降级但不算失败）：自审是增强项，
不能因为模型不听话而打挂一个已经产出代码的阶段。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from sortie_deck.models import ENG_BUILD_STAGES, StageContext, role_key, utc_now

logger = logging.getLogger(__name__)


class LensStatus(str, Enum):
    """单轮审查状态。

    ``passed`` / ``findings`` / ``blocking`` 是模型自报的结论；
    ``unparsed`` 表示回复缺少协议块（降级）；``failed`` 是超时或异常；
    ``skipped`` 是编排层主动跳过（mock harness 等）；
    ``prompted`` 仅用于 CLI 单发模式——已写进提示词但编排层无法验证。
    """

    PASSED = "passed"
    FINDINGS = "findings"
    BLOCKING = "blocking"
    UNPARSED = "unparsed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PROMPTED = "prompted"


class Severity(str, Enum):
    BLOCKING = "blocking"
    MAJOR = "major"
    MINOR = "minor"


@dataclass(frozen=True)
class Lens:
    """一个审查视角：每轮只换视角，不重复 checklist。"""

    name: str
    title: str
    instruction: str
    # False = 该轮只记录问题，不允许改代码（避免边审边改污染审查对象）
    fix_allowed: bool = True


LENSES: tuple[Lens, ...] = (
    Lens(
        name="correctness",
        title="正确性与不变量",
        instruction=(
            "对照验收标准（acceptance.json / PRD）逐条确认实现是否满足；检查是否破坏既有"
            "接口、数据契约、错误码或状态机不变量。只报能落到具体文件的结论。"
        ),
    ),
    Lens(
        name="boundaries",
        title="空值与边界",
        instruction=(
            "检查空输入、超长/超大、缺字段、重复调用、失败重试、异常分支是否被处理；"
            "看真实代码路径，不要假设上游一定给合法数据。"
        ),
    ),
    Lens(
        name="concurrency",
        title="并发与部分失败",
        instruction=(
            "检查并发读写、竞态、幂等、事务边界与部分失败后的可恢复性；"
            "若本阶段没有并发面，直接 status=passed 并说明理由。"
        ),
    ),
    Lens(
        name="security",
        title="安全与信任边界",
        instruction=(
            "检查注入、越权、密钥/token/手机号等敏感信息是否落日志或返回前端、"
            "外部输入是否被当成可信数据执行。"
        ),
    ),
    Lens(
        name="test_gaps",
        title="测试缺口",
        instruction=(
            "列出本阶段改动缺少哪些用例（正常/边界/失败路径），按严重级标注；"
            "本轮只记录缺口，不要新增或修改测试文件。"
        ),
        fix_allowed=False,
    ),
)

# 轻量模式（3 轮）取的视角：测试缺口固定最后，它只记录不修改
_LIGHT_LENS_NAMES = ("correctness", "boundaries", "test_gaps")

_REVIEW_BLOCK_RE = re.compile(r"```review\s*(.*?)```", re.DOTALL)

_MAX_FINDINGS = 20
_MAX_REVIEWED = 20


@dataclass(frozen=True)
class ReviewConfig:
    enabled: bool
    rounds: int
    stages: frozenset[str]
    timeout: float
    blocking_policy: str


def load_review_config() -> ReviewConfig:
    """读取自审配置；settings 缺字段时按「关闭」处理（单测/旧配置安全）。"""
    from sortie_deck.settings import settings

    enabled = bool(getattr(settings, "review_lenses_enabled", False))
    try:
        rounds = int(getattr(settings, "review_lens_rounds", 3) or 3)
    except (TypeError, ValueError):
        rounds = 3
    raw_stages = str(getattr(settings, "review_lens_stages", "build,selftest") or "")
    stages = frozenset(part.strip().lower() for part in raw_stages.split(",") if part.strip())
    try:
        timeout = float(getattr(settings, "review_lens_timeout_seconds", 300.0) or 300.0)
    except (TypeError, ValueError):
        timeout = 300.0
    policy = str(getattr(settings, "review_blocking_policy", "hitl") or "hitl").strip().lower()
    if policy not in {"hitl", "fail", "ignore"}:
        policy = "hitl"
    return ReviewConfig(
        enabled=enabled,
        rounds=max(0, rounds),
        stages=stages,
        timeout=max(10.0, timeout),
        blocking_policy=policy,
    )


def stage_in_review_scope(stage: str, cfg: ReviewConfig) -> bool:
    """阶段是否在自审档位内（build / selftest / integrate 三类档位）。"""
    if "build" in cfg.stages and stage in ENG_BUILD_STAGES:
        return True
    if "selftest" in cfg.stages and stage.endswith("_selftest"):
        return True
    return "integrate" in cfg.stages and stage == "eng_integrate"


def select_lenses(rounds: int) -> list[Lens]:
    """按轮次裁剪视角。

    3 轮（默认）取「正确性 → 边界 → 测试缺口」；>=5 轮取全量。
    4 轮按轻量模式处理——并发/安全需要更大的上下文预算，不做半吊子插入。
    """
    if rounds <= 0:
        return []
    if rounds >= len(LENSES):
        return list(LENSES)
    light = [lens for lens in LENSES if lens.name in _LIGHT_LENS_NAMES]
    return light[:rounds]


# --- 数据模型 -----------------------------------------------------------------


class Finding(BaseModel):
    """一条审查发现。严重级决定它是否阻断阶段。"""

    severity: Severity = Severity.MINOR
    where: str = ""
    issue: str = ""
    fix: str = ""


class ReviewScope(BaseModel):
    """本轮审查对象——回答「审查了什么」，同时作为提示词里的范围清单。"""

    stage: str
    role: str
    changed_files: list[str] = Field(default_factory=list)
    diff_added: int = 0
    diff_removed: int = 0
    contract_files: list[str] = Field(default_factory=list)
    upstream_files: list[str] = Field(default_factory=list)
    checklist_items: list[str] = Field(default_factory=list)

    def render(self) -> str:
        lines = [f"- 阶段：`{self.stage}`（角色 `{self.role}`）"]
        if self.changed_files:
            lines.append(
                f"- 改动文件：{len(self.changed_files)} 个（+{self.diff_added}/-{self.diff_removed} 行）"
            )
            lines.extend(f"  - `{name}`" for name in self.changed_files[:30])
        else:
            lines.append("- 改动文件：未检测到（无 git 差异，按工作区文件清单判断）")
        if self.contract_files:
            lines.append("- 本阶段契约产物：" + "、".join(f"`{n}`" for n in self.contract_files))
        if self.upstream_files:
            lines.append("- 上游产物：" + "、".join(f"`{n}`" for n in self.upstream_files))
        if self.checklist_items:
            lines.append("- 实现清单：")
            lines.extend(f"  {item}" for item in self.checklist_items[:20])
        return "\n".join(lines)


class LensRun(BaseModel):
    """单轮审查的完整记录（逐轮写进 review.jsonl）。"""

    lens: str
    title: str
    round: int
    status: LensStatus
    reviewed: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_ms: int = 0
    reply_chars: int = 0
    error: str = ""


class ReviewLog(BaseModel):
    """一次阶段自审的汇总记录（写 review.json / 转 StageResult.meta）。"""

    initiative_id: str
    stage: str
    role: str
    harness: str = ""
    mode: str = "session"  # session | static
    session_id: str = ""
    rounds_requested: int = 0
    lenses: list[LensRun] = Field(default_factory=list)
    scope: ReviewScope
    verdict: str = "not_run"  # passed | findings | blocking | degraded | not_run
    blocking_count: int = 0
    degraded: bool = False
    started_at: str = ""
    ended_at: str = ""
    duration_ms: int = 0
    log_path: str = ""
    log_lines_path: str = ""

    @property
    def executed(self) -> bool:
        """是否至少有一轮真正跑过（skipped / prompted 不算，不能包装成审过了）。"""
        return any(
            run.status not in {LensStatus.SKIPPED, LensStatus.PROMPTED} for run in self.lenses
        )

    def summary(self) -> dict[str, Any]:
        """给 StageResult.meta 的精简视图（不塞整份 findings）。"""
        blocking = [
            finding.model_dump()
            for run in self.lenses
            for finding in run.findings
            if finding.severity == Severity.BLOCKING
        ]
        return {
            "verdict": self.verdict,
            "mode": self.mode,
            "rounds": len(self.lenses),
            "blocking": self.blocking_count,
            "degraded": self.degraded,
            "log_path": self.log_path,
            "log_lines_path": self.log_lines_path,
            "blocking_findings": blocking[:5],
            "statuses": {run.lens: run.status.value for run in self.lenses},
        }


# --- 解析 ---------------------------------------------------------------------


def parse_diff_stats(text: str) -> tuple[list[str], int, int]:
    """从 unified diff 提取改动文件与增删行数（无 git 时兼容 worktree 文件清单）。"""
    body = text or ""
    if body.startswith("# worktree files"):
        files = [
            line[2:].strip()
            for line in body.splitlines()
            if line.startswith("+ ") and len(line) > 2
        ]
        return files[:200], 0, 0

    files: list[str] = []
    added = removed = 0
    for line in body.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files.append(parts[1].strip())
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return list(dict.fromkeys(files))[:200], added, removed


def _checklist_items(text: str) -> list[str]:
    """抽取 `checklist.md` 里的实现项（`- [ ]` / `- [x]`）。"""
    items = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("- [ ]", "- [x]", "- [X]")):
            items.append(stripped)
    return items[:30]


def build_scope(ctx: StageContext, diff: str = "", checklist_text: str = "") -> ReviewScope:
    """组装审查范围：git 改动 + 本阶段契约文件 + 上游产物 + 实现清单。

    ``diff.patch`` 本身不列进契约产物——它不是要审的对象，改动文件清单已经覆盖。
    """
    from sortie_deck.contracts import REQUIRED

    changed, added, removed = parse_diff_stats(diff)
    contract_files = [n for n in REQUIRED.get(ctx.stage, []) if n != "diff.patch"]
    return ReviewScope(
        stage=ctx.stage,
        role=role_key(ctx.role),
        changed_files=changed,
        diff_added=added,
        diff_removed=removed,
        contract_files=contract_files,
        upstream_files=sorted(ctx.upstream_artifacts),
        checklist_items=_checklist_items(checklist_text),
    )


def parse_review_reply(text: str) -> tuple[LensStatus, list[Finding], list[str], str]:
    """解析回复里的 ```review 块 → (status, findings, reviewed, summary)。

    无块 / JSON 损坏 → ``(UNPARSED, [], [], "")``，由调用方标降级而不是失败。
    """
    for block in _REVIEW_BLOCK_RE.findall(text or ""):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        findings: list[Finding] = []
        for row in data.get("findings") or []:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("severity") or "minor").strip().lower()
            severity = {
                "blocking": Severity.BLOCKING,
                "major": Severity.MAJOR,
                "minor": Severity.MINOR,
            }.get(raw, Severity.MINOR)
            findings.append(
                Finding(
                    severity=severity,
                    where=str(row.get("where") or "")[:300],
                    issue=str(row.get("issue") or "")[:1000],
                    fix=str(row.get("fix") or "")[:600],
                )
            )
            if len(findings) >= _MAX_FINDINGS:
                break
        reviewed = [str(x)[:300] for x in (data.get("reviewed") or []) if str(x).strip()]
        status = _derive_status(str(data.get("status") or ""), findings)
        return status, findings, reviewed[:_MAX_REVIEWED], str(data.get("summary") or "")[:1000]
    return LensStatus.UNPARSED, [], [], ""


def _derive_status(raw: str, findings: list[Finding]) -> LensStatus:
    """模型自报优先；缺省或非法值按 findings 严重级推导。"""
    if raw in {"passed", "findings", "blocking"}:
        return LensStatus(raw)
    if any(f.severity == Severity.BLOCKING for f in findings):
        return LensStatus.BLOCKING
    return LensStatus.FINDINGS if findings else LensStatus.PASSED


def compute_verdict(runs: list[LensRun]) -> str:
    """汇总多轮结论：blocking > degraded > findings > passed。"""
    executed = [
        run
        for run in runs
        if run.status
        in {
            LensStatus.PASSED,
            LensStatus.FINDINGS,
            LensStatus.BLOCKING,
            LensStatus.UNPARSED,
            LensStatus.FAILED,
        }
    ]
    if not executed:
        return "not_run"
    if any(run.status == LensStatus.BLOCKING for run in runs):
        return "blocking"
    if any(run.status in {LensStatus.UNPARSED, LensStatus.FAILED} for run in runs):
        return "degraded"
    if any(run.status == LensStatus.FINDINGS for run in runs):
        return "findings"
    return "passed"


# --- 提示词 -------------------------------------------------------------------


def build_lens_prompt(
    lens: Lens,
    index: int,
    total: int,
    scope: ReviewScope,
    previous: list[LensRun],
) -> str:
    """单轮审查提示词：范围 + 视角 + 输出协议。

    只带范围与视角，不重复贴需求全文——阶段首轮已经把 PRD/产物给过了，
    这里再灌一遍只会让 self-review 变成「重读 checklist」。
    """
    rule = (
        "发现问题就地在代码里修复，修复后继续检查；确实无法修复的标 blocking。"
        if lens.fix_allowed
        else "本轮只记录缺口，禁止修改任何文件（包括测试文件）。"
    )
    done = "、".join(f"{run.title}={run.status.value}" for run in previous)
    history = f"\n前面几轮结论（不要重复审这些角度）：{done}\n" if done else ""
    return (
        f"## 多镜头自审 · 第 {index}/{total} 轮：{lens.title}\n\n"
        "你正在同一个任务、同一个会话里做阶段收尾自审。审查对象是本阶段的真实改动，"
        "不是重新读一遍需求。\n\n"
        f"### 审查范围\n{scope.render()}\n"
        f"{history}\n"
        f"### 本轮视角\n{lens.instruction}\n\n"
        "### 规则\n"
        f"- {rule}\n"
        "- 只审本轮视角，不要罗列其他视角的结论。\n"
        "- 需要看细节时用 Read 打开上面的文件路径，不要凭文件名猜内容。\n"
        "- 不要输出整份 diff，不要复述需求。\n"
        "- 回复结尾必须输出**一个** ```review 围栏块（JSON，不加注释）：\n"
        "```review\n"
        '{"status": "passed|findings|blocking", "reviewed": ["你实际检查的文件:位置"], '
        '"findings": [{"severity": "blocking|major|minor", "where": "file:line", '
        '"issue": "问题", "fix": "怎么修"}], "summary": "一句话结论"}\n'
        "```\n"
        "- 没发现问题时 status=passed、findings=[]；问题已在本轮修好且无残留时也用 passed，"
        "并在 summary 写明修了什么。"
    )


# --- 日志落盘 -----------------------------------------------------------------


def review_log_dir(data_dir: Path, initiative_id: str, stage: str) -> Path:
    """日志目录：``missions/<id>/logs/<stage>``（过程文件，不进对外产物）。"""
    from sortie_deck.layout import mission_logs_dir

    return mission_logs_dir(Path(data_dir), initiative_id) / stage


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _persist(log: ReviewLog, data_dir: Path | None) -> ReviewLog:
    """写 review.json；任何 IO 失败只告警，不影响阶段结果。"""
    if data_dir is None:
        return log
    log_dir = review_log_dir(Path(data_dir), log.initiative_id, log.stage)
    log.log_path = str(log_dir / "review.json")
    log.log_lines_path = str(log_dir / "review.jsonl")
    try:
        _write_json(Path(log.log_path), log.model_dump(mode="json"))
    except OSError:
        logger.warning(
            "自审汇总日志写入失败: initiative=%s stage=%s", log.initiative_id, log.stage
        )
    return log


def _append_run_log(log: ReviewLog, run: LensRun) -> None:
    """逐轮追加审计行：进程崩溃时仍保留已完成的轮次。"""
    if not log.log_lines_path:
        return
    line = {
        "initiative_id": log.initiative_id,
        "stage": log.stage,
        "role": log.role,
        "harness": log.harness,
        "session_id": log.session_id,
        **run.model_dump(mode="json"),
    }
    try:
        _append_jsonl(Path(log.log_lines_path), line)
    except OSError:
        logger.warning(
            "自审逐轮日志写入失败: initiative=%s stage=%s lens=%s",
            log.initiative_id,
            log.stage,
            run.lens,
        )


# --- 执行 ---------------------------------------------------------------------

SendTurn = Callable[[str], Awaitable[Any]]


async def run_review_lenses(
    ctx: StageContext,
    scope: ReviewScope,
    send_turn: SendTurn,
    *,
    rounds: int,
    timeout: float,
    harness: str = "",
    session_id: str = "",
    data_dir: Path | None = None,
) -> ReviewLog:
    """在同一会话上依次跑 N 轮审查，返回汇总日志。

    ``send_turn`` 由执行器注入（复用同一个 provider/session），因此这里不感知
    vendor。任何一轮超时/异常只把该轮标 failed，后续轮次继续——自审是增强项。
    """
    lenses = select_lenses(rounds)
    started = time.monotonic()
    log = ReviewLog(
        initiative_id=ctx.initiative_id,
        stage=ctx.stage,
        role=role_key(ctx.role),
        harness=harness,
        mode="session",
        session_id=session_id,
        rounds_requested=rounds,
        scope=scope,
        started_at=utc_now().isoformat(),
    )
    if data_dir is not None:
        log_dir = review_log_dir(Path(data_dir), ctx.initiative_id, ctx.stage)
        log.log_path = str(log_dir / "review.json")
        log.log_lines_path = str(log_dir / "review.jsonl")

    runs: list[LensRun] = []
    for index, lens in enumerate(lenses, start=1):
        prompt = build_lens_prompt(lens, index, len(lenses), scope, runs)
        run = LensRun(
            lens=lens.name,
            title=lens.title,
            round=index,
            status=LensStatus.FAILED,
            started_at=utc_now().isoformat(),
        )
        run_started = time.monotonic()
        try:
            reply = await asyncio.wait_for(send_turn(prompt), timeout=timeout)
        except TimeoutError:
            run.error = f"timeout after {timeout:.0f}s"
            logger.warning(
                "自审轮次超时: initiative=%s stage=%s lens=%s timeout=%.0fs",
                ctx.initiative_id,
                ctx.stage,
                lens.name,
                timeout,
            )
        except Exception as exc:  # noqa: BLE001 — 单轮失败不打断阶段
            run.error = str(exc)[:300]
            logger.warning(
                "自审轮次异常: initiative=%s stage=%s lens=%s",
                ctx.initiative_id,
                ctx.stage,
                lens.name,
                exc_info=True,
            )
        else:
            text = str(getattr(reply, "text", "") or "")
            run.reply_chars = len(text)
            status, findings, reviewed, summary = parse_review_reply(text)
            run.status = status
            run.findings = findings
            run.reviewed = reviewed
            run.summary = summary
            if status == LensStatus.UNPARSED:
                run.error = "回复缺少 ```review 协议块"
        run.ended_at = utc_now().isoformat()
        run.duration_ms = int((time.monotonic() - run_started) * 1000)
        runs.append(run)
        _append_run_log(log, run)
        logger.info(
            "自审轮次完成: initiative=%s stage=%s lens=%s status=%s findings=%s duration=%dms",
            ctx.initiative_id,
            ctx.stage,
            lens.name,
            run.status.value,
            len(run.findings),
            run.duration_ms,
        )

    log.lenses = runs
    log.verdict = compute_verdict(runs)
    log.blocking_count = sum(
        1 for run in runs for finding in run.findings if finding.severity == Severity.BLOCKING
    )
    log.degraded = any(run.status in {LensStatus.UNPARSED, LensStatus.FAILED} for run in runs)
    log.ended_at = utc_now().isoformat()
    log.duration_ms = int((time.monotonic() - started) * 1000)
    return _persist(log, data_dir)


def build_skipped_log(
    ctx: StageContext,
    scope: ReviewScope,
    *,
    reason: str,
    harness: str = "",
    data_dir: Path | None = None,
) -> ReviewLog:
    """未实际执行的记录（mock harness / 首轮失败），保留「为什么没审」。"""
    now = utc_now().isoformat()
    cfg = load_review_config()
    log = ReviewLog(
        initiative_id=ctx.initiative_id,
        stage=ctx.stage,
        role=role_key(ctx.role),
        harness=harness,
        mode="session",
        rounds_requested=cfg.rounds,
        scope=scope,
        verdict="not_run",
        started_at=now,
        ended_at=now,
    )
    log.lenses = [
        LensRun(
            lens=lens.name,
            title=lens.title,
            round=index,
            status=LensStatus.SKIPPED,
            summary=reason,
            started_at=now,
            ended_at=now,
        )
        for index, lens in enumerate(select_lenses(cfg.rounds), start=1)
    ]
    return _persist(log, data_dir)


def record_static_review(
    ctx: StageContext,
    scope: ReviewScope,
    *,
    harness: str,
    rounds: int,
    data_dir: Path | None = None,
) -> ReviewLog:
    """CLI 单发模式的记录：已把 lens 清单写进提示词，但编排层无法验证结论。

    状态用 ``prompted`` 而不是 ``passed``——我们不能假装审过了。
    """
    now = utc_now().isoformat()
    log = ReviewLog(
        initiative_id=ctx.initiative_id,
        stage=ctx.stage,
        role=role_key(ctx.role),
        harness=harness,
        mode="static",
        rounds_requested=rounds,
        scope=scope,
        verdict="not_run",
        started_at=now,
        ended_at=now,
    )
    log.lenses = [
        LensRun(
            lens=lens.name,
            title=lens.title,
            round=index,
            status=LensStatus.PROMPTED,
            summary="已写入提示词要求自检；CLI 单发模式无法验证结论",
            started_at=now,
            ended_at=now,
        )
        for index, lens in enumerate(select_lenses(rounds), start=1)
    ]
    return _persist(log, data_dir)


def build_static_review_section(rounds: int) -> str:
    """CLI 执行器的静态自检清单（多轮续跑不可用，只能一次说清）。"""
    lenses = select_lenses(rounds)
    if not lenses:
        return ""
    lines = ["## Self-review before you finish（收尾前自检，按顺序逐项过一遍）"]
    for index, lens in enumerate(lenses, start=1):
        rule = "发现问题直接修" if lens.fix_allowed else "只记录缺口，不要改代码"
        lines.append(f"{index}. **{lens.title}**：{lens.instruction}（{rule}）")
    lines.append(
        "最后在回复里加一节 `## Self-review`，逐条写：视角、检查了什么、结论"
        "（passed / findings / blocking）；有 blocking 时不要宣告 DONE。"
    )
    return "\n".join(lines)


# --- 渲染 ---------------------------------------------------------------------


def render_review_markdown(log: ReviewLog) -> str:
    """把汇总日志渲染成 review.md（进阶段产物，供人工门禁与下游阶段阅读）。"""
    lines = [
        f"# 多镜头自审报告 — `{log.stage}`",
        "",
        f"- 结论：`{log.verdict}`（blocking {log.blocking_count} 条）",
        f"- 模式：`{log.mode}` · harness `{log.harness or '-'}` · 轮次 {len(log.lenses)}",
        f"- 会话：`{log.session_id or '-'}` · 耗时 {log.duration_ms} ms",
        f"- 日志：`{log.log_path or '-'}` / `{log.log_lines_path or '-'}`",
        "",
        "## 审查范围",
        log.scope.render(),
        "",
    ]
    for run in log.lenses:
        lines.append(f"## 第 {run.round} 轮 · {run.title} — `{run.status.value}`")
        if run.summary:
            lines.append(f"- 结论：{run.summary}")
        if run.reviewed:
            lines.append("- 实际检查：")
            lines.extend(f"  - {item}" for item in run.reviewed[:20])
        if run.findings:
            lines.append("- 发现：")
            for finding in run.findings:
                location = f" `{finding.where}`" if finding.where else ""
                fix = f"；建议：{finding.fix}" if finding.fix else ""
                lines.append(f"  - [{finding.severity.value}]{location} {finding.issue}{fix}")
        if run.error:
            lines.append(f"- 异常：{run.error}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
