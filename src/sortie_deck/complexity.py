"""开发任务复杂度判定：决定是否跑多镜头自审、跑几轮。

为什么用规则而不是让模型自评（编排层打分，零成本、可复现）：

- 多镜头自审是「阶段收尾的增强项」，每轮都是一次完整的模型往返
  （claude_code 单轮实测 20s 起、复杂时 5-10 分钟），简单改动套上 3 轮
  纯属浪费。业内成熟做法也是先用廉价信号估难度再决定投入
  （ARES / CogER 的 adaptive reasoning effort、RouteLLM 的难度阈值路由、
  Google CL 的「一次 review 约 100 行」规模经验）。
- 让 agent 自评要多一轮请求，且自评不可复现、无法审计；规则打分可以把
  「凭什么判 simple」写进 review.json，事后能复盘与调阈值。

判定口径（默认值均可通过 settings 调整）：

- ``complex``：diff 行数 > 800，或跨模块数 >= 3，或命中高风险词
  （重构/迁移/并发/支付/权限/鉴权/多端/架构/协议/auth/payment/migration…）
- ``simple``：改动文件 <= 3 且 diff 行数 < 200 且只有 1 个模块，且未命中高风险词
- 其余为 ``standard``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# 辅助目录不计入「模块」：测试/文档/运行期数据跟着业务代码走，
# 单独算模块会把「改了代码 + 顺手更新测试」误判成跨模块。
_AUX_ROOTS = frozenset(
    {
        "tests",
        "test",
        "__tests__",
        "docs",
        "doc",
        "scripts",
        "examples",
        "fixtures",
        "data",
        "assets",
        ".github",
    }
)

# 这些是「容器型」根目录，真正的模块在第二层（src/server、src/game）
_CONTAINER_ROOTS = frozenset(
    {"src", "lib", "app", "apps", "packages", "services", "server", "api", "internal", "cmd", "web"}
)

_DEFAULT_HIGH_RISK_KEYWORDS = (
    # 中文按子串匹配
    "重构",
    "迁移",
    "并发",
    "支付",
    "权限",
    "鉴权",
    "多端",
    "架构",
    "协议",
    "加密",
    "审计",
    # 英文按单词边界匹配
    "refactor",
    "migration",
    "concurrency",
    "payment",
    "auth",
    "oauth",
    "jwt",
    "encryption",
)


@dataclass(frozen=True)
class ComplexityLimits:
    """判定阈值（来自 settings，可热调）。

    默认分档（2026-11-05 按实际使用校准）：

    - simple：<= 3 个文件、<= 500 行、单模块；
    - standard：<= 10 个文件、<= 1500 行（其余不满足 complex 的情形）；
    - complex：> 10 个文件、或 > 1500 行、或跨模块 >= 3、或命中高风险词。
    """

    simple_max_files: int = 3
    simple_max_diff_lines: int = 500
    complex_min_files: int = 11
    complex_min_diff_lines: int = 1500
    complex_min_modules: int = 3
    high_risk_keywords: tuple[str, ...] = _DEFAULT_HIGH_RISK_KEYWORDS

    @classmethod
    def from_settings(cls) -> ComplexityLimits:
        """读 settings；缺字段时用默认值（单测/旧配置安全）。"""
        from sortie_deck.settings import settings

        raw = str(getattr(settings, "review_complexity_high_risk_keywords", "") or "")
        keywords = tuple(part.strip() for part in raw.split(",") if part.strip())
        return cls(
            simple_max_files=int(getattr(settings, "review_complexity_simple_max_files", 3) or 3),
            simple_max_diff_lines=int(
                getattr(settings, "review_complexity_simple_max_diff_lines", 500) or 500
            ),
            complex_min_files=int(
                getattr(settings, "review_complexity_complex_min_files", 11) or 11
            ),
            complex_min_diff_lines=int(
                getattr(settings, "review_complexity_complex_min_diff_lines", 1500) or 1500
            ),
            complex_min_modules=int(
                getattr(settings, "review_complexity_complex_min_modules", 3) or 3
            ),
            high_risk_keywords=keywords or _DEFAULT_HIGH_RISK_KEYWORDS,
        )


@dataclass(frozen=True)
class ComplexityReport:
    """一次复杂度判定的结果（进 review.json / StageResult.meta.review）。"""

    level: str  # simple | standard | complex
    score: float
    signals: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "score": round(self.score, 3),
            "signals": self.signals,
            "reasons": list(self.reasons),
        }

    def summary(self) -> str:
        files = self.signals.get("files", 0)
        lines = self.signals.get("diff_lines", 0)
        modules = self.signals.get("modules", [])
        return f"{files} 文件 / {lines} 行 / {len(modules)} 模块"


def _strip_repo_prefix(path: str) -> list[str]:
    """去掉任务沙盒的 ``repos/<name>/`` 前缀，只留业务内路径。"""
    parts = [part for part in str(path or "").replace("\\", "/").strip("/").split("/") if part]
    if len(parts) >= 3 and parts[0] == "repos":
        return parts[2:]
    return parts


def module_of(path: str) -> str:
    """把文件路径归到「模块」，辅助目录/根级配置文件归为 aux。

    - 容器型根目录取第二层：``src/server/api.js`` → ``src/server``；
    - 其余取第一段：``public/js/app.js`` → ``public``（避免 public/index.html
      与 public/js/app.js 被算成两个模块）；
    - 根级单段文件（package.json / README.md）不构成模块。
    """
    parts = _strip_repo_prefix(path)
    if not parts:
        return ""
    root = parts[0]
    if root in _AUX_ROOTS or len(parts) == 1:
        return "aux"
    if root in _CONTAINER_ROOTS and len(parts) >= 3:
        return "/".join(parts[:2])
    return root


def modules_of(paths: Iterable[str]) -> list[str]:
    """去重后的模块列表（不含 aux），用于判断是否跨模块。"""
    seen: list[str] = []
    for path in paths:
        module = module_of(path)
        if module and module != "aux" and module not in seen:
            seen.append(module)
    return seen


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    """高风险词命中；英文按单词边界，中文按子串（避免 auth 命中 author）。"""
    if not text:
        return []
    lowered = text.lower()
    hits: list[str] = []
    for keyword in keywords:
        word = keyword.strip()
        if not word:
            continue
        lowered_word = word.lower()
        if re.search(r"[a-z]", lowered_word):
            if re.search(rf"\b{re.escape(lowered_word)}\b", lowered):
                hits.append(word)
        elif word in text:
            hits.append(word)
    return hits


def estimate_complexity(
    *,
    changed_files: list[str],
    diff_added: int = 0,
    diff_removed: int = 0,
    brief: str = "",
    checklist: str = "",
    limits: ComplexityLimits | None = None,
) -> ComplexityReport:
    """按改动规模 / 模块跨度 / 高风险词判定复杂度。

    输入取「本阶段真实改动」，没有 diff 时（未绑仓库）用工作区文件清单兜底，
    此时 diff 行数为 0、只按文件数与模块跨度判。
    """
    cfg = limits or ComplexityLimits.from_settings()
    files = [str(path) for path in changed_files if str(path).strip()]
    modules = modules_of(files)
    diff_lines = max(0, int(diff_added)) + max(0, int(diff_removed))
    hits = _keyword_hits(
        "\n".join([brief or "", checklist or "", " ".join(files)]), cfg.high_risk_keywords
    )

    reasons: list[str] = [f"改动文件 {len(files)} 个"]
    if diff_lines:
        reasons.append(f"diff {diff_lines} 行")
    if modules:
        reasons.append(f"跨 {len(modules)} 个模块：{'、'.join(modules[:3])}")
    if hits:
        reasons.append(f"命中高风险词：{'、'.join(hits[:3])}")

    # 规则优先级：complex 先于 simple——命中高风险词时再小也不能当简单任务
    if (
        len(files) >= cfg.complex_min_files
        or diff_lines > cfg.complex_min_diff_lines
        or len(modules) >= cfg.complex_min_modules
        or bool(hits)
    ):
        level = "complex"
    elif (
        len(files) <= cfg.simple_max_files
        and diff_lines <= cfg.simple_max_diff_lines
        and len(modules) <= 1
    ):
        level = "simple"
    else:
        level = "standard"

    # 0-1 参照分：只用于观测分布与后续调阈值，不参与判定
    score = (
        0.4 * min(len(files) / 10, 1.0)
        + 0.4 * min(diff_lines / max(1, cfg.complex_min_diff_lines), 1.0)
        + 0.2 * min(len(modules) / max(1, cfg.complex_min_modules), 1.0)
    )
    if hits:
        score = min(1.0, score + 0.3)
    return ComplexityReport(
        level=level,
        score=score,
        signals={
            "files": len(files),
            "diff_lines": diff_lines,
            "modules": modules,
            "high_risk_hits": hits,
            "limits": {
                "simple_max_files": cfg.simple_max_files,
                "simple_max_diff_lines": cfg.simple_max_diff_lines,
                "complex_min_files": cfg.complex_min_files,
                "complex_min_diff_lines": cfg.complex_min_diff_lines,
                "complex_min_modules": cfg.complex_min_modules,
            },
        },
        reasons=tuple(reasons),
    )


def estimate_from_scope(scope: Any, *, brief: str = "", checklist: str = "") -> ComplexityReport:
    """从 ``ReviewScope`` 判定（执行器与 CLI 执行器共用同一条口径）。"""
    return estimate_complexity(
        changed_files=list(getattr(scope, "changed_files", []) or []),
        diff_added=int(getattr(scope, "diff_added", 0) or 0),
        diff_removed=int(getattr(scope, "diff_removed", 0) or 0),
        brief=brief,
        checklist=checklist,
    )
