"""航线复杂度分档：把「任务类型 + 档位」翻译成固定的阶段集合。

为什么需要这个模块：planner（LLM）此前可以自由增删阶段，一个贪吃蛇小游戏被
展开成 15 个阶段（2 个技术方案 + 2 条 build→selftest 分支 + 联调/提测），实测
105 分钟。档位把「允许出现哪些阶段」变成显式输入，planner 只在预算内排序与删减。

两个正交维度：
- ``task_kind``: ``delivery``（开发交付）/ ``knowledge``（办公、研究、写作）
- ``tier``: ``express``（极速）/ ``standard``（标准）/ ``full``（完整）

约束：``enforce_tier`` 只做「减」和「折叠」，绝不新增提案里没有的阶段
（补齐 intake/done 除外）。裁剪明细写进 ``tier_pruned``，供前端解释「规划官
提案被收敛了哪些节点」。
"""

from __future__ import annotations

import re
from typing import Any, Literal

TaskKind = Literal["delivery", "knowledge"]
Tier = Literal["express", "standard", "full"]

TASK_KINDS: tuple[TaskKind, ...] = ("delivery", "knowledge")
TIERS: tuple[Tier, ...] = ("express", "standard", "full")

TIER_LABELS = {"express": "极速", "standard": "标准", "full": "完整"}
TIER_LABELS_EN = {"express": "Express", "standard": "Standard", "full": "Full"}
TASK_KIND_LABELS = {"delivery": "开发交付", "knowledge": "办公/研究"}

# 每档允许的阶段（有序）。express/standard 用它做「裁剪 + 重排」；
# full 档也用它做白名单，但保留 planner 的顺序与并行分组。
TIER_ALLOWED: dict[tuple[str, str], list[str]] = {
    ("delivery", "express"): [
        "intake",
        "eng_implement",
        "qa_verify",
        "deploy_preview",
        "done",
    ],
    ("delivery", "standard"): [
        "intake",
        "product_prd",
        "design_ui",
        "qa_cases",
        "eng_implement",
        "eng_selftest",
        "qa_verify",
        "deploy_preview",
        "done",
    ],
    ("delivery", "full"): [
        "intake",
        "product_prd",
        "design_ui",
        "qa_cases",
        "eng_ios_tech_design",
        "eng_android_tech_design",
        "eng_web_tech_design",
        "eng_backend_tech_design",
        "eng_agent_tech_design",
        "eng_implement",
        "eng_ios",
        "eng_android",
        "eng_web",
        "eng_backend",
        "eng_agent",
        "eng_ios_selftest",
        "eng_android_selftest",
        "eng_web_selftest",
        "eng_backend_selftest",
        "eng_agent_selftest",
        "eng_selftest",
        "eng_integrate",
        "qa_handoff",
        "qa_verify",
        "deploy_preview",
        "done",
    ],
    ("knowledge", "express"): ["intake", "office_produce", "done"],
    ("knowledge", "standard"): ["intake", "office_collect", "office_produce", "done"],
    ("knowledge", "full"): [
        "intake",
        "office_collect",
        "office_produce",
        "office_recommend",
        "done",
    ],
}

# 各档在一句话说明里展示的链路（与 ALLOWED 一致，单列出来便于前端/文档复用）
TIER_DESCRIPTIONS = {
    ("delivery", "express"): "单文件工具、小游戏、页面微调、Demo",
    ("delivery", "standard"): "单端 + 有验收口径的中型需求",
    ("delivery", "full"): "多端、跨模块、外部依赖",
    ("knowledge", "express"): "周报、邮件、单篇文案、简单整理",
    ("knowledge", "standard"): "会议纪要、资料汇总、带引用的短文",
    ("knowledge", "full"): "调研报告、方案对比、需要结论与行动建议",
}

