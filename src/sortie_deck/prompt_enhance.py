"""提示词增强：按场景用 LLM 优化用户输入，返回改写后的文本。

供前端「增强提示词」按钮调用（POST /api/prompts/enhance）。每个场景有独立的
system prompt，塑造不同的优化方向；用户输入直接覆盖为优化结果，因此要求
模型"只输出改写后的文本"，避免混入解释性内容。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

EnhanceScene = Literal[
    "briefing", "room", "rewrite", "forge", "memory", "persona", "persona_section"
]

# 输出约束：所有场景共用。强调保留原意、不编造事实、只输出正文。
_OUTPUT_RULES = (
    "输出要求：\n"
    "1. 只输出改写后的文本本身，不要任何解释、前缀、引号或 Markdown 代码块。\n"
    "2. 保留用户的原始意图与全部事实信息，不得编造不存在的需求或细节。\n"
    "3. 使用用户输入的主要语言改写。\n"
    "4. 长度与原文相当或略长，不做无关扩写。"
)

# 场景 → 优化方向。业务背景见各条注释。
_SCENE_PROMPTS: dict[str, str] = {
    # 建队规划官对话：用户在描述一个待建任务的需求，规划官会据此拆解流水线。
    # 优化目标是让需求更结构化、可规划。
    "briefing": (
        "你是任务需求优化助手。用户正在与建队规划官对话，输入是任务需求的粗糙描述。"
        "请把需求改写为结构清晰的任务描述：明确目标与期望结果、范围与非目标、"
        "关键约束（平台/技术栈/时间等，若原文提及）、验收标准（若可推断）。"
        "保持第一人称陈述式表达，不要生成清单以外的解释。"
    ),
    # 小队房间聊天：用户对 AI 角色小队发言（可能 @ 某个角色）。
    # 优化目标是表达更清楚，但必须保留聊天语气与 @提及 标记。
    "room": (
        "你是小队沟通助手。用户正在任务房间的讨论区对 AI 角色小队发言。"
        "请改写这段话，让诉求、问题或意见更清晰具体：明确想得到什么回答或行动，"
        "补充必要的背景。保持与原文一致的口语化语气，不要变成正式文档。"
        "原文中 @ 开头的提及标记必须原样保留。"
    ),
    # HITL 修订指令：用户在 /rewrite 后写的阶段产物修改意见，会注入阶段执行 prompt。
    # 优化目标是让修改指令可执行。
    "rewrite": (
        "你是产物修订指令优化助手。用户正在对某个阶段产物（如 PRD、设计稿）"
        "下达修改指令。请把指令改写为具体、可执行的修改说明：明确改什么、"
        "怎么改、边界与不变的部分（若原文提及）。"
        "原文中 / 开头的斜杠命令必须原样保留在开头。"
    ),
    # Agent Forge 角色生成：用户输入能力关键词，LLM 据此起草角色。
    # 优化目标是把短语扩写为清晰的能力/职责描述。
    "forge": (
        "你是 Agent 角色能力描述优化助手。用户在角色工坊输入了一个能力关键词或短语，"
        "系统将据此生成「10 年经验资深专家」角色。请把它扩写为清晰的能力描述："
        "擅长领域、能解决什么问题、期望的工作方式（若可推断）。保持简短（一两句话），"
        "不要生成完整角色设定。"
    ),
    # 记忆新建：用户写入团队记忆的约定/事实，简介与全文都会注入 Agent 上下文。
    # 优化目标是精炼为完整、可复用的陈述。
    "memory": (
        "你是团队记忆优化助手。用户正在写入一条团队记忆（约定/偏好/教训/事实）。"
        "请改写为完整、精炼、可复用的陈述句：去除口语与冗余，"
        "保留全部关键约束与条件，让未来的 Agent 一读即懂。不要添加原文没有的约定。"
    ),
    # 角色人设 / Agent 系统提示：用户编辑的 persona 将作为 LLM 的 system prompt。
    # 优化目标是把粗糙描述改写为六段式专业人设（角色/背景/技能/工作流/示例/约束）。
    "persona": (
        "你是 Agent 人设优化助手。用户正在编辑一个 AI 角色的人设（将作为系统提示注入模型）。"
        "请改写为标准六段式 Markdown，标题必须原样使用：\n"
        "## 角色\n## 背景\n## 技能\n## 工作流\n## 示例\n## 约束\n"
        "各段要求：\n"
        "- 角色：一句话，格式为「你是拥有 10 年经验的资深<领域>专家，擅长…」；\n"
        "- 背景：2-4 条，说明业务环境、上下游、产物用途与时间预算；\n"
        "- 技能：4-6 条「能力名：一句解释」，包含该职能需要的知识储备；\n"
        "- 工作流：3-5 步执行顺序，动词开头；\n"
        "- 示例：1-3 条，每条写「- 输入：」「- 期望输出：」；\n"
        "- 约束：4-6 条硬约束，写清禁止事项、边界与失败处理。\n"
        "使用第二人称（你）陈述，保留用户设定的一切细节，不要编造额外身份或能力。"
    ),
    # 分段编辑器的单段增强：只改写用户正在编辑的那一段，避免整篇重写冲掉其他段。
    "persona_section": (
        "你是 Agent 人设单段优化助手。用户只优化人设中的某一段，段名见上下文。"
        "请只输出该段正文：不要输出段落标题，不要输出其他段落，不要解释。\n"
        "格式要求：\n"
        "- 角色：一句话，格式为「你是拥有 10 年经验的资深<领域>专家，擅长…」；\n"
        "- 背景 / 技能 / 约束：每条一行，以「- 」开头；技能写成「能力名：一句解释」；\n"
        "- 工作流：每条一步，以「1. 」「2. 」开头；\n"
        "- 示例：每条写「- 输入：」「- 期望输出：」。\n"
        "保留用户原始意图与事实，不编造额外身份或能力。"
    ),
}


class EnhancePromptRequest(BaseModel):
    """提示词增强请求。

    text: 用户输入框的当前内容（优化对象）。
    context: 场景上下文片段（可选）。briefing 传当前会话 brief 摘要，
    room 传最近几条聊天，rewrite 传目标产物摘要；帮助模型贴合语境。
    """

    scene: EnhanceScene
    text: str = Field(min_length=1, max_length=20_000)
    context: str = Field(default="", max_length=8_000)


# 开头的斜杠命令或 @提及 标记。优化时剥离后只改写正文，避免模型破坏指令结构。
_LEADING_MARKER = re.compile(r"^\s*((?:/[A-Za-z_-]+)|(?:@[\w.-]+))(?:\s+((?:@[\w.-]+\s*)*))?")


def _split_markers(text: str) -> tuple[str, str]:
    """剥离输入开头的斜杠命令与连续 @提及，返回 (标记前缀, 待优化正文)。"""
    m = _LEADING_MARKER.match(text)
    if not m:
        return "", text
    prefix = m.group(0).rstrip()
    return prefix, text[len(prefix) :]


async def enhance_prompt(req: EnhancePromptRequest) -> str:
    """按场景调用 LLM 优化文本，返回可直接覆盖输入框的改写结果。

    标记前缀（/rewrite、@角色）原样保留；正文经场景 system prompt 改写。
    LLM 不可用或失败时抛出异常，由调用方转 HTTP 错误。
    """
    from sortie_deck.llm import LlmClient

    # deepseek 思考链偶发超过 30s（线上出现过 httpx.ReadTimeout），放宽到 60s
    llm = LlmClient(timeout=60.0)
    if not llm.enabled:
        raise RuntimeError("LLM_NOT_CONFIGURED")

    marker_prefix, body = _split_markers(req.text)
    if not body.strip():
        # 输入只有标记（如只打了 /rewrite），无正文可优化
        return req.text

    system = _SCENE_PROMPTS.get(req.scene, _SCENE_PROMPTS["briefing"]) + "\n\n" + _OUTPUT_RULES
    user_parts = []
    if req.context.strip():
        user_parts.append(f"上下文（仅供理解，不要复述）：\n{req.context.strip()[:4000]}")
    user_parts.append(f"用户输入：\n{body.strip()}")
    enhanced = (await llm.chat_text(system=system, user="\n\n".join(user_parts), temperature=0.4)).strip()
    # 模型偶发用引号或代码块包裹，剥掉常见包裹形式
    enhanced = enhanced.strip("`").strip('"').strip("'").strip()
    if not enhanced:
        return req.text
    return f"{marker_prefix} {enhanced}".strip() if marker_prefix else enhanced
