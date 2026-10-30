"""Mission briefing harness — multi-turn chat to plan squad + pipeline.

Replaces the complex create-task form with:
  user chat → waterfall (history / KB / pipeline / loadout / roles) → confirm → create
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, PrivateAttr

from sortie_deck import mission_input
from sortie_deck.agent_forge import AgentForge, ForgeRequest
from sortie_deck.agents import AgentCatalog, CreateAgentRequest
from sortie_deck.harness import (
    EventKind,
    Harness,
    HarnessEvent,
    HarnessState,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    new_session_id,
)
from sortie_deck.harness.waterfall import run_waterfall
from sortie_deck.knowledge_gateway import KnowledgeGateway, gateway_from_settings
from sortie_deck.layout import ensure_mission_dirs, mission_chat_dir
from sortie_deck.models import (
    CreateInitiativeRequest,
    ExternalMaterial,
    Initiative,
    RepoConfig,
    new_id,
    utc_now,
)
from sortie_deck.pipeline import apply_proposal_to_meta
from sortie_deck.settings import Settings
from sortie_deck.settings import settings as default_settings
from sortie_deck.templates import load_persona

if TYPE_CHECKING:  # 仅类型标注用，避免顶层导入 llm 造成循环依赖
    from sortie_deck.llm import LlmClient

logger = logging.getLogger(__name__)

# create_initiative 需要接受预生成的任务 id（见 PlannerSessionStore 说明）
CreateFn = Callable[..., Awaitable[Initiative]]
ListFn = Callable[[], list[Any]]
SaveFn = Callable[[Initiative], Awaitable[Any] | Any]


class BriefingAttachment(BaseModel):
    name: str
    mime: str = "application/octet-stream"
    size: int = 0
    kind: str = "file"  # image | file
    data_url: str = ""
    text_excerpt: str = ""


class BriefingMessage(BaseModel):
    role: str
    text: str
    attachments: list[BriefingAttachment] = Field(default_factory=list)
    at: str = Field(default_factory=lambda: utc_now().isoformat())


class BriefingSession(BaseModel):
    id: str = Field(default_factory=new_session_id)
    status: str = "planning"
    title: str = ""
    brief: str = ""
    coding_executor: str = "auto"
    messages: list[BriefingMessage] = Field(default_factory=list)
    events: list[HarnessEvent] = Field(default_factory=list)
    draft: dict[str, Any] = Field(default_factory=dict)
    # External materials imported on the intake page; every handle() refreshes
    # them from the client's material panel snapshot.
    materials: list[ExternalMaterial] = Field(default_factory=list)
    initiative_id: str | None = None
    # 会话发起人用户 id：确认建队时写入任务的 created_by，
    # 否则简报创建的任务无归属，会落进"全员可见"的旧数据兜底口径
    owner_id: str = ""
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())
    # 建队完成时间：区分「会话还开着」与「已交付成任务」，供清理/审计判断
    committed_at: str = ""
    # 已落盘边界：save() 只追加这之后的新消息/事件（append-only 语义）。
    # 用私有属性而非字段，避免被 model_dump 写进 state 文件。
    _persisted_messages: int = PrivateAttr(default=0)
    _persisted_events: int = PrivateAttr(default=0)


_MAX_ATTACH = 6
_MAX_ATTACH_BYTES = 4 * 1024 * 1024
_MAX_DATA_URL_STORE = 1_500_000  # keep chat previewable without huge JSON


def _sanitize_attachments(raw: list[BriefingAttachment] | None) -> list[BriefingAttachment]:
    out: list[BriefingAttachment] = []
    for att in (raw or [])[:_MAX_ATTACH]:
        size = int(att.size or 0)
        data_url = att.data_url or ""
        if size > _MAX_ATTACH_BYTES or len(data_url) > _MAX_DATA_URL_STORE * 2:
            # drop oversized payload; keep metadata for planning context
            data_url = ""
        kind = att.kind if att.kind in {"image", "file"} else (
            "image" if (att.mime or "").startswith("image/") else "file"
        )
        out.append(
            BriefingAttachment(
                name=(att.name or "file")[:180],
                mime=(att.mime or "application/octet-stream")[:120],
                size=size,
                kind=kind,
                data_url=data_url[:_MAX_DATA_URL_STORE] if kind == "image" else "",
                text_excerpt=(att.text_excerpt or "")[:24_000],
            )
        )
    return out


def _compose_brief(text: str, attachments: list[BriefingAttachment]) -> str:
    parts: list[str] = []
    if text.strip():
        parts.append(text.strip())
    if attachments:
        parts.append("【附件】")
        for att in attachments:
            parts.append(f"- {att.name} ({att.kind}, {att.mime}, {att.size} bytes)")
            if att.text_excerpt.strip():
                parts.append(f"```\n{att.text_excerpt.strip()[:8000]}\n```")
            elif att.kind == "image":
                parts.append("  (用户上传图片，请结合任务描述理解需求)")
    return "\n".join(parts).strip()


# Per-material excerpt cap folded into the planning-discussion context (chars).
_MAX_MATERIAL_DISCUSSION_CHARS = 6000


def _materials_context(mats: list[ExternalMaterial]) -> str:
    """Planning-discussion context from imported materials.

    All materials carrying a text excerpt (pinned or not) join the discussion
    while the mission is being planned. Only pinned ones are later folded into
    the mission brief as persistent context (see materials.compose_brief).
    """
    parts: list[str] = []
    for m in mats:
        excerpt = (m.text_excerpt or "").strip()
        if not excerpt:
            continue
        tag = "持续上下文" if m.pinned else "讨论参考"
        parts.append(f"- {m.name}（{m.kind} · {tag}）")
        parts.append(f"```\n{excerpt[:_MAX_MATERIAL_DISCUSSION_CHARS]}\n```")
    if not parts:
        return ""
    return "【外部资料 · 规划讨论】\n" + "\n".join(parts)


class PlannerSessionStore:
    """规划会话存储：一个任务一个目录（``missions/<id>/chat/``）。

    与原 ``BriefingStore`` 的差异（这是本次改造的核心）：

    - 不再读写全局 ``data/briefings.json`` —— 单文件全量读改写，多用户并发建任务
      会互相覆盖，且会话数与文件体积线性膨胀；
    - 消息与事件 append-only 追加到 JSONL，只有小状态写 ``planner.state.json``；
    - 状态写入走 tmp→``os.replace`` 原子替换，读侧永远看到完整 JSON；
    - 任务 id（``ini_`` 前缀）在会话开启时预生成，建队确认复用同一 id，
      因此「会话—输入资料—任务产物」从头到尾在同一个目录里，零迁移。

    会话期任务尚未写入 initiatives 表，鉴权只能依赖 state 里的 ``owner_id``
    （见 API 层 ``_guard_briefing_owner``）。
    """

    STATE_NAME = "planner.state.json"
    MESSAGES_NAME = "planner.jsonl"
    EVENTS_NAME = "planner.events.jsonl"

    def __init__(self, settings: Settings) -> None:
        self.cfg = settings
        self.data_dir = Path(settings.data_dir)

    # --- paths ---------------------------------------------------------------

    def _chat_dir(self, mission_id: str) -> Path:
        return mission_chat_dir(self.data_dir, mission_id)

    def state_path(self, mission_id: str) -> Path:
        return self._chat_dir(mission_id) / self.STATE_NAME

    # --- crud ----------------------------------------------------------------

    def create(self, owner_id: str = "") -> BriefingSession:
        """预生成任务 id 并建好任务目录，返回空白会话。"""
        mission_id = new_id("ini_")
        ensure_mission_dirs(self.data_dir, mission_id)
        sess = BriefingSession(id=mission_id, owner_id=owner_id)
        return self.save(sess)

    def get(self, mission_id: str) -> BriefingSession:
        path = self.state_path(mission_id)
        if not path.exists():
            raise KeyError(mission_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            # 状态文件损坏按「会话不存在」处理：调用方拿不到半截状态更安全
            logger.warning("planner state 读取失败: %s: %s", path, exc)
            raise KeyError(mission_id) from exc
        sess = BriefingSession.model_validate(data)
        sess.messages = self._read_rows(mission_id, self.MESSAGES_NAME, BriefingMessage)
        sess.events = self._read_rows(mission_id, self.EVENTS_NAME, HarnessEvent)
        # 资料以 input/manifest.json 为唯一事实源，state 里不存副本
        sess.materials = mission_input.list_materials(self.data_dir, mission_id)
        sess._persisted_messages = len(sess.messages)
        sess._persisted_events = len(sess.events)
        return sess

    def save(self, sess: BriefingSession) -> BriefingSession:
        """写状态 + 追加尚未落盘的消息/事件（加锁，避免并发请求交错写）。"""
        sess.updated_at = utc_now().isoformat()
        lock_path = self.state_path(sess.id).with_suffix(".lock")
        with mission_input.exclusive_lock(lock_path):
            self._chat_dir(sess.id).mkdir(parents=True, exist_ok=True)
            state = sess.model_dump(mode="json", exclude={"messages", "events"})
            state.pop("materials", None)
            # 显式写 mission_id：目录名与状态文件自证归属，便于脱离目录扫库
            state["mission_id"] = sess.id
            _atomic_write_json(self.state_path(sess.id), state)
            self._append_rows(sess.id, self.MESSAGES_NAME, sess.messages[sess._persisted_messages :])
            sess._persisted_messages = len(sess.messages)
            self._append_rows(sess.id, self.EVENTS_NAME, sess.events[sess._persisted_events :])
            sess._persisted_events = len(sess.events)
        return sess

    def delete(self, mission_id: str) -> None:
        """删除会话文件（不动 input/ 与任务目录，供测试与清理使用）。"""
        for name in (self.STATE_NAME, self.MESSAGES_NAME, self.EVENTS_NAME):
            target = self._chat_dir(mission_id) / name
            if target.exists():
                target.unlink()

    # --- jsonl helpers -------------------------------------------------------

    def _read_rows(self, mission_id: str, name: str, model: type[BaseModel]) -> list[Any]:
        path = self._chat_dir(mission_id) / name
        if not path.exists():
            return []
        rows: list[Any] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(model.model_validate(json.loads(line)))
            except Exception:  # noqa: BLE001 — 单行损坏不应丢掉整个会话
                logger.warning("planner 记录损坏（跳过）: %s: %.120s", path, line)
        return rows

    def _append_rows(self, mission_id: str, name: str, rows: list[Any]) -> None:
        if not rows:
            return
        path = self._chat_dir(mission_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(row.model_dump_json() + "\n")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """同目录临时文件 + fsync + os.replace；避免读到写了一半的 state。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def planner_owners(data_dir: Path) -> dict[str, str]:
    """扫描任务目录，返回 ``mission_id -> owner_id``（供归属回填脚本使用）。"""
    out: dict[str, str] = {}
    for state_path in sorted(Path(data_dir).glob(f"missions/*/chat/{PlannerSessionStore.STATE_NAME}")):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            continue
        mission_id = str(data.get("mission_id") or state_path.parent.parent.name)
        owner = str(data.get("owner_id") or "").strip()
        if mission_id and owner:
            out[mission_id] = owner
    return out