TIER_ETA_MINUTES = {
    ("delivery", "express"): (5, 10),
    ("delivery", "standard"): (20, 35),
    ("delivery", "full"): (45, 90),
    ("knowledge", "express"): (3, 6),
    ("knowledge", "standard"): (8, 15),
    ("knowledge", "full"): (15, 30),
}

# 各档的角色上限（含 LLM 自建角色），避免极速档拉一堆角色把时间拖回去
TIER_MAX_ROLES = {"express": 2, "standard": 4, "full": 6}

# 核心产出阶段：提案裁完必须有它，否则回落到规则版航线
CORE_STAGE = {
    ("delivery", "express"): "eng_implement",
    ("delivery", "standard"): "eng_implement",
    ("delivery", "full"): "eng_implement",
    ("knowledge", "express"): "office_produce",
    ("knowledge", "standard"): "office_produce",
    ("knowledge", "full"): "office_produce",
}

_ENG_BUILD_ALIASES = frozenset(
    {"eng_implement", "eng_ios", "eng_android", "eng_web", "eng_backend", "eng_agent"}
)

# research_* 是研究轨的历史命名；LLM 若从 catalog 里提了它，折叠成 office_*。
_KNOWLEDGE_ALIASES = {
    "research_scan": "office_collect",
    "research_synthesize": "office_produce",
    "research_recommend": "office_recommend",
}

# --- 任务类型判定 -------------------------------------------------------------

_DELIVERY_SIGNALS = [
    r"小游戏",
    r"游戏",
    r"页面",
    r"网站",
    r"接口",
    r"\bapi\b",
    r"\bapp\b",
    r"小程序",
    r"脚本",
    r"插件",
    r"爬虫",
    r"部署",
    r"数据库",
    r"前端",
    r"后端",
    r"\bbug\b",
    r"修复",
    r"重构",
    r"代码",
    r"程序",
]

_KNOWLEDGE_SIGNALS = [
    r"周报",
    r"日报",
    r"月报",
    r"纪要",
    r"会议",
    r"调研",
    r"报告",
    r"文案",
    r"邮件",
    r"总结",
    r"复盘",
    r"汇报",
    r"\bppt\b",
    r"幻灯片",
    r"表格",
    r"\bexcel\b",
    r"简历",
    r"翻译",
    r"整理",
    r"写作",
    r"提纲",
]

# --- 档位推荐 ---------------------------------------------------------------

# 命中即至少 standard：有持久化/排行榜/多页面/外部依赖，极速档撑不住
_DELIVERY_STANDARD_SIGNALS = [
    r"排行榜",
    r"持久化",
    r"数据库",
    r"存储",
    r"多页面",
    r"后台管理",
    r"导入导出",
    r"第三方",
    r"支付",
]

# 命中即 full：多端协同或架构级改动
_DELIVERY_FULL_SIGNALS = [
    r"多端",
    r"联调",
    r"微服务",
    r"架构",
    r"迁移",
    r"重构",
    r"权限体系",
    r"高并发",
    r"审计",
    r"多角色",
]

_KNOWLEDGE_STANDARD_SIGNALS = [
    r"调研",
    r"整理",
    r"汇总",
    r"纪要",
    r"对比",
    r"分析",
    r"多份",
]

_KNOWLEDGE_FULL_SIGNALS = [
    r"调研报告",
    r"方案对比",
    r"行动建议",
    r"结论",
    r"来源",
    r"引用",
]


