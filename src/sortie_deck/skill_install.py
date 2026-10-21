"""技能获取与优化：外网 GitHub 安装、市场精选、skill-install 优化、双版本草稿。

V4 工具箱的后端支撑：
- install_from_github: 从 GitHub 仓库拉取 SKILL.md 生成技能条目（仅支持仓库地址，不支持 MD 直链）
- MARKET_SKILLS: 内置市场精选（首版静态目录，接口形状预留后续接外部市场 API）
- optimize_skill_md: skill-install 优化——LLM 可用走 LLM，否则规则模板兜底
- SkillDraftStore: 双版本草稿（原版 + 优化版）落盘 data/skill_drafts.json，确认选版后整份删除，
  即未选版本从服务器本地存储清除
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from sortie_deck.toolkit import CreateToolkitRequest, parse_skill_markdown

logger = logging.getLogger(__name__)

_GITHUB_REPO_RE = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/?$")

# 市场精选：静态内置目录，installed 状态由 /api/market/skills 按 store 实时计算
MARKET_SKILLS: list[dict[str, str]] = [
    {
        "key": "data-viz",
        "name": "data-viz",
        "summary": "将结构化数据渲染为图表与简报，输出 PNG 与结论摘要",
        "src": "市场",
        "category": "qa",
        "tags": "charts,report",
        "body": "---\nname: data-viz\ndescription: 将结构化数据渲染为图表与简报\n---\n## 用途\n输入表格数据（CSV/JSON），产出趋势图、分布图与一段结论。\n## 步骤\n1. 确认数据字段与统计口径\n2. 选择图表类型并生成\n3. 输出 PNG 与解读文字\n",
    },
    {
        "key": "api-tester",
        "name": "api-tester",
        "summary": "从 OpenAPI 规范生成接口冒烟用例与断言",
        "src": "市场",
        "category": "qa",
        "tags": "api,test",
        "body": "---\nname: api-tester\ndescription: 依据 OpenAPI 生成接口冒烟用例\n---\n## 用途\n读取 OpenAPI 文档，为每个端点生成冒烟用例与断言。\n## 步骤\n1. 解析 OpenAPI 端点清单\n2. 按方法与参数生成用例\n3. 执行并输出通过率报告\n",
    },
    {
        "key": "doc-summarizer",
        "name": "doc-summarizer",
        "summary": "长文档分节摘要，输出引用定位",
        "src": "GitHub",
        "category": "knowledge",
        "tags": "summary,doc",
        "body": "---\nname: doc-summarizer\ndescription: 长文档分节摘要并保留引用定位\n---\n## 用途\n对长文档分节摘要，每条结论附原文定位。\n## 步骤\n1. 按标题切分章节\n2. 逐节摘要\n3. 汇总为带引用的摘要报告\n",
    },
]


def parse_github_repo(url: str) -> tuple[str, str]:
    """校验并解析 GitHub 仓库地址，返回 (org, repo)。仅接受仓库 URL，拒绝 MD 直链等。"""
    m = _GITHUB_REPO_RE.match((url or "").strip())
    if not m:
        raise ValueError("仅支持 GitHub 仓库地址，如 https://github.com/org/skill-repo")
    return m.group(1), m.group(2).removesuffix(".git")


async def install_from_github(url: str) -> CreateToolkitRequest:
    """从 GitHub 仓库拉取 SKILL.md 生成技能条目。

    依次尝试 main / master 分支的 raw 地址；仓库不存在或无 SKILL.md 时抛 RuntimeError。
    """
    import httpx

    org, repo = parse_github_repo(url)
    last_err = ""
    for branch in ("main", "master"):
        raw_url = f"https://raw.githubusercontent.com/{org}/{repo}/{branch}/SKILL.md"
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(raw_url)
            if resp.status_code == 200:
                req = parse_skill_markdown(resp.text, name_hint=repo)
                req.source = f"github:{org}/{repo}"
                req.tags = list(dict.fromkeys([*(req.tags or []), "github"]))
                return req
            last_err = f"HTTP {resp.status_code} @ {branch}"
        except httpx.HTTPError as exc:
            last_err = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(f"拉取 {org}/{repo} 的 SKILL.md 失败（{last_err}）")


def market_request(key: str) -> CreateToolkitRequest:
    """市场精选条目 → 技能创建请求；key 不存在抛 KeyError。"""
    for entry in MARKET_SKILLS:
        if entry["key"] == key:
            return CreateToolkitRequest(
                kind="skill",
                name=entry["name"],
                summary=entry["summary"],
                body=entry["body"],
                category=entry["category"],
                tags=[t for t in entry["tags"].split(",") if t],
                source=f"market:{entry['src']}",
                load="defer",
            )
    raise KeyError(key)


def rule_optimize(name: str, body: str) -> str:
    """规则模板优化（LLM 不可用时的兜底）：保留原正文，补齐结构化段落。"""
    return (
        f"---\nname: {name}\n"
        f"description: {name} · skill-install 优化版：补齐用途、步骤与边界说明，便于 tool_search 检索\n"
        f"---\n\n"
        f"## 用途\n- 依据正文推断核心能力与适用场景，供 tool_search 检索匹配\n\n"
        f"## 使用步骤\n1. 输入准备：明确任务目标与所需材料\n"
        f"2. 执行：按下方原始正文指引操作\n3. 产出校验：确认输出符合预期后再交付\n\n"
        f"## 注意事项\n- 涉及写操作时先向用户确认\n- 超出能力范围时明确告知，不猜测执行\n\n"
        f"## 原始正文\n{body}"
    )


_OPTIMIZE_SYSTEM = (
    "你是 skill-install 优化器。把用户给出的 SKILL.md 草稿优化完善："
    "补齐 YAML frontmatter（name/description）、用途、使用步骤、注意事项；"
    "保留原稿的全部有效信息，不要编造原稿没有的能力。只输出优化后的完整 Markdown。"
)


async def optimize_skill_md(name: str, body: str) -> tuple[str, str]:
    """skill-install 优化入口。返回 (优化后正文, backend)。

    LlmClient 可用（配置了 API key 或本机 claude CLI）时走 LLM；
    失败或不可用时回退规则模板，保证功能永远可用。
    """
    from sortie_deck.llm import LlmClient

    client = LlmClient()
    if client.enabled:
        try:
            optimized = await client.chat_text(system=_OPTIMIZE_SYSTEM, user=f"# 技能名：{name}\n\n{body}")
            if optimized.strip():
                return optimized.strip(), client.backend
        except Exception as exc:  # noqa: BLE001 — LLM 失败回退规则优化
            logger.warning("skill-install LLM optimize failed, fallback to rule: %s", exc)
    return rule_optimize(name, body), "rule"


class SkillDraft(BaseModel):
    draft_id: str = Field(default_factory=lambda: f"sd_{uuid.uuid4().hex[:12]}")
    name: str
    category: str = ""
    original: str
    optimized: str
    backend: str = "rule"
    created_at: str = ""


class SkillDraftStore:
    """双版本草稿存储：确认选版后整份删除（未选版本随之从服务器本地存储清除）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, drafts: list[dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(drafts, ensure_ascii=False, indent=2), encoding="utf-8")

    def save(self, draft: SkillDraft) -> SkillDraft:
        with self._lock:
            drafts = self._read()
            drafts.append(draft.model_dump(mode="json"))
            self._write(drafts)
        return draft

    def get(self, draft_id: str) -> SkillDraft:
        for d in self._read():
            if d.get("draft_id") == draft_id:
                return SkillDraft.model_validate(d)
        raise KeyError(draft_id)

    def delete(self, draft_id: str) -> None:
        with self._lock:
            drafts = [d for d in self._read() if d.get("draft_id") != draft_id]
            self._write(drafts)
