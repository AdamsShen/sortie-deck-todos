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
import inspect
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
    circuit_breaker: bool = True
    # 自适应档位：按复杂度改轮次（0 轮 = 按老逻辑跳过 LLM 自审）
    adaptive: bool = True
    rounds_simple: int = 0
    rounds_standard: int = 3
    rounds_complex: int = 5

    def rounds_for(self, level: str) -> int:
        """复杂度档位 → 实际轮次。未识别档位回落到 ``rounds``。"""
        key = str(level or "").strip().lower()
        if key == "simple":
            return max(0, self.rounds_simple)
        if key == "standard":
            return max(0, self.rounds_standard)
        if key == "complex":
            return max(0, self.rounds_complex)
        return max(0, self.rounds)


# 未命中 harness 映射时的兜底超时：编码型 agent loop 单回合常见 5–10 分钟，
# 300s 会让每轮都撞线（见 settings.review_lens_timeout_by_harness 注释）。
_DEFAULT_LENS_TIMEOUT = 600.0


def _timeout_by_harness(raw: Any) -> dict[str, float]:
    """解析 `claude_code=900,codex=900` 形式的映射，非法项直接丢弃。"""
    mapping: dict[str, float] = {}
    for part in str(raw or "").split(","):
        name, _, value = part.partition("=")
        key = name.strip().lower()
        if not key:
            continue
        try:
            seconds = float(value.strip())
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            mapping[key] = seconds
    return mapping


def load_review_config(harness: str | None = None) -> ReviewConfig:
    """读取自审配置；settings 缺字段时按「关闭」处理（单测/旧配置安全）。

    超时优先级：显式 ``review_lens_timeout_seconds`` > 该 harness 的自适应值 >
    兜底 600s。显式值写 0 表示「交给 harness 自适应」。
    """
    from sortie_deck.settings import settings

    enabled = bool(getattr(settings, "review_lenses_enabled", False))
    try:
        rounds = int(getattr(settings, "review_lens_rounds", 3) or 3)
    except (TypeError, ValueError):
        rounds = 3
    raw_stages = str(getattr(settings, "review_lens_stages", "build") or "")
    stages = frozenset(part.strip().lower() for part in raw_stages.split(",") if part.strip())
    try:
        configured = float(getattr(settings, "review_lens_timeout_seconds", 0.0) or 0.0)
    except (TypeError, ValueError):
        configured = 0.0
    if configured > 0:
        timeout = configured
    else:
        mapping = _timeout_by_harness(getattr(settings, "review_lens_timeout_by_harness", ""))
        key = str(harness or "").strip().lower()
        timeout = mapping.get(key, _DEFAULT_LENS_TIMEOUT)
    policy = str(getattr(settings, "review_blocking_policy", "hitl") or "hitl").strip().lower()
    if policy not in {"hitl", "fail", "ignore"}:
        policy = "hitl"
    def _as_int(name: str, fallback: int) -> int:
        try:
            return max(0, int(getattr(settings, name, fallback) or fallback))
        except (TypeError, ValueError):
            return fallback

    return ReviewConfig(
        enabled=enabled,
        rounds=max(0, rounds),
        stages=stages,
        timeout=max(10.0, timeout),
        blocking_policy=policy,
        circuit_breaker=bool(getattr(settings, "review_lens_circuit_breaker", True)),
        adaptive=bool(getattr(settings, "review_adaptive", True)),
        rounds_simple=_as_int("review_rounds_simple", 0),
        rounds_standard=_as_int("review_rounds_standard", 3),
        rounds_complex=_as_int("review_rounds_complex", 5),
    )


