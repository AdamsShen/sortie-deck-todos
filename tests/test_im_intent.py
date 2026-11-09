"""自然语言意图解析：规则层、LLM 兜底层、以及"写操作必须显式命令"的边界。"""

from __future__ import annotations

import asyncio

from sortie_deck.im.intent import (
    Intent,
    IntentParser,
    Reference,
    describe_blocked,
    parse_reference,
    rule_parse,
)


class FakeLlm:
    def __init__(
        self,
        data=None,
        *,
        enabled: bool = True,
        delay: float = 0.0,
        exc: Exception | None = None,
    ) -> None:
        self._data = data
        self._enabled = enabled
        self._delay = delay
        self._exc = exc
        self.calls: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def chat_json(self, *, system: str, user: str, temperature: float = 0.0):
        self.calls.append(user)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return self._data


def parser(llm=None, *, timeout: float = 8.0) -> IntentParser:
    return IntentParser(llm_factory=lambda: llm, timeout=timeout)


# --- 规则层 -------------------------------------------------------------


def test_rule_asks_what_missions_exist():
    """用户原话就是这句：不能回"我只接受命令"。"""
    hit = rule_parse("目前有什么任务")
    assert hit is not None
    assert hit.command == "list"


def test_rule_running_wins_over_list():
    """「有哪些运行中的任务」要判 running——"任务"这个更宽的词不能抢先。"""
    hit = rule_parse("有哪些运行中的任务")
    assert hit.command == "running"


def test_rule_pending_hitl():
    assert rule_parse("有什么待审批的吗").command == "running"


def test_rule_new_with_brief():
    hit = rule_parse("帮我建个任务：做一个用户查询接口")
    assert hit.command == "new"
    assert "用户查询接口" in hit.args[0]


def test_rule_status_extracts_mission_id():
    hit = rule_parse("看下 ini_abc12345 的进度")
    assert hit.command == "status"
    assert hit.args == ["ini_abc12345"]


def test_rule_help():
    assert rule_parse("你能做什么").command == "help"


def test_rule_miss_returns_none():
    """纯闲聊不该被硬塞成命令。"""
    assert rule_parse("今天天气不错") is None
    assert rule_parse("") is None


# --- LLM 层 -------------------------------------------------------------


async def test_llm_used_when_rules_miss():
    llm = FakeLlm({"command": "list", "args": [], "reply": ""})
    intent = await parser(llm).understand("手里攒了几件事想看看")
    assert intent.command == "list"
    assert llm.calls == ["手里攒了几件事想看看"]


async def test_rules_win_without_calling_llm():
    """规则命中就不该再花钱调模型。"""
    llm = FakeLlm({"command": "help", "args": [], "reply": ""})
    await parser(llm).understand("目前有什么任务")
    assert llm.calls == []


async def test_llm_disabled_falls_through():
    intent = await parser(FakeLlm(None, enabled=False)).understand("随便说点什么")
    assert intent is None


async def test_llm_timeout_is_bounded():
    """IM 里等 30 秒才回"没听懂"比不回更糟。"""
    intent = await parser(FakeLlm({"command": "list"}, delay=1.0), timeout=0.05).understand(
        "随便说点什么"
    )
    assert intent is None


async def test_llm_exception_degrades():
    intent = await parser(FakeLlm(exc=RuntimeError("boom"))).understand("随便说点什么")
    assert intent is None


async def test_llm_invalid_json_degrades():
    intent = await parser(FakeLlm({"nonsense": 1})).understand("随便说点什么")
    assert intent is None


async def test_llm_chitchat_reply_surfaces():
    llm = FakeLlm({"command": "", "args": [], "reply": "我可以帮你查任务、批门禁。"})
    intent = await parser(llm).understand("你是谁")
    assert intent.command == ""
    assert "查任务" in intent.reply


async def test_llm_fabricated_mission_id_is_dropped():
    """模型爱编 ID：原话里没有的 ini_xxx 必须丢掉，否则会指到别人的任务。"""
    llm = FakeLlm({"command": "approve", "args": ["ini_fake999"], "reply": ""})
    intent = await parser(llm).understand("把那个批了吧")
    assert intent.command == "approve"
    assert intent.args == []


async def test_llm_keeps_mission_id_spoken_by_user():
    llm = FakeLlm({"command": "approve", "args": ["ini_real123"], "reply": ""})
    intent = await parser(llm).understand("通过 ini_real123")
    assert intent.args == ["ini_real123"]


async def test_llm_unknown_command_ignored():
    llm = FakeLlm({"command": "rm -rf", "args": [], "reply": ""})
    assert await parser(llm).understand("随便") is None


# --- 安全边界 -----------------------------------------------------------


def test_blocked_hint_tells_what_to_send():
    """写操作被拦时要告诉他发什么命令，而不是只说"不行"。"""
    text = describe_blocked(Intent(command="approve", args=["ini_x"]))
    assert "/approve ini_x" in text
    assert "显式命令" in text


def test_confirm_list_covers_state_changes():
    from sortie_deck.im.intent import CONFIRM_REQUIRED

    assert {"approve", "reject", "stop", "confirm"} == set(CONFIRM_REQUIRED)
    # new 只建草稿、不启动，允许口语触发
    assert "new" not in CONFIRM_REQUIRED


# --- 序数 / 指代 ---------------------------------------------------------


def test_ordinal_detail_parsed_as_status():
    """用户原话：这句曾被"任务"两个字吃掉，回成了列表。"""
    hit = rule_parse("看一下第一个任务详情")
    assert hit.command == "status"
    assert hit.args == []
    assert parse_reference("看一下第一个任务详情") == Reference(0, "status")


def test_ordinal_second():
    assert parse_reference("第二个呢") == Reference(1, "status")
    assert rule_parse("第二个呢").command == "status"


def test_ordinal_last():
    assert parse_reference("最后一个的产物") == Reference(-1, "artifacts")
    assert rule_parse("最后一个的产物").command == "artifacts"


def test_chinese_ordinal_ten():
    assert parse_reference("第十个").index == 9


def test_pronoun_requires_query_word():
    """「帮我把这个做了」是动作，不是查详情——代词必须配查询词才认。"""
    assert parse_reference("帮我把这个做了") is None
    assert parse_reference("它的产物呢") == Reference(0, "artifacts")


def test_explicit_id_beats_ordinal():
    """同时给了 ID 和「第一个」，以 ID 为准。"""
    hit = rule_parse("看一下第一个任务详情 ini_abc12345")
    assert hit.args == ["ini_abc12345"]


def test_list_still_needs_enumeration_word():
    """单纯出现"任务"不再等于 /list，否则任何含"任务"的句子都被吞。"""
    assert rule_parse("目前有什么任务").command == "list"
    assert rule_parse("任务") is None
