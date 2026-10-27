from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str = "") -> str:
    value = uuid4().hex[:12]
    return f"{prefix}{value}" if prefix else value


class RoleId(str, Enum):
    OPS = "ops"
    PRODUCT = "product"
    DESIGN = "design"
    ENG = "eng"
    ENG_IOS = "eng_ios"
    ENG_ANDROID = "eng_android"
    ENG_WEB = "eng_web"
    ENG_BACKEND = "eng_backend"
    ENG_AGENT = "eng_agent"
    QA = "qa"
    DEPLOY = "deploy"

    @classmethod
    def _missing_(cls, value: object) -> RoleId | None:
        """Accept unknown role keys as first-class members (registry-backed roles).

        Members above stay the built-in set; anything else (e.g. ``parenting``)
        is materialised on first use so every existing call site that relies on
        ``.value`` or ``== RoleId.X`` keeps working unchanged. Custom roles are
        owned by ``sortie_deck.roles.RoleRegistry``; this only widens the type.
        """
        if not isinstance(value, str):
            return None
        key = value.strip()
        if not key:
            return None
        cached = cls._value2member_map_.get(key)
        if cached is not None:
            return cached
        pseudo = str.__new__(cls, key)
        pseudo._name_ = key.upper().replace("-", "_")
        pseudo._value_ = key
        cls._value2member_map_[key] = pseudo
        return pseudo


def role_key(role: Any) -> str:
    """Normalise a ``RoleId`` (or plain str) to its string key."""
    value = getattr(role, "value", None)
    return str(value) if value is not None else str(role)


# Roles that run coding executors (Cursor / Claude Code / mock_eng)
CODING_ROLE_VALUES = frozenset(
    {
        RoleId.ENG.value,
        RoleId.ENG_IOS.value,
        RoleId.ENG_ANDROID.value,
        RoleId.ENG_WEB.value,
        RoleId.ENG_BACKEND.value,
        RoleId.ENG_AGENT.value,
    }
)

# Pipeline build stages owned by engineering specialists
ENG_BUILD_STAGES = frozenset(
    {
        "eng_implement",
        "eng_ios",
        "eng_android",
        "eng_web",
        "eng_backend",
        "eng_agent",
    }
)


class InitiativeStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    WAITING_HITL = "waiting_hitl"
    STOPPED = "stopped"
    FAILED = "failed"
    DONE = "done"


