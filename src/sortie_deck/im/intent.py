"""IM 意图解析：把自然语言映射到命令。

**为什么需要这一层**：用户不会先记命令再说话。群里问一句「目前有什么任务」，
回「我只接受命令」是把产品缺陷甩给用户。

两层策略：

1. **规则优先**：高频说法用正则覆盖，零延迟、离线可用、结果可预期；
2. **LLM 兜底**：规则没命中且模型可用时，让模型输出 ``{command, args, reply}``。

**安全边界（重要）**：自然语言**不能**直接触发状态变更。``approve`` /
``reject`` / ``stop`` / ``confirm`` 这类动作，解析出来后只回一条「要这么做请发
/approve ini_xxx」的提示，不执行——否则群里任何人说一句「可以」「通过吧」就能
把门禁放过了。``new`` 是例外：它只建草稿、不启动，风险低，允许口语创建。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 只读命令：解析出来直接执行
READ_COMMANDS: frozenset[str] = frozenset(
    {"help", "list", "running", "status", "artifacts"}
)
# 允许自然语言触发的写命令：只建草稿、不启动，风险可接受
SOFT_WRITE_COMMANDS: frozenset[str] = frozenset({"new"})
# 必须显式输入命令的动作：自然语言只能提示，不能执行
CONFIRM_REQUIRED: frozenset[str] = frozenset({"confirm", "approve", "reject", "stop"})

ALL_COMMANDS = READ_COMMANDS | SOFT_WRITE_COMMANDS | CONFIRM_REQUIRED

# 规则顺序即优先级：「运行中的任务」要判成 running 而不是 list
_RULES: tuple[tuple[str, str], ...] = (
    (r"运行中|正在跑|在跑|执行中|running", "running"),
    (r"待审批|待处理|等着?批|等待(审核|审批)|pending", "running"),
    (r"产物|产出|交付物|artifact", "artifacts"),
    (r"进度|状态|到哪(了|一步)|status", "status"),
    (r"有哪些|有什么|多少个|多少|列表|列出|所有|最近.{0,4}任务|任务.{0,3}列表", "list"),
    (r"详情|详细|展开|说说|讲讲|讲讲|什么情况", "status"),
    (r"帮助|怎么用|能做什么|会什么|help|命令", "help"),
)
_COMPILED = tuple((re.compile(pattern, re.IGNORECASE), cmd) for pattern, cmd in _RULES)

_ID_RE = re.compile(r"ini_[0-9a-zA-Z]{4,}")
# 「帮我建个任务：做登录页」——把冒号/引号后的内容当需求
_NEW_RE = re.compile(
    r"(?:帮我|给我|请)?(?:新建|创建|建|开)(?:一个|个)?(?:任务|mission)[：:，,\s]*(.+)",
    re.IGNORECASE | re.DOTALL,
)

_CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_ORDINAL_RE = re.compile(r"第\s*([一二三四五六七八九十]|\d{1,2})\s*(?:个|条|项|只)?")
_LAST_RE = re.compile(r"(?:最后|末尾|最新)(?:一)?(?:个|条|项)")
_THIS_RE = re.compile(r"它|这个|那个|这条|那条|该任务|上面那个|刚才那个")
_DETAIL_WORDS = re.compile(r"详情|详细|看看|看一下|看下|状态|进度|什么|情况|在哪|到哪")
_ARTIFACT_WORDS = re.compile(r"产物|产出|交付物|artifact|文件")


@dataclass
class Reference:
    """「第一个」「最后一个」「它」—— 指向上一轮列表里的某个任务。"""

    index: int  # 0-based；-1 表示最后一个
    want: str  # status | artifacts


def parse_reference(text: str) -> Reference | None:
    """解析序数/指代。只解析位置，不解析成 ID —— ID 需要会话上下文。

    「它」这类代词必须同时出现查询词才认，否则「帮我把这个做了」会被
    误判成"查看详情"。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    want = "artifacts" if _ARTIFACT_WORDS.search(raw) else "status"

    ordinal = _ORDINAL_RE.search(raw)
    if ordinal:
        token = ordinal.group(1)
        index = int(token) if token.isdigit() else _CN_DIGITS.get(token, 1)
        return Reference(index=max(index - 1, 0), want=want)

    if _LAST_RE.search(raw):
        return Reference(index=-1, want=want)

    if _THIS_RE.search(raw) and (_DETAIL_WORDS.search(raw) or _ARTIFACT_WORDS.search(raw)):
        return Reference(index=0, want=want)
    return None