def _hit(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def resolve_task_kind(
    title: str = "", brief: str = "", *, has_repos: bool = False, requested: str = "auto"
) -> tuple[TaskKind, str]:
    """判定任务类型，返回 ``(task_kind, 理由)``。

    规则优先于模型：办公/研究类信号命中且没有工程信号 → knowledge；其余
    delivery。绑定 git 仓库的任务一律按 delivery 处理（办公任务不 clone 仓库）。
    """
    if requested in TASK_KINDS:
        return requested, f"用户指定 task_kind={requested}"  # type: ignore[return-value]

    blob = f"{title}\n{brief}".lower()
    if has_repos:
        return "delivery", "绑定了 git 仓库 → delivery"
    if not blob.strip():
        return "delivery", "空简报 → delivery（保持旧行为）"

    knowledge_hit = _hit(_KNOWLEDGE_SIGNALS, blob)
    delivery_hit = _hit(_DELIVERY_SIGNALS, blob)
    if knowledge_hit and not delivery_hit:
        return "knowledge", "命中办公/研究信号且无工程信号 → knowledge"
    if delivery_hit:
        return "delivery", "命中工程信号 → delivery"
    return "delivery", "默认 delivery"


def resolve_tier(
    task_kind: str, title: str = "", brief: str = "", *, requested: str = "auto"
) -> tuple[Tier, str]:
    """按任务类型推荐档位；``requested`` 显式指定时直接采用。

    判定保守：只有明确没有持久化/多端/外部依赖的短需求才降到极速档，
    宁可多一个阶段，也不要交付不足再返工。
    """
    if requested in TIERS:
        return requested, f"用户指定 tier={requested}"  # type: ignore[return-value]

    blob = f"{title}\n{brief}".lower()
    if task_kind == "knowledge":
        if _hit(_KNOWLEDGE_FULL_SIGNALS, blob):
            return "full", "命中调研报告/方案对比/结论建议 → full"
        if _hit(_KNOWLEDGE_STANDARD_SIGNALS, blob):
            return "standard", "命中调研/整理/汇总/对比 → standard"
        return "express", "单篇产出类需求 → express"

    if _hit(_DELIVERY_FULL_SIGNALS, blob):
        return "full", "命中多端/联调/架构改造 → full"
    if _hit(_DELIVERY_STANDARD_SIGNALS, blob):
        return "standard", "命中持久化/多页面/第三方依赖 → standard"
    if len(re.sub(r"\s+", "", blob)) <= 120:
        return "express", "短简报且无复杂信号 → express"
    return "standard", "简报较长 → standard"


# --- 档位元数据（给 API / 前端） ---------------------------------------------


def tier_definition(
    task_kind: str, tier: str, catalog: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """单档的展示定义：阶段链路 + 说明 + 预估时长。"""
    allowed = TIER_ALLOWED.get((task_kind, tier)) or []
    stages = []
    for sid in allowed:
        spec = (catalog or {}).get(sid) or {}
        label = spec.get("label") or sid
        stages.append(
            {
                "id": sid,
                "label": label,
                "label_en": spec.get("label_en") or label,
            }
        )
    eta = TIER_ETA_MINUTES.get((task_kind, tier)) or (0, 0)
    return {
        "tier": tier,
        "label": TIER_LABELS.get(tier, tier),
        "label_en": TIER_LABELS_EN.get(tier, tier),
        "description": TIER_DESCRIPTIONS.get((task_kind, tier), ""),
        "stages": stages,
        "stage_count": len(stages),
        "eta_minutes": [eta[0], eta[1]],
        "max_roles": TIER_MAX_ROLES.get(tier, 0),
    }


def list_tier_definitions(
    task_kind: str | None = None, catalog: dict[str, dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """返回两套（或指定类型的）三档定义，供建任务页渲染。"""
    kinds = [task_kind] if task_kind in TASK_KINDS else list(TASK_KINDS)
    out: list[dict[str, Any]] = []
    for kind in kinds:
        out.append(
            {
                "task_kind": kind,
                "label": TASK_KIND_LABELS.get(kind, kind),
                "tiers": [tier_definition(kind, t, catalog) for t in TIERS],
            }
        )
    return out


# --- 裁剪 -------------------------------------------------------------------


def _merge_stage(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """把被折叠掉的阶段信息并入保留阶段（依赖、门禁取并集）。"""
    deps = list(dict.fromkeys([*(dst.get("depends_on") or []), *(src.get("depends_on") or [])]))
    if deps:
        dst["depends_on"] = deps
    dst["hitl_after"] = bool(dst.get("hitl_after") or src.get("hitl_after"))


def _map_stage(sid: str, task_kind: str, allowed: set[str]) -> str | None:
    """把提案阶段映射到档位允许的阶段；返回 None 表示丢弃。"""
    if sid in allowed:
        return sid
    if task_kind == "knowledge":
        mapped = _KNOWLEDGE_ALIASES.get(sid)
        return mapped if mapped in allowed else None
    if task_kind != "delivery":
        return None
    # 细分工种在极速/标准档折叠成单开发阶段
    if sid in _ENG_BUILD_ALIASES and "eng_implement" in allowed:
        return "eng_implement"
    # 分工种自测在标准档折叠成通用自测
    if sid.endswith("_selftest") and "eng_selftest" in allowed:
        return "eng_selftest"
    return None


def enforce_tier(
    proposal: dict[str, Any],
    *,
    task_kind: str,
    tier: str,
    catalog: dict[str, dict[str, Any]] | None = None,
    allowed_override: list[str] | None = None,
) -> dict[str, Any] | None:
    """把 planner 提案收敛到档位允许集合内；无法满足核心产出时返回 None。

    只减不加：结果里的阶段一定是提案里出现过的（或由细分阶段折叠而来），
    不会凭空补出提案没提的阶段（intake/done 由上游 normalize 保证）。

    ``allowed_override`` 用于调用方显式指定航线（旧 ``pipeline_track`` 入口）：
    此时以那条航线的阶段集合为预算，避免档位推荐覆盖用户选择。
    """
    allowed_list = list(allowed_override or TIER_ALLOWED.get((task_kind, tier)) or [])
    if not allowed_list:
        return None
    allowed = set(allowed_list)

    kept: dict[str, dict[str, Any]] = {}
    pruned: list[dict[str, Any]] = []
    for raw in proposal.get("stages") or []:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("id") or raw.get("name") or "").strip()
        if not sid:
            continue
        mapped = _map_stage(sid, task_kind, allowed)
        if mapped is None:
            pruned.append(
                {
                    "id": sid,
                    "reason": "tech_design" if sid.endswith("_tech_design") else "not_in_tier",
                }
            )
            continue
        if mapped != sid:
            pruned.append({"id": sid, "mapped_to": mapped, "reason": "collapsed"})
        if mapped in kept:
            _merge_stage(kept[mapped], raw)
            continue
        spec = dict(raw)
        spec["id"] = mapped
        spec["name"] = mapped
        if mapped != sid:
            # 折叠后的阶段是单节点：不能再带原分支的并行属性，否则图里会出现
            # 只有一个成员的 parallel wave
            spec.pop("parallel_group", None)
            spec.pop("branch", None)
        # 折叠后保留档位目录里的角色与产物契约（raw 里通常是细分阶段的定义）
        base = (catalog or {}).get(mapped) or {}
        for key in ("label", "label_en", "role", "output_contract"):
            if base.get(key):
                spec[key] = base[key]
        kept[mapped] = spec

    core = CORE_STAGE.get((task_kind, tier))
    if core and core not in kept:
        return None

    stages = [kept[sid] for sid in allowed_list if sid in kept]
    if len(stages) < 2:
        return None

    # 依赖清理：被裁掉的阶段不能继续挂在 depends_on 里，否则图构建时找不到节点
    kept_ids = {s["id"] for s in stages}
    for spec in stages:
        deps = [d for d in (spec.get("depends_on") or []) if d in kept_ids]
        if deps:
            spec["depends_on"] = deps
        else:
            spec.pop("depends_on", None)

    notes = list(proposal.get("notes") or [])
    if pruned:
        notes.append(f"tier={task_kind}/{tier}: 收敛 {len(pruned)} 个节点")
    return {
        **proposal,
        "stages": stages,
        "task_kind": task_kind,
        "tier": tier,
        "tier_pruned": pruned,
        "notes": notes,
    }