class HitlAction(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT_INSTRUCTION = "edit_instruction"
    STOP = "stop"
    REROUTE = "reroute"
    # Human-authorised harness switch (mid-run unlock, see HarnessLockedError)
    SWITCH_HARNESS = "switch_harness"


class PipelineNotConfirmedError(RuntimeError):
    code = "pipeline_not_confirmed"

    def __init__(self, message: str = "Pipeline not confirmed — confirm or edit stages first") -> None:
        super().__init__(message)
        self.message = message


class HarnessLockedError(RuntimeError):
    """Raised when the mission harness is changed after the mission started."""

    code = "harness_locked"

    def __init__(self, message: str = "Harness is locked once the mission has started") -> None:
        super().__init__(message)
        self.message = message


class ForgeReviewRequiredError(RuntimeError):
    """建队时自动 forge 的角色仍待人工审核，出击被拒。

    自动创建 ≠ 自动发布：目录外角色由规划官生成 `pending-review` 草稿，
    必须经 `/api/forge/drafts/{id}/approve` 审核注册后才能启动任务。
    """

    code = "forge_review_required"

    def __init__(self, draft_ids: list[str], message: str | None = None) -> None:
        self.draft_ids = list(draft_ids)
        super().__init__(
            message
            or (
                "Agent forge drafts still pending review: "
                + ", ".join(self.draft_ids)
                + " — approve them before starting the mission"
            )
        )
        self.message = str(self)


class UnknownHarnessError(RuntimeError):
    code = "unknown_harness"

    def __init__(self, message: str = "Unknown harness provider") -> None:
        super().__init__(message)
        self.message = message


class ArtifactKind(str, Enum):
    PRD = "prd.md"
    TECH_DESIGN = "tech_design.md"
    ACCEPTANCE = "acceptance.json"
    TASKS = "tasks.json"
    IMPLEMENTATION = "implementation.md"
    DIFF = "diff.patch"
    TEST_CASES = "test_cases.md"
    TEST_REPORT = "test_report.json"
    DEPLOY_PLAN = "deploy_plan.md"
    DEPLOY_RESULT = "deploy_result.json"
    LOG = "log.txt"
    OTHER = "other"


class RepoConfig(BaseModel):
    """任务关联的 git 仓库（开发 agent 的迭代目标）。

    ``name`` 是 checkout 目录名（默认取 URL basename 去掉 ``.git``）；
    ``branch`` 是**任务关联的目标分支**——任务分支 ``sortie/<initiative_id>``
    基于它创建，编码 agent 在任务分支上迭代，push 后向它发起 MR（人工合并）；
    空 = 远端默认分支；``token`` 注入 https clone/push URL（日志脱敏，不落远端配置）。
    """

    url: str
    branch: str = ""
    name: str = ""
    token: str | None = None


class RoleAgent(BaseModel):
    id: str
    role: RoleId
    title: str
    persona: str
    executor: str
    status: str = "idle"
    toolkit_ids: list[str] = Field(default_factory=list)
    # Repositories this role must clone & read in its workspace before coding
    repos: list[RepoConfig] = Field(default_factory=list)
    catalog_agent_id: str | None = None
    # preset:<role> | https://… | data:image/… — shown in crew + pipeline rail
    avatar: str | None = None


class ArtifactRef(BaseModel):
    kind: str
    path: str
    stage: str
    created_at: datetime = Field(default_factory=utc_now)
    meta: dict[str, Any] = Field(default_factory=dict)


class ExternalMaterial(BaseModel):
    """A user-supplied input file attached to a mission (PRD / plan / design …).

    ``pinned`` marks the material as *persistent context*: its text excerpt is
    folded into the mission brief so every downstream stage (and squad chat)
    keeps carrying it into the prompt. Unpinned materials are archived only.
    """

    id: str = Field(default_factory=lambda: new_id("mat_"))
    name: str
    # Free-form taxonomy surfaced by the UI: prd | plan | design | data | other
    kind: str = "other"
    mime: str = ""
    size: int = 0
    pinned: bool = False
    # Decoded text for text-like materials (markdown / json / csv / …)
    text_excerpt: str = ""
    added_at: datetime = Field(default_factory=utc_now)


class TimelineEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evt_"))
    at: datetime = Field(default_factory=utc_now)
    stage: str
    role: str | None = None
    # toolkit_usage / toolkit_meta / memory_meta 由 graph 的元工具链路写入，
    # 缺失会让阶段收尾的 timeline 合并直接抛校验异常、整个 run 判失败
    kind: Literal[
        "info",
        "artifact",
        "hitl",
        "error",
        "transition",
        "consult",
        "handoff",
        "subroom",
        "toolkit_usage",
        "toolkit_meta",
        "memory_meta",
    ] = "info"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    # 产生该事件的 trace id（32 位 hex）；前端据此拼出 Jaeger/Tempo 深链，
    # 把「业务事件」直接对到「执行 span」。未启用追踪时为空串。
    trace_id: str = ""


class HitlRequest(BaseModel):
    stage: str
    prompt: str
    allowed_actions: list[HitlAction] = Field(
        default_factory=lambda: [
            HitlAction.APPROVE,
            HitlAction.REJECT,
            HitlAction.EDIT_INSTRUCTION,
            HitlAction.STOP,
            HitlAction.REROUTE,
            HitlAction.SWITCH_HARNESS,
        ]
    )
    artifacts: list[ArtifactRef] = Field(default_factory=list)


class HitlDecision(BaseModel):
    action: HitlAction
    instruction: str | None = None
    next_stage: str | None = None
    note: str | None = None
    # Target harness for `switch_harness` decisions
    harness: str | None = None


class ActorKind(str, Enum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class RoomParticipant(BaseModel):
    id: str = Field(default_factory=lambda: new_id("user_"))
    name: str
    kind: ActorKind = ActorKind.HUMAN
    role: str | None = None  # agent role id or human title
    # First-stage discussion: human/agent can opt in/out of the huddle
    discussing: bool = True
    joined_at: datetime = Field(default_factory=utc_now)


class MissionShare(BaseModel):
    """任务共享记录：一条对应一个被分享的账号用户。

    被分享者拥有除删除外的全部任务权限（查看详情/对话/启动停止等），
    删除仍仅限创建者或 admin。输入输出示例：
    {"user_id": "usr_ab12", "username": "operator", "display_name": "Operator",
     "added_by": "usr_cd34", "added_at": "2026-10-15T08:00:00Z"}
    """

    user_id: str
    username: str = ""
    display_name: str = ""
    added_by: str = ""
    added_at: datetime = Field(default_factory=utc_now)


class Initiative(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ini_"))
    title: str
    brief: str
    template: str = "default"
    status: InitiativeStatus = InitiativeStatus.DRAFT
    current_stage: str = "intake"
    thread_id: str = Field(default_factory=lambda: new_id("thr_"))
    room_id: str = Field(default_factory=lambda: new_id("squad_"))
    # Agent harness this mission runs on (claude_code | opencode | codex | cursor | mock).
    # Selected at creation; locked (harness_locked) once the pipeline starts —
    # a mid-run change requires the HITL `switch_harness` action.
    harness: str = "claude_code"
    harness_locked: bool = False
    roles: list[RoleAgent] = Field(default_factory=list)
    participants: list[RoomParticipant] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    # External input materials (PRD / plan / design …); pinned ones are folded
    # into ``brief`` so they ride along into every stage prompt.
    materials: list[ExternalMaterial] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    pending_hitl: HitlRequest | None = None
    human_instruction: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    webhook_url: str | None = None
    # 创建者用户 id（多租户归属，workspace/删除校验用；旧数据为 None 时仅 admin 可操作他人任务）
    created_by: str | None = None
    # 共享给其他账号用户的记录（被分享者拥有除删除外的全部权限）
    shared_with: list[MissionShare] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class StageContext(BaseModel):
    initiative_id: str
    thread_id: str
    stage: str
    role: RoleId
    brief: str
    human_instruction: str | None = None
    artifact_dir: str
    workspace_dir: str | None = None
    upstream_artifacts: dict[str, str] = Field(default_factory=dict)
    # 上游产物的绝对路径映射（与 upstream_artifacts 同键）。执行器据此在 prompt 里
    # 只给「摘要 + 路径」，让 agent 按需 Read，而不是把全文灌进上下文。
    upstream_paths: dict[str, str] = Field(default_factory=dict)
    persona: str = ""
    permissions: dict[str, Any] = Field(default_factory=dict)
    toolkit_ids: list[str] = Field(default_factory=list)
    toolkit_brief: str = ""
    # Mission-scoped git repos for this role (cloned into the workspace by
    # coding executors before the agent starts iterating)
    repos: list[RepoConfig] = Field(default_factory=list)
    # Layered memory brief injected into the agent prompt. Sub-room conclusions
    # arrive here, scoped to the stage that requested them.
    memory_brief: str = ""
    # Initiative-level agent harness (claude_code | opencode | codex | cursor | mock)
    harness: str = ""
    harness_locked: bool = False
    # 任务运行根目录：由编排器按自身配置注入（data_dir / 任务沙盒根）。
    # 执行器不得直接读全局 settings，否则单测、多实例会写到进程级真实数据目录。
    data_dir: str = ""
    worktrees_dir: str = ""
    # Roles this agent's contract allows it to hand off to (read-only hint)
    handoff_targets: list[str] = Field(default_factory=list)
    # 本阶段可自主交接的落点：[{stage, role, label}]。
    # 由图按「plan 后继节点 ∩ 契约白名单 ∩ 非 HITL 节点」生成，提示词据此
    # 让模型用角色表达交接意图，执行器再把角色解析成阶段名写回 proposed_next。
    next_targets: list[dict[str, str]] = Field(default_factory=list)


class StageResult(BaseModel):
    status: Literal["ok", "fail", "needs_hitl"] = "ok"
    message: str = ""
    artifacts: dict[str, str] = Field(default_factory=dict)
    proposed_next: str | None = None
    qa_passed: bool | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class CreateInitiativeRequest(BaseModel):
    title: str
    brief: str
    template: str = "default"
    # Pipeline track hint for planner agent; empty → agent infers
    pipeline_track: Literal["express", "standard", "default", "research"] | None = None
    # auto | trivial | normal | complex — feeds planner when track unset
    complexity: Literal["auto", "trivial", "normal", "complex"] = "auto"
    coding_executor: Literal["mock", "claude_code", "cursor_cli", "auto"] = "auto"
    # Agent harness for the whole mission; None → TDT_DEFAULT_HARNESS.
    # Locked on start; mid-run change requires HITL switch_harness approval.
    harness: str | None = None
    webhook_url: str | None = None
    auto_start: bool = False
    # When true with auto_start, skip human confirm (CLI/tests)
    auto_confirm_pipeline: bool = False
    creator_name: str = "Operator"
    # 创建者用户 id（API 层从登录用户注入；多租户归属校验用）
    created_by: str | None = None
    # Optional published agent ids keyed by pipeline slot: product/eng/qa/deploy
    role_agents: dict[str, str] = Field(default_factory=dict)
    # Toolkit ids for each role slot (applies to that role's stages)
    role_toolkits: dict[str, list[str]] = Field(default_factory=dict)
    # Git/github repositories per role slot — the slot's dev agent clones &
    # reads these before iterating (e.g. eng_backend → server repo, eng_web →
    # web repo)
    role_repos: dict[str, list[RepoConfig]] = Field(default_factory=dict)
    # Optional per-stage override (stage name -> toolkit ids)
    stage_toolkits: dict[str, list[str]] = Field(default_factory=dict)
    # External input materials; pinned ones become persistent mission context
    materials: list[ExternalMaterial] = Field(default_factory=list)


class PipelineStageSpec(BaseModel):
    id: str
    label: str | None = None
    label_en: str | None = None
    role: str | None = None
    hitl_after: bool | None = None
    on_fail: str | None = None
    on_reject: str | None = None
    parallel_group: str | None = None
    branch: str | None = None
    depends_on: list[str] | None = None
    enabled: bool = True


class UpdatePipelineRequest(BaseModel):
    """Manual add/remove/reorder/edit stages (draft only)."""
    stages: list[PipelineStageSpec]
    confirm: bool = False


class ConfirmPipelineRequest(BaseModel):
    """Confirm planner proposal; optional last-mile edits."""
    stages: list[PipelineStageSpec] | None = None
    author_name: str | None = None


class HitlSubmitRequest(BaseModel):
    action: HitlAction
    instruction: str | None = None
    next_stage: str | None = None
    note: str | None = None
    author_name: str | None = None
    # Target harness for `switch_harness` decisions
    harness: str | None = None


class RoomMessage(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg_"))
    at: datetime = Field(default_factory=utc_now)
    initiative_id: str
    actor_kind: ActorKind
    actor_id: str
    actor_name: str
    role: str | None = None
    text: str
    msg_type: Literal[
        "chat",
        "stage",
        "hitl",
        "decision",
        "system",
        "consult",
        "consult_reply",
        "subroom",
    ] = "chat"
    # "main" = the big room. Sub-room traffic lives in its own room file and is
    # never listed with the main timeline.
    room_key: str = "main"
    meta: dict[str, Any] = Field(default_factory=dict)


class PostRoomMessageRequest(BaseModel):
    text: str
    author_name: str = "Operator"
    author_id: str | None = None


class JoinRoomRequest(BaseModel):
    name: str | None = None
    title: str | None = None


class DiscussParticipateRequest(BaseModel):
    """Toggle whether a human or agent participant joins the first-stage huddle."""

    discussing: bool
    participant_id: str | None = None
    # For agents: role slot id e.g. product / eng_web
    role: str | None = None
    # For humans: match by display name when participant_id omitted
    name: str | None = None