def stage_in_review_scope(stage: str, cfg: ReviewConfig) -> bool:
    """阶段是否在自审档位内（build / integrate 两类档位）。

    自测阶段（``*_selftest``）不在范围内：它已改为「跑一次 runner → 读
    selftest_result.json → 只修失败项」，本阶段 diff 里只有注入的
    ``scripts/selftest_runner.sh`` 与几个结果文件，审这些等于重复审编码阶段的
    产出。自测的质量兜底交给 selftest_result.json 与下游 QA，不再消耗 N 轮模型
    往返。``TDT_REVIEW_LENS_STAGES`` 里写 ``selftest`` 也不再生效。
    """
    if "build" in cfg.stages and stage in ENG_BUILD_STAGES:
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
    # 改动面来源：diff = 真实 git 差异；workspace = 无差异时按现存文件推断
    source: str = "diff"
    # 供 prompt 直接消费的 diff 节选（已按预算裁剪）；统计仍用完整 diff。
    # 为空表示本次没有可给的正文（无改动 / 影子仓不可用 / 旧路径），prompt 会退回「自行 Read」。
    diff_excerpt: str = ""

    def render(self) -> str:
        lines = [f"- 阶段：`{self.stage}`（角色 `{self.role}`）"]
        if self.changed_files:
            if self.source == "workspace":
                lines.append(
                    f"- 工作区文件：{len(self.changed_files)} 个"
                    "（无 git 差异，按现存文件推断，无法给出增删行）"
                )
            else:
                lines.append(
                    f"- 改动文件：{len(self.changed_files)} 个"
                    f"（+{self.diff_added}/-{self.diff_removed} 行）"
                )
            lines.extend(f"  - `{name}`" for name in self.changed_files[:30])
        else:
            lines.append("- 改动文件：未检测到（无 git 差异，且工作区没有可审文件）")
        if self.contract_files:
            lines.append("- 本阶段契约产物：" + "、".join(f"`{n}`" for n in self.contract_files))
        if self.upstream_files:
            lines.append("- 上游产物：" + "、".join(f"`{n}`" for n in self.upstream_files))
        if self.checklist_items:
            lines.append("- 实现清单：")
            lines.extend(f"  {item}" for item in self.checklist_items[:20])
        if self.diff_excerpt:
            # 正文直接给到 prompt：没有这段，模型只能靠逐个 Read 了解改动
            lines.append("- 本阶段改动（unified diff，已按预算裁剪）：")
            lines.append("```diff")
            lines.append(self.diff_excerpt)
            lines.append("```")
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
    # 未执行时的顶层原因（skipped / prompted）：排障时不必逐轮翻 lens.summary
    reason: str = ""
    # 复杂度判定结果（level / score / signals / reasons），用于复盘与调阈值
    complexity: dict[str, Any] = Field(default_factory=dict)
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
        out: dict[str, Any] = {
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
        if self.reason:
            out["reason"] = self.reason
        if self.complexity:
            out["complexity"] = self.complexity
        return out


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


# 从新增行里抽符号名，用于「大文件被裁剪」时给模型一个索引
_DIFF_SYMBOL_RE = re.compile(
    r"^\+\s*(?:async\s+def|def|class|function|"
    r"export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const)|const)\s+([A-Za-z_$][\w$]*)"
)


def _split_diff_blocks(diff: str) -> tuple[str, list[list[str]]]:
    """按 ``diff --git`` 切块，返回 (preamble, blocks)。"""
    preamble: list[str] = []
    blocks: list[list[str]] = []
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            preamble.append(line)
    return "\n".join(preamble), blocks


def _block_symbols(block: list[str], limit: int = 12) -> list[str]:
    names: list[str] = []
    for line in block:
        matched = _DIFF_SYMBOL_RE.match(line)
        if matched and matched.group(1) not in names:
            names.append(matched.group(1))
        if len(names) >= limit:
            break
    return names


def _summarize_block(block: list[str], *, reason: str) -> str:
    """把超预算的单文件块压成「文件头 + 增删行数 + 符号清单」。"""
    added = sum(1 for line in block if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in block if line.startswith("-") and not line.startswith("---"))
    out = [block[0], f"# {reason}：+{added}/-{removed} 行"]
    symbols = _block_symbols(block)
    if symbols:
        out.append("# 符号：" + ", ".join(symbols))
    return "\n".join(out)


def clip_diff(
    diff: str,
    *,
    max_chars: int = 60_000,
    max_file_lines: int = 2_000,
    max_file_chars: int = 1_048_576,
) -> str:
    """把 unified diff 裁到 prompt 预算内（``diff.patch`` 产物不走这里）。

    为什么需要：24 个文件从 0 开发的 diff 就是全部文件内容，直接塞 prompt 会
    挤掉审查指令；而完全不塞又退回「模型逐个 Read」。按块裁剪能在保留文件面
    的同时给出可读的正文。

    规则：
    1. 单块超过 ``max_file_lines`` 行或 ``max_file_chars`` 字符 → 降级为
       文件头 + 增删行数 + 符号清单；
    2. 累计字符超过 ``max_chars`` → 后续块同样降级；
    3. 降级后仍超预算 → 只保留文件头。
    """
    body = diff or ""
    if not body.strip():
        return ""
    preamble, blocks = _split_diff_blocks(body)
    if not blocks:
        return body[:max_chars]

    out: list[str] = [preamble] if preamble.strip() else []
    used = len(preamble)
    clipped = False
    for block in blocks:
        text = "\n".join(block)
        over_file_chars = len(text) > max_file_chars
        too_long = len(block) > max_file_lines or over_file_chars
        over_budget = used + len(text) > max_chars
        if not too_long and not over_budget:
            out.append(text)
            used += len(text)
            continue
        clipped = True
        if over_file_chars and len(block) <= max_file_lines:
            reason = f"单文件超过 {max_file_chars} 字符，已省略正文"
        elif len(block) > max_file_lines:
            reason = f"单文件超过 {max_file_lines} 行，已省略正文"
        else:
            reason = "超出 diff 总量预算，已省略正文"
        summary = _summarize_block(block, reason=reason)
        if used + len(summary) > max_chars:
            summary = block[0]
        out.append(summary)
        used += len(summary)
    result = "\n".join(part for part in out if part)
    if clipped:
        result += f"\n# [diff 已裁剪：原始 {len(body)} 字符]"
    return result


def _checklist_items(text: str) -> list[str]:
    """抽取 `checklist.md` 里的实现项（`- [ ]` / `- [x]`）。"""
    items = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("- [ ]", "- [x]", "- [X]")):
            items.append(stripped)
    return items[:30]


