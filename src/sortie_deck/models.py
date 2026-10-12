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


class UnknownHarnessError(RuntimeError):
    code = "unknown_harness"

    def __init__(self, message: str = "Unknown harness provider") -> None:
        super().__init__(message)
        self.message = message


class ArtifactKind(str, Enum):
    PRD = "prd.md"
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
    """A git/github repository a dev agent must read before iterating.

    ``name`` is the checkout dir name (defaults to the URL basename without
    ``.git``); ``branch`` empty means the remote default branch; ``token`` is
    injected into https clone URLs when present.
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


class TimelineEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evt_"))
    at: datetime = Field(default_factory=utc_now)
    stage: str
    role: str | None = None
    kind: Literal[
        "info", "artifact", "hitl", "error", "transition", "consult", "handoff", "subroom"
    ] = "info"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


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
    timeline: list[TimelineEvent] = Field(default_factory=list)
    pending_hitl: HitlRequest | None = None
    human_instruction: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    webhook_url: str | None = None
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
    # Roles this agent's contract allows it to hand off to (read-only hint)
    handoff_targets: list[str] = Field(default_factory=list)


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