def drop_legacy_briefing_store(data_dir: Path) -> int:
    """删除全局 ``data/briefings.json``，返回被丢弃的会话数。

    新布局把会话写到 ``missions/<id>/chat/``，旧文件已无读取方；留着只会
    让人误以为数据仍在生效。按约定不做迁移，启动时直接清理。
    """
    path = Path(data_dir) / "briefings.json"
    if not path.exists():
        return 0
    count = 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "[]")
        count = len(raw) if isinstance(raw, list) else len(raw or {})
    except (OSError, json.JSONDecodeError):
        count = 0
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("删除旧 briefings.json 失败（保留原文件）: %s: %s", path, exc)
        return 0
    logger.warning("已删除旧全局 briefings.json（%s 个会话，按约定不迁移）: %s", count, path)
    return count


_CONFIRM_RE = re.compile(r"^(确认|confirm|/confirm|同意|就这样|开干|组建)\b", re.I)
_REJECT_RE = re.compile(r"^(驳回|reject|/reject|重来|改一下)\b", re.I)
# 续聊词：中断后用「继续」等词恢复，不应覆盖此前已确定的标题/需求上下文
_CONTINUE_RE = re.compile(r"^(继续|接着|继续刚才|按刚才|按之前|沿用|continue|go\s*on|resume)\b", re.I)
_WELCOME_PREFIX = "我是建队规划官"  # start() 注入的欢迎语，拼历史时需剔除
# 拼入规划上下文的历史轮次上限（防上下文膨胀）
_MAX_PRIOR_TURNS = 12
_MAX_PRIOR_CHARS = 6000