# 兜底扫描时要跳过的目录：编排层注入（.upstream/.archive）与依赖目录都不是审查对象
_SCOPE_SKIP_PARTS = frozenset(
    {
        ".git",
        ".upstream",
        ".archive",
        ".runs",
        "node_modules",
        "__pycache__",
        ".cursor",
        ".sortie-deck",
        ".write_probe",
    }
)


def _workspace_file_list(workspace: Path, limit: int = 40) -> list[str]:
    """无 git 差异时的兜底：按现存文件推断本阶段改动面。

    为什么需要：没绑仓库的任务里 workspace 不是 git 仓库，代码写在
    ``workspace/repos/<name>`` 下，``git diff`` 拿不到任何改动，自审范围就成了
    空表，模型只能凭 checklist 审——等于放弃了「审真实改动」这个前提。
    跳过编排层注入目录与依赖目录，按 mtime 倒序取最近写的文件。
    """
    rows: list[tuple[float, str]] = []
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace)
        if any(part in _SCOPE_SKIP_PARTS for part in rel.parts):
            continue
        if path.name == "README.md":
            continue
        try:
            rows.append((path.stat().st_mtime, str(rel)))
        except OSError:
            continue
    rows.sort(key=lambda row: row[0], reverse=True)
    return [rel for _, rel in rows[:limit]]