_SYSTEM = """你是 IM 助手的意图解析器。把用户的一句话映射成一个命令。

可用命令与参数：
- help                      查看帮助
- list                      我最近的任务
- running                   运行中 / 待审批的任务
- status [任务ID]            任务详情
- artifacts [任务ID]         产物列表
- new <一句话需求>           创建任务（只建草稿，不启动）
- confirm [任务ID]           启动草稿
- approve [任务ID] [备注]     放行门禁
- reject [任务ID] <原因>      打回
- stop [任务ID]              停止任务

输出严格 JSON：
{"command": "<命令名或空字符串>", "args": ["..."], "reply": "<无法映射时的中文说明>"}

规则：
- 只输出 JSON，不要代码块、不要解释。
- 任务 ID 只从用户原话里取形如 ini_xxx 的字符串，不要编造。
- 用户只是闲聊或问能力范围时，command 留空，reply 里用一句中文说明你能做什么。
- 不要把闲聊硬塞成某个命令。"""


@dataclass
class Intent:
    command: str = ""
    args: list[str] = field(default_factory=list)
    reply: str = ""


def _extract_id(text: str) -> list[str]:
    return _ID_RE.findall(text or "")


def rule_parse(text: str) -> Intent | None:
    """关键词规则。命中返回 Intent，否则 None。

    只做「意图归类 + 抽任务 ID」，不试图理解复杂句式——那是 LLM 的活。
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # 「帮我建个任务：做登录页」优先判成 new，否则会被下面的"任务"规则吃掉
    new_match = _NEW_RE.search(raw)
    if new_match:
        brief = new_match.group(1).strip().strip("：:，, ")
        if brief:
            return Intent(command="new", args=[brief])

    ids = _extract_id(raw)
    # 「看一下第一个任务详情」：位置指代优先于关键词归类，
    # 否则会被"任务"两个字吃掉、回成列表。
    if not ids:
        ref = parse_reference(raw)
        if ref is not None:
            return Intent(command=ref.want)

    for pattern, command in _COMPILED:
        if pattern.search(raw):
            if command in {"status", "artifacts"}:
                return Intent(command=command, args=ids[:1])
            return Intent(command=command)
    return None


def _from_json(data: Any, raw_text: str) -> Intent | None:
    if not isinstance(data, dict):
        return None
    command = str(data.get("command") or "").strip().lower()
    if command not in ALL_COMMANDS:
        reply = str(data.get("reply") or "").strip()
        return Intent(reply=reply) if reply else None
    args = data.get("args")
    if isinstance(args, str):
        args = [args]
    if not isinstance(args, list):
        args = []
    cleaned = [str(a).strip() for a in args if str(a).strip()]

    # 模型有时会编造任务 ID：只接受原话里出现过的，避免它指到别的任务上
    needs_id = command in {"status", "artifacts", "approve", "reject", "stop", "confirm"}
    spoken_ids = _extract_id(raw_text) if needs_id else []
    if command in {"status", "artifacts"} and spoken_ids:
        cleaned = [spoken_ids[0]] + [a for a in cleaned if not a.startswith("ini_")]
    elif cleaned and cleaned[0].startswith("ini_") and cleaned[0] not in spoken_ids:
        cleaned = cleaned[1:]
    return Intent(command=command, args=cleaned)


class IntentParser:
    """规则 + LLM 两层解析。无状态，可复用。"""

    def __init__(
        self,
        *,
        llm_factory: Callable[[], Any] | None = None,
        timeout: float = 8.0,
    ) -> None:
        self._llm_factory = llm_factory
        self.timeout = timeout

    def _llm(self) -> Any:
        if self._llm_factory is not None:
            return self._llm_factory()
        from sortie_deck.llm import LlmClient

        return LlmClient()

    async def understand(self, text: str) -> Intent | None:
        """先规则后模型。两者都没结果返回 None（调用方给兜底提示）。"""
        hit = rule_parse(text)
        if hit is not None:
            return hit

        try:
            llm = self._llm()
        except Exception as exc:  # noqa: BLE001 — 缺依赖/配置时降级
            logger.debug("IM 意图解析无 LLM 可用: %s", exc)
            return None
        if not getattr(llm, "enabled", False):
            return None

        try:
            # 限时必须短：IM 里等 30 秒才回一条"没听懂"比不回更糟
            data = await asyncio.wait_for(
                llm.chat_json(system=_SYSTEM, user=text, temperature=0.0),
                timeout=self.timeout,
            )
        except TimeoutError:
            logger.warning("IM 意图解析超时（%.0fs）", self.timeout)
            return None
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("IM 意图解析返回非 JSON: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 — 模型/网络故障不能打断 IM 通道
            logger.warning("IM 意图解析失败: %s", exc)
            return None
        return _from_json(data, text)


def describe_blocked(intent: Intent) -> str:
    """写操作被拦下时的提示：告诉他该发什么命令，而不是只说"不行"。"""
    cmd = intent.command
    args = " ".join(intent.args).strip()
    suffix = f" {args}" if args else ""
    labels = {
        "approve": "放行这个门禁",
        "reject": "打回",
        "stop": "停止任务",
        "confirm": "启动这个任务",
    }
    what = labels.get(cmd, cmd)
    return (
        f"要{what}的话，请直接发送命令：/{cmd}{suffix}\n"
        "（这类操作需要显式命令，避免群里一句「可以」就误放行）"
    )