_SLOT_TITLES = {
    "design": "Design Navigator",
    "eng_ios": "iOS Raider",
    "eng_android": "Android Raider",
    "eng_web": "Web Raider",
    "eng_backend": "Backend Raider",
    "eng_agent": "Agent Raider",
    "eng": "Platform Eng Raider",
    "product": "PRD Scout",
    "qa": "QA Gatekeeper",
    "deploy": "Deploy Officer",
}


class MissionBriefingAgent:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        store: PlannerSessionStore | None = None,
        agents: AgentCatalog | None = None,
        knowledge: KnowledgeGateway | None = None,
        forge: AgentForge | None = None,
        create_initiative: CreateFn | None = None,
        list_initiatives: ListFn | None = None,
        save_initiative: SaveFn | None = None,
    ) -> None:
        self.cfg = settings or default_settings
        self.data_dir = Path(self.cfg.data_dir)
        self.store = store or PlannerSessionStore(self.cfg)
        self.agents = agents
        self.forge = forge
        # Brief of the mission currently being planned — used to seed forge drafts
        self._current_brief = ""
        self.knowledge = knowledge or gateway_from_settings(self.cfg)
        self._create_initiative = create_initiative
        self._list_initiatives = list_initiatives
        self._save_initiative = save_initiative

    def _llm(self) -> LlmClient:
        from sortie_deck.llm import LlmClient

        return LlmClient(cli_fallback=bool(self.cfg.llm_cli_fallback))

    def _coding_executor(self) -> str:
        from sortie_deck.llm import resolve_coding_executor

        return resolve_coding_executor(self.cfg.default_coding_executor)

    def start(self, *, owner_id: str = "") -> BriefingSession:
        """开启建队会话；owner_id 为发起人用户 id，确认建队时作为任务创建者。

        任务 id 在这里预生成并落盘（``missions/<id>/``）：会话消息、导入资料
        从第一轮就写进任务目录，确认建队时复用同一 id，不存在「先写全局文件、
        再迁移一次」的空窗，也不会出现会话与任务两份数据。
        """
        sess = self.store.create(owner_id=owner_id)
        coding = self._coding_executor()
        sess.coding_executor = coding
        llm = self._llm()
        planner_hint = (
            f"规划官后端：`{llm.backend}` · 工程执行器：`{coding}`"
            if llm.enabled
            else f"规划官：启发式（无 LLM）· 工程执行器：`{coding}`"
        )
        welcome = (
            "我是建队规划官。用自然语言描述任务目标——我会查阅历史任务与知识库，"
            "由真实 agent 提案航线与小队编制；缺角色时自动创建。确认后才组建小队。\n"
            f"{planner_hint}\n"
            "例：`做一个门户预发健康检查` · 回复 `确认` / `驳回`"
        )
        sess.messages.append(BriefingMessage(role="assistant", text=welcome))
        sess.events.append(
            HarnessEvent(
                kind=EventKind.ASSISTANT,
                text=welcome,
                data={"step": "welcome", "coding_executor": coding, "llm_backend": llm.backend},
            )
        )
        return self.store.save(sess)


    async def handle(
        self,
        session_id: str,
        text: str,
        *,
        user_name: str = "Operator",
        user_id: str = "",
        attachments: list[BriefingAttachment] | None = None,
        materials: list[ExternalMaterial] | None = None,
    ) -> BriefingSession:
        sess = self.store.get(session_id)
        # 存量会话（新增 owner_id 之前创建）没有归属：由首个续聊的操作人补绑，
        # 之后不再改写，避免他人接手会话时把任务挪到自己名下
        if user_id and not sess.owner_id:
            sess.owner_id = user_id
        text = (text or "").strip()
        atts = _sanitize_attachments(attachments)
        # Materials snapshot is refreshed only when provided, so the confirm
        # turn (which sends no materials) keeps the session's last panel state.
        if materials is not None:
            # 落 missions/<id>/input/manifest.json：任务目录是唯一事实源，
            # state 文件里不再冗余一份（原件/抽取信息由 mission_input 维护）。
            sess.materials = mission_input.upsert_materials(
                self.data_dir, sess.id, [ExternalMaterial.model_validate(m) for m in materials]
            )
        if not text and not atts:
            return self.store.save(sess)
        if sess.status == "done":
            sess.messages.append(
                BriefingMessage(role="assistant", text="本会话已组建完成，请到「小队」查看。")
            )
            return self.store.save(sess)

        display_text = text or (f"[附件 ×{len(atts)}]" if atts else "")
        sess.messages.append(BriefingMessage(role="user", text=display_text, attachments=atts))
        # Persist the user turn up front: if the client aborts mid-planning the
        # session keeps the message and can be continued afterwards.
        self.store.save(sess)
        harness, state = self._bind(sess)

        if sess.status == "awaiting_confirm" and _CONFIRM_RE.search(text):
            harness.confirm(state, approved=True, note="用户确认规划")
            await self._commit(sess, state, harness, user_name=user_name)
            return self._persist(sess, state)

        if sess.status == "awaiting_confirm" and _REJECT_RE.search(text):
            harness.confirm(state, approved=False, note="用户驳回")
            sess.status = "planning"
            sess.draft = {}
            sess.brief = ""
            sess.title = ""
            reply = "已驳回。请重新描述任务目标。"
            sess.messages.append(BriefingMessage(role="assistant", text=reply))
            harness.emit(state, EventKind.ASSISTANT, reply)
            return self._persist(sess, state)

        composed = _compose_brief(text, atts)
        # 续聊词（继续/接着…）不覆盖此前已确定的标题，避免标题变成「继续」
        new_title = (text.split("\n", 1)[0] if text else (atts[0].name if atts else "Untitled"))[:48]
        if not _CONTINUE_RE.search(text) or not sess.title:
            sess.title = new_title
        sess.brief = composed

        # 继承本轮之前的会话上下文：中断后说「继续」也能还原需求与多端拆分。
        # 仅取历史轮次（排除刚 appended 的当前轮），剔除欢迎语，限长防上下文膨胀。
        prior_msgs = sess.messages[:-1] if len(sess.messages) > 1 else []
        prior_turns: list[str] = []
        for m in prior_msgs:
            if m.role not in ("user", "assistant"):
                continue
            if m.role == "assistant" and (m.text or "").startswith(_WELCOME_PREFIX):
                continue
            t = (m.text or "").strip()
            if t:
                prior_turns.append(f"[{m.role}] {t}")
        prior_turns = prior_turns[-_MAX_PRIOR_TURNS:]
        prior_block = "\n".join(prior_turns)
        if len(prior_block) > _MAX_PRIOR_CHARS:
            prior_block = prior_block[-_MAX_PRIOR_CHARS:]

        # Discussion context: 历史对话 + 当前需求 + 全部导入资料（钉住或未钉）
        discussion = composed
        if prior_block:
            discussion = f"{prior_block}\n\n{composed}"
        mat_ctx = _materials_context(sess.materials)
        if mat_ctx:
            discussion = f"{discussion}\n\n{mat_ctx}"
        self._current_brief = composed
        state.status = "planning"
        state.goal = discussion
        if sess.materials:
            pinned_n = sum(1 for m in sess.materials if m.pinned)
            harness.emit(
                state,
                EventKind.WATERFALL,
                f"⓪ 导入外部资料 {len(sess.materials)} 份（钉住 {pinned_n}）",
                step="materials",
                names=[m.name for m in sess.materials],
            )
        if atts:
            harness.emit(
                state,
                EventKind.WATERFALL,
                f"⓪ 收到附件 {len(atts)} 个",
                step="attachments",
                names=[a.name for a in atts],
            )

        # 五步瀑布交给 LangGraph 图驱动：节点顺序、事件文案与参数取值
        # 见 sortie_deck/harness/waterfall.py
        await run_waterfall(
            harness=harness,
            harness_state=state,
            title=sess.title,
            composed=composed,
            discussion=discussion,
        )

        draft = {
            "title": sess.title,
            "brief": sess.brief,
            "coding_executor": sess.coding_executor or self._coding_executor(),
            "pipeline": state.context.get("pipeline"),
            "stages": state.context.get("stages"),
            "role_agents": state.context.get("role_agents") or {},
            "history_hits": state.context.get("history_hits") or [],
            "knowledge_hits": state.context.get("knowledge_hits") or [],
            "created_agents": state.context.get("created_agents") or [],
            "forged_agents": state.context.get("forged_agents") or [],
            "attachments": [
                {"name": a.name, "mime": a.mime, "kind": a.kind, "size": a.size} for a in atts
            ],
            "planner": (state.context.get("pipeline") or {}).get("planner"),
            "planner_backend": (state.context.get("pipeline") or {}).get("planner_backend"),
            "loadout_planner": state.context.get("loadout_planner"),
        }
        sess.draft = draft
        harness.request_confirm(
            state,
            title="请确认航线与编制",
            plan={
                "title": draft["title"],
                "stages": [s.get("id") for s in (draft["stages"] or [])],
                "roles": list((draft["role_agents"] or {}).keys()),
                "created_agents": draft["created_agents"],
                "forged_agents": draft.get("forged_agents") or [],
                "attachments": draft["attachments"],
            },
        )
        sess.status = "awaiting_confirm"
        reply = self._format_plan_reply(draft)
        sess.messages.append(BriefingMessage(role="assistant", text=reply))
        harness.emit(state, EventKind.ASSISTANT, reply, step="propose")
        return self._persist(sess, state)

    def _bind(self, sess: BriefingSession) -> tuple[Harness, HarnessState]:
        registry = ToolRegistry()
        registry.register(ToolSpec("search_history", "Search past missions", self._tool_history))
        registry.register(ToolSpec("retrieve_knowledge", "Retrieve KB", self._tool_kb))
        registry.register(ToolSpec("propose_pipeline", "Propose stages", self._tool_pipeline))
        registry.register(ToolSpec("propose_loadout", "Propose loadout", self._tool_loadout))
        registry.register(ToolSpec("ensure_missing_roles", "Create missing agents", self._tool_ensure_roles))
        harness = Harness(registry, name="mission-briefing")
        state = HarnessState(session_id=sess.id, goal=sess.brief or sess.title)
        state.events = list(sess.events)
        state.context = {}
        return harness, state

    def _persist(self, sess: BriefingSession, state: HarnessState) -> BriefingSession:
        sess.events = list(state.events)
        if state.status == "done":
            sess.status = "done"
        elif state.status == "awaiting_confirm":
            sess.status = "awaiting_confirm"
        return self.store.save(sess)

    def set_role_repos(
        self,
        session_id: str,
        role_repos: dict[str, list[dict[str, Any]]],
    ) -> BriefingSession:
        """Attach per-slot git repos to the pending draft (UI edit before confirm).

        Each entry: ``{"url": ..., "branch": ..., "name": ..., "token": ...}``.
        Empty ``url`` rows are dropped; only slots keep non-empty lists.
        """
        sess = self.store.get(session_id)
        clean: dict[str, list[dict[str, Any]]] = {}
        for slot, repos in (role_repos or {}).items():
            rows = []
            for r in repos or []:
                if not isinstance(r, dict):
                    continue
                url = str(r.get("url") or "").strip()
                if not url:
                    continue
                rows.append(
                    {
                        "url": url,
                        "branch": str(r.get("branch") or "").strip(),
                        "name": str(r.get("name") or "").strip(),
                        "token": str(r.get("token") or "").strip() or None,
                    }
                )
            if rows:
                clean[str(slot)] = rows
        sess.draft = {**(sess.draft or {}), "role_repos": clean}
        return self.store.save(sess)

    async def _commit(
        self,
        sess: BriefingSession,
        state: HarnessState,
        harness: Harness,
        *,
        user_name: str,
    ) -> None:
        if not self._create_initiative:
            harness.emit(state, EventKind.ERROR, "create_initiative not wired")
            sess.status = "failed"
            return
        draft = sess.draft or {}
        req = CreateInitiativeRequest(
            title=str(draft.get("title") or sess.title or "Untitled"),
            brief=str(draft.get("brief") or sess.brief),
            # Materials ride into the mission; orchestrator folds pinned ones
            # into the brief as persistent context (materials.compose_brief).
            materials=list(sess.materials or []),
            coding_executor=str(draft.get("coding_executor") or self._coding_executor()),  # type: ignore[arg-type]
            role_agents=dict(draft.get("role_agents") or {}),
            role_repos={
                slot: [RepoConfig.model_validate(r) for r in repos]
                for slot, repos in (draft.get("role_repos") or {}).items()
                if repos
            },
            creator_name=user_name,
            # 归属到会话发起人；owner_id 为空时留 None（仅 admin 可见，见 app._visible_initiatives）
            created_by=sess.owner_id or None,
            auto_confirm_pipeline=False,
            auto_start=False,
        )
        # 复用会话预生成的任务 id：input/chat 目录从头到尾是同一个任务目录
        ini = await self._create_initiative(req, initiative_id=sess.id)
        stages = draft.get("stages") or []
        if stages:
            proposal = draft.get("pipeline") or {
                "agent": "pipeline_planner",
                "track_hint": "standard",
                "rationale": "mission briefing harness",
                "stages": stages,
                "status": "pending",
            }
            ini.meta = apply_proposal_to_meta(ini.meta, proposal, confirmed=False)
            if self._save_initiative:
                maybe = self._save_initiative(ini)
                if hasattr(maybe, "__await__"):
                    await maybe  # type: ignore[misc]
        # 待审 forge 草稿写入任务 meta：出击门禁据此阻断未审核角色的任务
        # （Orchestrator.start_pipeline → forge_review_required）
        forged = [f for f in (draft.get("forged_agents") or []) if f.get("draft_id")]
        if forged:
            ini.meta = {
                **ini.meta,
                "forge_drafts": [str(f["draft_id"]) for f in forged],
                "forge_pending_roles": [str(f.get("role_key") or "") for f in forged],
            }
            if self._save_initiative:
                maybe = self._save_initiative(ini)
                if hasattr(maybe, "__await__"):
                    await maybe  # type: ignore[misc]
        sess.initiative_id = ini.id
        sess.committed_at = utc_now().isoformat()
        sess.status = "done"
        state.status = "done"
        msg = f"小队已组建：`{ini.id}`。请到「小队」确认航线后出击。"
        sess.messages.append(BriefingMessage(role="assistant", text=msg))
        harness.emit(state, EventKind.DONE, msg, initiative_id=ini.id)
        harness.emit(state, EventKind.WATERFALL, "⑥ 组建完成", step="done", initiative_id=ini.id)

    async def _tool_history(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "").lower()
        hits: list[dict[str, Any]] = []
        if self._list_initiatives:
            try:
                items = self._list_initiatives()
            except Exception as exc:  # noqa: BLE001
                return ToolResult(ok=False, summary=str(exc))
            tokens = [t for t in re.findall(r"[\w\u4e00-\u9fff]{2,}", query)]
            for ini in items[:50]:
                blob = f"{ini.title} {ini.brief}".lower()
                if not tokens or any(t in blob for t in tokens):
                    hits.append(
                        {
                            "id": ini.id,
                            "title": ini.title,
                            "status": getattr(ini.status, "value", str(ini.status)),
                            "brief": (ini.brief or "")[:160],
                        }
                    )
                if len(hits) >= 5:
                    break
        return ToolResult(ok=True, summary=f"历史任务命中 {len(hits)} 条", data={"hits": hits})

    async def _tool_kb(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "")
        try:
            hits = self.knowledge.retrieve(query, limit=4)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, summary=f"知识库检索失败: {exc}")
        return ToolResult(
            ok=True,
            summary=f"知识库命中 {len(hits)} 段",
            data={
                "hits": [
                    {
                        "title": h.title,
                        "score": h.score,
                        "source": h.source,
                        "excerpt": h.content[:240],
                    }
                    for h in hits
                ]
            },
        )

    async def _tool_pipeline(self, args: dict[str, Any]) -> ToolResult:
        from sortie_deck.pipeline_llm import plan_pipeline_smart

        proposal = await plan_pipeline_smart(
            title=str(args.get("title") or ""),
            brief=str(args.get("brief") or ""),
            use_llm=bool(self.cfg.pipeline_llm),
            client=self._llm(),
        )
        stages = proposal.get("stages") or []
        backend = proposal.get("planner_backend") or proposal.get("planner") or "heuristic"
        return ToolResult(
            ok=True,
            summary=f"航线提案 `{proposal.get('track_hint')}` · {len(stages)} 节点 · {backend}",
            data={"pipeline": proposal, "stages": stages},
        )

    async def _tool_loadout(self, args: dict[str, Any]) -> ToolResult:
        from sortie_deck.pipeline_llm import propose_loadout_llm

        stages = args.get("stages") or []
        brief = str(args.get("brief") or "")
        loadout_meta: dict[str, Any] = {"planner": "heuristic"}
        roles: list[str] = []
        missing: list[dict[str, Any]] = []
        if self.cfg.pipeline_llm:
            llm_loadout = await propose_loadout_llm(
                brief=brief, stages=stages if isinstance(stages, list) else [], client=self._llm()
            )
            if llm_loadout:
                roles = list(llm_loadout.get("needed_slots") or [])
                missing = list(llm_loadout.get("missing_slots") or [])
                loadout_meta = {
                    "planner": "llm",
                    "planner_backend": llm_loadout.get("planner_backend"),
                    "rationale": llm_loadout.get("rationale") or "",
                    "missing_slots": missing,
                }
        # 目录外角色也占编制位：先让 ensure_missing_roles 生成待审草稿，
        # 而不是悄悄用一个相近角色顶替（那会把"缺角色"伪装成"角色齐全"）。
        for item in missing:
            key = str(item.get("role_key") or "").strip()
            if key and key not in roles:
                roles.append(key)
        if not roles:
            roles = sorted({str(s.get("role") or "eng") for s in stages if isinstance(s, dict)})
            for r in ("product", "qa", "deploy"):
                if r not in roles:
                    roles.append(r)
        role_agents: dict[str, str] = {}
        if self.agents is not None:
            for slot in roles:
                cands = self.agents.list(slot=slot)
                if cands:
                    role_agents[slot] = cands[0].id
        return ToolResult(
            ok=True,
            summary=(
                f"编制位 {len(roles)}：{', '.join(roles)} · {loadout_meta.get('planner')}"
                + (f" · 待 forge {len(missing)}" if missing else "")
            ),
            data={
                "needed_slots": roles,
                "missing_slots": missing,
                "role_agents": role_agents,
                "loadout_planner": loadout_meta,
            },
        )


    async def _tool_ensure_roles(self, args: dict[str, Any]) -> ToolResult:
        slots = [str(s) for s in (args.get("slots") or []) if str(s).strip()]
        # 规划官给出的目录外角色元数据（标题/能力/理由），用于生成可读的 forge 草稿
        missing_meta = {
            str(m.get("role_key")): m
            for m in (args.get("missing") or [])
            if isinstance(m, dict) and str(m.get("role_key") or "").strip()
        }
        created: list[dict[str, str]] = []
        forged: list[dict[str, Any]] = []
        if self.agents is None:
            return ToolResult(ok=True, summary="无角色目录，跳过", data={"created": []})
        from sortie_deck.roles import get_role_registry

        registry = get_role_registry()
        for slot in slots:
            if self.agents.list(slot=slot):
                continue
            meta = missing_meta.get(slot) or {}
            title = (
                _SLOT_TITLES.get(slot)
                or (registry.maybe(slot).title if registry.maybe(slot) else "")
                or str(meta.get("title") or "")
            )
            title = title or f"{slot} specialist"
            try:
                persona = load_persona(slot)
            except Exception:  # noqa: BLE001 — persona is optional
                persona = ""
            tags = [slot, "auto", "briefing"]
            # Unknown role key → forge a pending-review draft instead of faking a persona
            if self.forge is not None and not registry.is_known(slot):
                try:
                    capability = str(meta.get("capability") or "").strip()
                    draft = await self.forge.propose(
                        ForgeRequest(
                            capability=capability
                            or f"{slot} role for mission: {(self._current_brief or title)[:400]}",
                            title=title,
                            summary=str(meta.get("why") or "").strip(),
                            role_key=slot,
                            requested_by="mission-briefing",
                        )
                    )
                    forged.append(
                        {
                            "slot": slot,
                            "draft_id": draft.id,
                            "role_key": draft.role_key,
                            "issues": draft.issues,
                            "similar": draft.similar,
                        }
                    )
                    title = draft.title or title
                    persona = draft.persona or persona
                    tags = [slot, "forge", "pending-review"]
                except Exception:  # noqa: BLE001 — forge failure falls back to placeholder
                    pass
            if not persona:
                persona = f"You are the fireteam specialist for `{slot}`."
            is_pending = "pending-review" in tags
            agent = self.agents.create(
                CreateAgentRequest(
                    title=title,
                    slot=slot,
                    summary=f"Auto-created by mission briefing (slot={slot})",
                    persona=persona,
                    tags=tags,
                    visibility="public",
                    # Unverified forged personas must be reviewed before going live
                    published=not is_pending,
                ),
                author_id="harness",
                author_name="Mission Briefing",
            )
            created.append({"slot": slot, "id": agent.id, "title": agent.title})
        if created:
            summary = f"新建角色 {len(created)} 个"
            if forged:
                summary += f"（其中 {len(forged)} 个待审核 forge 草稿）"
        else:
            summary = "编制角色齐全"
        return ToolResult(ok=True, summary=summary, data={"created": created, "forged": forged})

    def _format_plan_reply(self, draft: dict[str, Any]) -> str:
        stages = draft.get("stages") or []
        labels = " → ".join(str(s.get("label") or s.get("id")) for s in stages)
        roles = ", ".join((draft.get("role_agents") or {}).keys()) or "(默认模板)"
        hist = draft.get("history_hits") or []
        kb = draft.get("knowledge_hits") or []
        created = draft.get("created_agents") or []
        forged = draft.get("forged_agents") or []
        lines = [
            f"**提案：{draft.get('title') or '未命名'}**",
            "",
            f"航线：{labels or '(空)'}",
            f"编制：{roles}",
        ]
        if hist:
            lines.append(f"历史参考：{', '.join(h['title'] for h in hist[:3])}")
        if kb:
            lines.append(f"知识库：{', '.join(h['title'] for h in kb[:3])}")
        if created:
            lines.append(f"新建角色：{', '.join(c['title'] for c in created)}")
        if forged:
            keys = ", ".join(str(f.get("role_key")) for f in forged)
            lines.append(f"⚙ 新角色草稿待审核（未发布）：{keys}")
        atts = draft.get("attachments") or []
        if atts:
            lines.append(f"附件：{', '.join(a['name'] for a in atts[:5])}")
        planner = draft.get("planner") or (draft.get("pipeline") or {}).get("planner")
        backend = draft.get("planner_backend") or (draft.get("pipeline") or {}).get("planner_backend")
        if planner:
            lines.append(f"规划引擎：{planner}" + (f"/{backend}" if backend else ""))
        coding = draft.get("coding_executor")
        if coding:
            lines.append(f"工程执行器：`{coding}`")
        role_repos = draft.get("role_repos") or {}
        if role_repos:
            lines.append("代码仓库：")
            for slot, repos in role_repos.items():
                for r in repos:
                    branch = r.get("branch") or "默认分支"
                    lines.append(f"- {slot}：`{r.get('url')}` @ {branch}")
        lines.extend(["", "回复 **确认** 组建小队，或 **驳回** 后重述需求。"])
        return "\n".join(lines)