def build_scope(
    ctx: StageContext,
    diff: str = "",
    checklist_text: str = "",
    *,
    workspace: Path | None = None,
    diff_excerpt: str = "",
) -> ReviewScope:
    """组装审查范围：git 改动 + 本阶段契约文件 + 上游产物 + 实现清单。

    ``diff.patch`` 本身不列进契约产物——它不是要审的对象，改动文件清单已经覆盖。
    ``workspace`` 传入且 diff 解析不出文件时，退化为工作区现存文件清单（标记
    ``source="workspace"``，提示词与报告里都会注明「无法给出增删行」）。
    """
    from sortie_deck.contracts import REQUIRED

    changed, added, removed = parse_diff_stats(diff)
    source = "diff"
    if not changed and workspace is not None:
        inferred = _workspace_file_list(workspace)
        if inferred:
            changed, source = inferred, "workspace"
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
        source=source,
        diff_excerpt=diff_excerpt,
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
    disallowed: list[str] | None = None,
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
    # 有 diff 正文时明令禁止逐个 Read：这是 24 文件场景下 16 分钟空转的直接原因
    read_rule = (
        "- 改动已在上面给出，默认不要逐个 Read 文件；仅在需要调用链上下文时读取，最多 3 次。\n"
        if scope.diff_excerpt
        else "- 需要看细节时用 Read 打开上面的文件路径，不要凭文件名猜内容。\n"
    )
    # 禁用说明必须写进规则区：追加到 prompt 末尾会盖掉后面的 ```review 输出协议
    banned_rule = (
        f"- 本轮不可用工具：{', '.join(disallowed)}（已禁用，不要尝试调用）；"
        "需要行号时以 diff 的 hunk 头为准。\n"
        if disallowed
        else ""
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
        f"{banned_rule}"
        "- 只审本轮视角，不要罗列其他视角的结论。\n"
        f"{read_rule}"
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
# 进度回调：同步或异步均可，异常一律吞掉（进度不能反过来失败掉阶段）
ProgressFn = Callable[[dict[str, Any]], Any]


async def _notify_progress(fn: ProgressFn | None, payload: dict[str, Any]) -> None:
    """把一轮进度交给编排层；回调缺失/抛错都只告警，不影响自审。"""
    if fn is None:
        return
    try:
        result = fn(payload)
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 — 进度是观测增强项
        logger.warning("自审进度回调失败: %s", payload.get("phase"), exc_info=True)


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
    on_progress: ProgressFn | None = None,
    circuit_breaker: bool = True,
    complexity: dict[str, Any] | None = None,
    disallowed_tools: list[str] | None = None,
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
        complexity=dict(complexity or {}),
        started_at=utc_now().isoformat(),
    )
    if data_dir is not None:
        log_dir = review_log_dir(Path(data_dir), ctx.initiative_id, ctx.stage)
        log.log_path = str(log_dir / "review.json")
        log.log_lines_path = str(log_dir / "review.jsonl")

    runs: list[LensRun] = []
    # 熔断额度：任意一轮超时后仍允许「再等一轮」，第二次超时即跳过剩余视角。
    # 三个视角各超时一次 = 15 分钟纯等待，UI 上完全看不出在干什么。
    timeout_credits = 1
    skip_reason = ""
    for index, lens in enumerate(lenses, start=1):
        if skip_reason:
            skipped = LensRun(
                lens=lens.name,
                title=lens.title,
                round=index,
                status=LensStatus.SKIPPED,
                summary=skip_reason,
                started_at=utc_now().isoformat(),
                ended_at=utc_now().isoformat(),
            )
            runs.append(skipped)
            _append_run_log(log, skipped)
            await _notify_progress(
                on_progress,
                {
                    "phase": "review_skip",
                    "round": index,
                    "total": len(lenses),
                    "lens": lens.name,
                    "title": lens.title,
                    "status": skipped.status.value,
                },
            )
            continue

        prompt = build_lens_prompt(lens, index, len(lenses), scope, runs, disallowed_tools or [])
        run = LensRun(
            lens=lens.name,
            title=lens.title,
            round=index,
            status=LensStatus.FAILED,
            started_at=utc_now().isoformat(),
        )
        run_started = time.monotonic()
        await _notify_progress(
            on_progress,
            {
                "phase": "review_start",
                "round": index,
                "total": len(lenses),
                "lens": lens.name,
                "title": lens.title,
                "timeout": timeout,
            },
        )
        try:
            reply = await asyncio.wait_for(send_turn(prompt), timeout=timeout)
        except TimeoutError:
            run.error = f"timeout after {timeout:.0f}s"
            logger.warning(
                "自审轮次超时: initiative=%s stage=%s lens=%s timeout=%.0fs credits=%d",
                ctx.initiative_id,
                ctx.stage,
                lens.name,
                timeout,
                timeout_credits,
            )
            if circuit_breaker:
                if timeout_credits <= 0:
                    skip_reason = f"熔断：第 {index} 轮超时，跳过剩余审查"
                else:
                    timeout_credits -= 1
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
        await _notify_progress(
            on_progress,
            {
                "phase": "review_end",
                "round": index,
                "total": len(lenses),
                "lens": lens.name,
                "title": lens.title,
                "status": run.status.value,
                "findings": len(run.findings),
                "duration_ms": run.duration_ms,
                "error": run.error,
            },
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
    complexity: dict[str, Any] | None = None,
) -> ReviewLog:
    """未实际执行的记录（mock harness / 首轮失败 / 简单任务），保留「为什么没审」。"""
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
        reason=reason,
        complexity=dict(complexity or {}),
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
        reason="CLI 单发模式：已写入提示词，编排层无法验证结论",
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
    if log.complexity:
        level = log.complexity.get("level", "?")
        detail = "；".join(log.complexity.get("reasons") or [])
        lines.insert(3, f"- 复杂度：`{level}`" + (f"（{detail}）" if detail else ""))
    if log.reason:
        # 跳过/静态模式的真实原因放在最显眼处：否则报告只写 not_run 无从判断
        lines.insert(3, f"- 未执行原因：{log.reason}")
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
