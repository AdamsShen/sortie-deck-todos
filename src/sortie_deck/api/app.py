from __future__ import annotations

import asyncio
import io
import json
import logging
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from sortie_deck.agent_forge import ForgeRequest, get_agent_forge
from sortie_deck.agents import (
    AgentCatalog,
    CreateAgentRequest,
    UpdateAgentRequest,
)
from sortie_deck.auth import (
    AuthStore,
    CreateUserRequest,
    LoginRequest,
    PublicUser,
    RegisterRequest,
)
from sortie_deck.doc_extract import extract_office_text
from sortie_deck.knowledge import (
    CreateKnowledgeBaseRequest,
    CreateKnowledgeRequest,
    KnowledgeBaseModel,
    KnowledgeBaseStore,
    KnowledgeStore,
    UpdateKnowledgeBaseRequest,
    UpdateKnowledgeRequest,
)
from sortie_deck.knowledge_gateway import (
    ImportKnowledgeRequest,
    KnowledgeGateway,
    gateway_from_settings,
)
from sortie_deck.memory import (
    CreateMemoryRequest,
    MemoryStore,
    UpdateMemoryRequest,
)
from sortie_deck.models import (
    ConfirmPipelineRequest,
    CreateInitiativeRequest,
    DiscussParticipateRequest,
    ExternalMaterial,
    ForgeReviewRequiredError,
    HitlAction,
    HitlDecision,
    HitlSubmitRequest,
    JoinRoomRequest,
    MissionShare,
    PipelineNotConfirmedError,
    PostRoomMessageRequest,
    UpdatePipelineRequest,
)
from sortie_deck.orchestrator import Orchestrator, bind_catalogs, get_orchestrator
from sortie_deck.persona import parse_persona
from sortie_deck.pipeline import list_tracks, stage_catalog
from sortie_deck.prompt_enhance import EnhancePromptRequest
from sortie_deck.roles import RoleSpec, get_role_registry, normalize_role_key
from sortie_deck.settings import settings
from sortie_deck.toolkit import (
    CreateToolkitRequest,
    ImportToolkitRequest,
    ToolkitStore,
    UpdateToolkitRequest,
)
from sortie_deck.weknora import WeKnoraError

_auth: AuthStore | None = None
logger = logging.getLogger(__name__)
_knowledge: KnowledgeStore | None = None
_knowledge_gw: KnowledgeGateway | None = None
_kb_bases: KnowledgeBaseStore | None = None
_memory: MemoryStore | None = None
_agents: AgentCatalog | None = None
_toolkit: ToolkitStore | None = None
_briefing: Any = None


def get_auth() -> AuthStore:
    global _auth
    if _auth is None:
        _auth = AuthStore(settings.users_db, token_secret=settings.resolved_token_secret())
    return _auth


def _ensure_mission_delete(ini: Any, user: PublicUser) -> None:
    """解散任务（删除）：仅创建者或 admin。

    管理员对他人任务只读，但保留「解散」这一项管理能力。
    无归属任务（created_by 为空）同样只允许 admin：历史数据应先用
    scripts/backfill_mission_owner.py 回填创建者。
    """
    if user.role == "admin":
        return
    created_by = getattr(ini, "created_by", None)
    if not created_by or created_by != user.id:
        raise HTTPException(403, "仅任务创建者或 admin 可解散任务")


def _ensure_mission_share_owner(ini: Any, user: PublicUser) -> None:
    """共享管理（加人/移除他人）：仅创建者。

    admin 不再是共享操作的旁路——管理员不能把他人任务共享给第三方，
    需要共享时由创建者自己操作。被共享者退出共享另走 remove 自身分支。
    """
    created_by = getattr(ini, "created_by", None)
    if not created_by or created_by != user.id:
        raise HTTPException(403, "仅任务创建者可将任务共享给他人")


def _is_mission_shared_with(ini: Any, user_id: str) -> bool:
    """判断用户是否在任务的共享名单中。"""
    return any(s.user_id == user_id for s in getattr(ini, "shared_with", []) or [])


def _ensure_mission_control(ini: Any, user: PublicUser) -> None:
    """任务运行控制：创建者或被显式共享的用户。

    admin 对他人任务只读（可看列表/详情/实时过程，可解散），不能启动、停止、
    确认门禁、改航线、换 harness 或在房间里发言。若 admin 本人是创建者，
    或被 owner 显式共享，则同样拥有控制权。
    """
    created_by = getattr(ini, "created_by", None)
    if created_by and created_by == user.id:
        return
    if _is_mission_shared_with(ini, user.id):
        return
    raise HTTPException(403, "仅任务创建者或被共享用户可控制任务运行；管理员为只读")


def _ensure_mission_access(ini: Any, user: PublicUser) -> None:
    """任务访问校验：创建者 / admin / 被共享用户可访问（删除另有 owner 校验）。

    共享用户拥有除删除外的全部权限（查看详情/对话、启动/停止、HITL 等）。
    无归属任务（created_by 为空）仅 admin 可访问：这类历史数据需先跑
    scripts/backfill_mission_owner.py 回填创建者，或由 owner 显式共享。
    """
    if user.role == "admin":
        return
    # 显式共享优先：无归属的历史任务只要 owner 共享过，被共享者仍可访问
    if _is_mission_shared_with(ini, user.id):
        return
    created_by = getattr(ini, "created_by", None)
    if not created_by:
        raise HTTPException(403, "该任务无归属信息，仅管理员可访问；请先回填创建者")
    if created_by == user.id:
        return
    raise HTTPException(403, "无权访问该任务（未共享给当前账号）")


def _visible_initiatives(items: list[Any], user: PublicUser) -> list[Any]:
    """任务列表按账号过滤：创建者 / 被共享用户 / admin 可见。

    无归属任务（created_by 为空）仅 admin 可见，不再全站放行；
    其他账号要访问必须由 owner 通过 shared_with 显式共享。
    """
    if user.role == "admin":
        return items
    return [
        i
        for i in items
        if getattr(i, "created_by", None) == user.id
        or _is_mission_shared_with(i, user.id)
    ]


def _owned_mission_ids(user: PublicUser) -> set[str]:
    """当前用户作为所有权人（created_by）的任务 id 集合。

    小群属于执行过程的内部讨论，可见性比任务本身更窄：只认所有权，
    不随 shared_with 传递，也不给 admin 角色豁免。想要"能不能看别人任务"
    这种更宽的口径请用 `_ensure_mission_access`。
    """
    return {
        i.id for i in orch().list_initiatives() if getattr(i, "created_by", None) == user.id
    }


def get_knowledge() -> KnowledgeStore:
    global _knowledge
    if _knowledge is None:
        _knowledge = KnowledgeStore(settings.knowledge_db)
    return _knowledge


def get_knowledge_gateway() -> KnowledgeGateway:
    global _knowledge_gw
    if _knowledge_gw is None:
        _knowledge_gw = gateway_from_settings(settings, get_knowledge())
    return _knowledge_gw


def get_knowledge_bases() -> KnowledgeBaseStore:
    global _kb_bases
    if _kb_bases is None:
        _kb_bases = KnowledgeBaseStore(settings.knowledge_bases_db)
    return _kb_bases


def _visible_base_ids(user: PublicUser) -> set[str] | None:
    """当前用户可见的知识库 id 集合；admin 返回 None 表示不过滤（全可见）。

    可见性规则：global 全员；user 仅所有者本人；task 按任务可见性
    （创建者 / 被共享用户，复用 _visible_initiatives 的口径）。
    """
    if user.role == "admin":
        return None
    visible_tasks = {i.id for i in _visible_initiatives(orch().list_initiatives(), user)}
    out: set[str] = set()
    for b in get_knowledge_bases().list():
        if b.scope == "global" or (b.scope == "user" and b.scope_id == user.id):
            out.add(b.id)
        elif b.scope == "task" and b.scope_id in visible_tasks:
            out.add(b.id)
    return out


def _ensure_base_write(user: PublicUser, base: KnowledgeBaseModel) -> None:
    """知识库写权限（建库后续的编辑/导入/删除）：global 仅 admin；user 仅本人；task 校验任务访问。"""
    if user.role == "admin":
        return
    if base.scope == "global":
        raise HTTPException(403, "全局共享知识库仅管理员可操作")
    if base.scope == "user" and base.scope_id != user.id:
        raise HTTPException(403, "仅知识库所有者可操作")
    if base.scope == "task":
        # 任务知识库：能访问该任务（创建者/被共享）即可导入
        ini = orch().get(base.scope_id)
        if ini is not None:
            _ensure_mission_access(ini, user)


def get_memory() -> MemoryStore:
    global _memory
    if _memory is None:
        # Pluggable shared-memory backend: local, or local + mem0 in parallel.
        from sortie_deck.memory import build_memory_store

        _memory = build_memory_store(settings.memory_db)  # type: ignore[assignment]
    return _memory


def get_agents() -> AgentCatalog:
    global _agents
    if _agents is None:
        _agents = AgentCatalog(settings.agents_db)
    return _agents


def get_toolkit() -> ToolkitStore:
    global _toolkit
    if _toolkit is None:
        _toolkit = ToolkitStore(settings.toolkit_db)
    return _toolkit


def get_briefing():
    global _briefing
    if _briefing is None:
        from sortie_deck.mission_briefing import MissionBriefingAgent

        o = get_orchestrator()

        async def _save(ini):
            o.store.upsert(ini)
            return ini

        _briefing = MissionBriefingAgent(
            settings=settings,
            agents=get_agents(),
            knowledge=get_knowledge_gateway(),
            forge=get_agent_forge(),
            create_initiative=o.create_initiative,
            list_initiatives=o.list_initiatives,
            save_initiative=_save,
        )
    return _briefing


@asynccontextmanager
async def lifespan(app: FastAPI):
    from sortie_deck import observability as obs
    from sortie_deck.runtime import (
        configure_logging,
        ensure_runtime_dirs,
        validate_settings,
    )

    configure_logging(settings.log_level)
    # 全链路追踪：开关关闭 / 未装 SDK / endpoint 为空时自动降级为 no-op，
    # 不阻断启动（见 sortie_deck/observability.py）
    obs.init_tracing(settings)
    ensure_runtime_dirs(settings)
    # 旧布局（data/artifacts|worktrees|rooms）→ data/missions/<id>/ 启动迁移（幂等）
    from sortie_deck.layout import migrate_legacy_layout

    migrated = migrate_legacy_layout(settings.data_dir)
    if migrated:
        logger.info("mission layout 迁移完成: %s", migrated)
    validate_settings(settings)
    orch = get_orchestrator()
    await orch.startup()
    get_auth()
    get_knowledge()
    mem = get_memory()
    agents = get_agents()
    toolkit = get_toolkit()
    bind_catalogs(agents=agents, memory=mem, toolkit=toolkit)
    app.state.orch = orch
    yield
    await orch.shutdown()
    # flush 最后一批 span，否则进程退出时 BatchSpanProcessor 队列里的会丢
    obs.shutdown_tracing()


app = FastAPI(title="Sortie Deck", version="0.5.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def orch() -> Orchestrator:
    return get_orchestrator()


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization.strip()


def require_user(authorization: str | None = Header(default=None)) -> PublicUser:
    user = get_auth().resolve(_bearer(authorization))
    if not user:
        raise HTTPException(401, "login required")
    return user


def require_admin(user: PublicUser = Depends(require_user)) -> PublicUser:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user


def _mission_access_guard(
    initiative_id: str, user: PublicUser = Depends(require_user)
) -> PublicUser:
    """FastAPI 依赖：任务级访问校验（创建者/admin/被共享用户，删除另走 owner 校验）。"""
    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    _ensure_mission_access(ini, user)
    return user


def _mission_control_guard(
    initiative_id: str, user: PublicUser = Depends(require_user)
) -> PublicUser:
    """FastAPI 依赖：任务控制级校验（创建者或被共享用户；admin 只读）。"""
    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    _ensure_mission_control(ini, user)
    return user


@app.get("/api/health")
async def health() -> dict[str, Any]:
    o = orch()
    return {
        "status": "ok",
        "product": "Sortie Deck",
        "version": app.version,
        "env": settings.env,
        "storage": settings.storage,
        "run_backend": settings.run_backend,
        "artifact_backend": settings.artifact_backend,
        "initiatives": len(o.list_initiatives()),
        "executors": len(o.registry.names()),
    }


@app.post("/api/auth/login")
async def login(req: LoginRequest) -> dict[str, Any]:
    try:
        payload = get_auth().login(req.username, req.password)
        return payload.model_dump()
    except PermissionError as exc:
        # 密码正确但账号待审核/已被拒绝时返回 403 与具体原因，其余为 401 凭据错误
        msg = str(exc)
        if msg == "account pending":
            raise HTTPException(403, "account pending approval") from None
        if msg == "account rejected":
            raise HTTPException(403, "account rejected") from None
        raise HTTPException(401, "invalid username or password") from None


@app.post("/api/auth/register")
async def register(req: RegisterRequest) -> dict[str, Any]:
    """自助注册：创建待审核账号，管理员通过后才能登录；用户名重复返回 400。"""
    if not req.username.strip() or not req.password:
        raise HTTPException(400, "username and password required")
    try:
        user = get_auth().register(req)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "pending", "user": user.model_dump()}


@app.post("/api/auth/logout")
async def logout(authorization: str | None = Header(default=None)) -> dict[str, str]:
    token = _bearer(authorization)
    if token:
        get_auth().logout(token)
    return {"status": "ok"}


@app.delete("/api/auth/account")
async def delete_own_account(user: PublicUser = Depends(require_user)) -> dict[str, str]:
    """账号注销：用户永久删除自己的账号（清除登录 token）；名下任务与共享关系保留。"""
    try:
        get_auth().delete_self(user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except KeyError:
        raise HTTPException(404, "user not found") from None
    return {"status": "deleted"}


@app.get("/api/auth/me")
async def me(user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    return user.model_dump()


@app.get("/api/admin/users")
async def admin_list_users(_admin: PublicUser = Depends(require_admin)) -> list[dict[str, Any]]:
    return [u.model_dump() for u in get_auth().list_users()]


@app.post("/api/admin/users")
async def admin_create_user(
    req: CreateUserRequest, _admin: PublicUser = Depends(require_admin)
) -> dict[str, Any]:
    try:
        return get_auth().create_user(req).model_dump()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class AdminUpdateUserRequest(BaseModel):
    # 可选字段：只传哪个就改哪个；status 用于审核（active=通过 / rejected=拒绝）
    role: Literal["admin", "member"] | None = None
    status: Literal["active", "rejected"] | None = None


@app.patch("/api/admin/users/{user_id}")
async def admin_update_user(
    user_id: str,
    req: AdminUpdateUserRequest,
    _admin: PublicUser = Depends(require_admin),
) -> dict[str, Any]:
    """管理员修改用户角色 / 审核状态（二选一或同时传）。"""
    try:
        if req.role is not None:
            get_auth().update_role(user_id, req.role)
        if req.status is not None:
            get_auth().set_status(user_id, req.status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except KeyError:
        raise HTTPException(404, "user not found") from None
    return get_auth().get_by_id(user_id).model_dump(mode="json", include={"id", "username", "display_name", "role", "status"})


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: str, _admin: PublicUser = Depends(require_admin)) -> dict[str, str]:
    try:
        get_auth().delete_user(user_id)
        return {"status": "deleted"}
    except KeyError:
        raise HTTPException(404, "user not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/users/search")
async def search_users(
    q: str = Query(default=""),
    _user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    """按账号名 / 显示名 / 用户 id 模糊搜索账号（任务分享面板选人用，最多返回 20 条）。"""
    kw = q.strip().lower()
    users = get_auth().list_users()
    if kw:
        users = [
            u
            for u in users
            if kw in u.id.lower() or kw in u.username.lower() or kw in (u.display_name or "").lower()
        ]
    return [u.model_dump() for u in users[:20]]


@app.get("/api/dashboard")
async def dashboard(user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    items = _visible_initiatives(orch().list_initiatives(), user)
    by_status: dict[str, int] = {}
    for i in items:
        by_status[i.status.value] = by_status.get(i.status.value, 0) + 1
    recent = [i.model_dump(mode="json") for i in items[:8]]
    return {
        "user": user.model_dump(),
        "totals": {
            "initiatives": len(items),
            "draft": by_status.get("draft", 0),
            "running": by_status.get("running", 0),
            "waiting_hitl": by_status.get("waiting_hitl", 0),
            "done": by_status.get("done", 0),
            "failed": by_status.get("failed", 0),
            "stopped": by_status.get("stopped", 0),
        },
        "by_status": by_status,
        "recent": recent,
    }


@app.get("/api/knowledge")
async def list_knowledge(
    q: str | None = Query(default=None),
    base_id: str | None = Query(default=None),
    user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    """文档目录：始终按所属库的可见性过滤（他人 user/task 库的文档不下发）。"""
    docs = get_knowledge_gateway().list_catalog(q)
    default_base = get_knowledge_bases().ensure_default()
    bases = {b.id: b for b in get_knowledge_bases().list()}
    visible = _visible_base_ids(user)
    out: list[dict[str, Any]] = []
    for d in docs:
        bid = d.base_id or default_base.id
        base = bases.get(bid)
        if base is None:
            continue
        if visible is not None and bid not in visible:
            continue
        if base_id and bid != base_id:
            continue
        out.append(d.model_dump(mode="json"))
    return out


@app.get("/api/knowledge/bases")
async def list_knowledge_bases(user: PublicUser = Depends(require_user)) -> list[dict[str, Any]]:
    """知识库列表（按用户可见性过滤），附每个库的文档数。"""
    bstore = get_knowledge_bases()
    default_base = bstore.ensure_default()
    visible = _visible_base_ids(user)
    counts: dict[str, int] = {}
    for d in get_knowledge().list():
        key = d.base_id or default_base.id
        counts[key] = counts.get(key, 0) + 1
    return [
        {**b.model_dump(mode="json"), "doc_count": counts.get(b.id, 0)}
        for b in bstore.list()
        if visible is None or b.id in visible
    ]


@app.post("/api/knowledge/bases")
async def create_knowledge_base(
    req: CreateKnowledgeBaseRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    """创建知识库：global 仅 admin；task 必须关联任务且校验任务访问权限。"""
    if not req.name.strip():
        raise HTTPException(400, "知识库名称不能为空")
    if req.scope == "global" and user.role != "admin":
        raise HTTPException(403, "全局共享知识库仅管理员可创建")
    if req.scope == "task":
        if not req.task_id:
            raise HTTPException(400, "任务知识库必须关联任务")
        ini = orch().get(req.task_id)
        if ini is None:
            raise HTTPException(404, "任务不存在")
        _ensure_mission_access(ini, user)
    base = get_knowledge_bases().create(req, user_id=user.id, user_name=user.display_name)
    return {**base.model_dump(mode="json"), "doc_count": 0}


@app.patch("/api/knowledge/bases/{base_id}")
async def update_knowledge_base(
    base_id: str, req: UpdateKnowledgeBaseRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        base = get_knowledge_bases().get(base_id)
    except KeyError:
        raise HTTPException(404, "knowledge base not found") from None
    _ensure_base_write(user, base)
    return get_knowledge_bases().update(base_id, req).model_dump(mode="json")


@app.delete("/api/knowledge/bases/{base_id}")
async def delete_knowledge_base(
    base_id: str, user: PublicUser = Depends(require_user)
) -> dict[str, str]:
    try:
        base = get_knowledge_bases().get(base_id)
    except KeyError:
        raise HTTPException(404, "knowledge base not found") from None
    _ensure_base_write(user, base)
    remaining = [d for d in get_knowledge().list() if d.base_id == base_id]
    if remaining:
        raise HTTPException(400, f"库内还有 {len(remaining)} 篇文档，请先清空后再删除")
    get_knowledge_bases().delete(base_id)
    return {"status": "deleted"}


_IMPORT_ALLOWED_EXTS = {
    ".md", ".markdown", ".txt", ".csv",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".pdf",
}
_IMPORT_TEXT_EXTS = {".md", ".markdown", ".txt", ".csv"}
# local 模式可用纯 Python 库解析的 OOXML 格式（旧版二进制 .doc/.ppt/.xls 不支持）
_IMPORT_OFFICE_EXTS = {".docx", ".pptx", ".xlsx"}
_IMPORT_MAX_BYTES = 50 * 1024 * 1024


def _extract_pdf_text(raw: bytes) -> str:
    """local 后端的 PDF 文本抽取（pypdf 可选依赖）；扫描件/空文档返回空串由调用方报错。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise HTTPException(
            400, "服务端缺少 pypdf，无法解析 PDF：请在后端环境执行 pip install pypdf，或配置 WeKnora"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 — 加密/损坏的 PDF 转为用户可读的 400
        logger.warning("PDF 解析失败: %s", exc)
        raise HTTPException(400, f"PDF 解析失败（文件可能损坏或已加密）：{exc}") from exc
    return "\n\n".join(p for p in pages if p.strip()).strip()


@app.post("/api/knowledge/bases/{base_id}/import")
async def import_knowledge_file(
    base_id: str,
    file: UploadFile = File(...),
    user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    """multipart 文件导入知识库：weknora 模式转交企业库解析；local 模式仅收文本格式。"""
    try:
        base = get_knowledge_bases().get(base_id)
    except KeyError:
        raise HTTPException(404, "knowledge base not found") from None
    _ensure_base_write(user, base)

    filename = file.filename or "import.md"
    ext = Path(filename).suffix.lower()
    if ext not in _IMPORT_ALLOWED_EXTS:
        raise HTTPException(400, f"不支持的文件格式：{ext or '（无扩展名）'}")
    raw = await file.read()
    if len(raw) > _IMPORT_MAX_BYTES:
        raise HTTPException(400, "文件超过 50MB 上限")

    title = Path(filename).stem.strip() or filename
    # 任务知识库的文档自动补 ini:<id> 标签，保证检索时任务命名空间 boost 继续生效
    tags: list[str] = [f"ini:{base.scope_id}"] if base.scope == "task" else []

    gw = get_knowledge_gateway()
    if ext in _IMPORT_TEXT_EXTS or (ext == ".pdf" and not gw.uses_weknora):
        if ext == ".pdf":
            body = _extract_pdf_text(raw)
            if not body:
                raise HTTPException(
                    400, "未能从 PDF 中提取到文本（可能是纯扫描件）：扫描版 PDF 请配置 WeKnora（OCR）解析"
                )
        else:
            body = raw.decode("utf-8", errors="replace")
        doc = get_knowledge().create(
            CreateKnowledgeRequest(
                title=title, summary="", body=body, tags=tags,
                category="reference", base_id=base.id,
            ),
            author_id=user.id,
            author_name=user.display_name,
        )
        return doc.model_dump(mode="json")

    # local 模式 Office 解析：docx/pptx/xlsx 用纯 Python 库抽取正文（doc_extract.py）
    if ext in _IMPORT_OFFICE_EXTS and not gw.uses_weknora:
        try:
            body = extract_office_text(ext, raw)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not body:
            raise HTTPException(400, f"未能从 {ext} 文件中提取到文本内容（可能是纯图片/空白文档）")
        doc = get_knowledge().create(
            CreateKnowledgeRequest(
                title=title, summary="", body=body, tags=tags,
                category="reference", base_id=base.id,
            ),
            author_id=user.id,
            author_name=user.display_name,
        )
        return doc.model_dump(mode="json")

    if not gw.uses_weknora:
        raise HTTPException(
            400,
            "当前知识库后端为 local，不支持旧版二进制 .doc/.ppt/.xls 格式："
            "请另存为 docx/pptx/xlsx 后重新导入，或配置 WeKnora（TDT_KB_BACKEND=weknora）解析",
        )
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    try:
        tmp.write(raw)
        tmp.close()
        doc = gw.import_doc(
            ImportKnowledgeRequest(
                title=title,
                file_path=tmp.name,
                folder_path=f"bases/{base.id}",
                tags=tags,
                category="reference",
                base_id=base.id,
            ),
            author_id=user.id,
            author_name=user.display_name,
        )
    except (WeKnoraError, ValueError) as exc:
        logger.warning("知识库文件导入失败 base=%s file=%s: %s", base_id, filename, exc)
        raise HTTPException(400, str(exc)) from exc
    finally:
        with suppress(OSError):
            Path(tmp.name).unlink(missing_ok=True)
    return doc.model_dump(mode="json")


@app.get("/api/knowledge/status")
async def knowledge_status(_user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    return get_knowledge_gateway().status()


@app.get("/api/knowledge/folders")
async def knowledge_folders(_user: PublicUser = Depends(require_user)) -> Any:
    try:
        return get_knowledge_gateway().list_folders()
    except WeKnoraError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/knowledge/retrieve")
async def knowledge_retrieve(
    body: dict[str, Any],
    _user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    query = str(body.get("query") or "").strip()
    limit = int(body.get("limit") or 6)
    try:
        hits = get_knowledge_gateway().retrieve(query, limit=limit)
    except WeKnoraError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        "backend": get_knowledge_gateway().status()["backend"],
        "hits": [h.model_dump(mode="json") for h in hits],
    }


@app.post("/api/knowledge/sync")
async def knowledge_sync(user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    try:
        return get_knowledge_gateway().sync_catalog(author_name=user.display_name or "WeKnora")
    except WeKnoraError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/knowledge/import")
async def knowledge_import(
    req: ImportKnowledgeRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        doc = get_knowledge_gateway().import_doc(
            req, author_id=user.id, author_name=user.display_name
        )
    except (WeKnoraError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return doc.model_dump(mode="json")


@app.post("/api/knowledge/bases/{base_id}/refresh-status")
async def refresh_knowledge_parse_status(
    base_id: str, user: PublicUser = Depends(require_user)
) -> list[dict[str, Any]]:
    """刷新库内文档的 WeKnora 解析状态（前端轮询用；只需库可见性，不需写权限）。"""
    try:
        base = get_knowledge_bases().get(base_id)
    except KeyError:
        raise HTTPException(404, "knowledge base not found") from None
    visible = _visible_base_ids(user)
    if visible is not None and base.id not in visible:
        raise HTTPException(403, "无权访问该知识库")
    try:
        docs = get_knowledge_gateway().refresh_parse_status(base.id)
    except WeKnoraError as exc:
        logger.warning("解析状态刷新失败 base=%s: %s", base_id, exc)
        raise HTTPException(502, str(exc)) from exc
    return [d.model_dump(mode="json") for d in docs]


@app.get("/api/knowledge/{doc_id}")
async def get_knowledge_doc(doc_id: str, _user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    try:
        return get_knowledge_gateway().get(doc_id).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "document not found") from None


@app.post("/api/knowledge")
async def create_knowledge(
    req: CreateKnowledgeRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    """手动建文档：校验目标库写权限；任务库自动补 ini:<id> 标签（检索命名空间）。"""
    default_base = get_knowledge_bases().ensure_default()
    try:
        base = get_knowledge_bases().get(req.base_id or default_base.id)
    except KeyError:
        raise HTTPException(404, "knowledge base not found") from None
    _ensure_base_write(user, base)
    tags = list(req.tags)
    task_tag = f"ini:{base.scope_id}"
    if base.scope == "task" and task_tag not in tags:
        tags.append(task_tag)
    req = req.model_copy(update={"base_id": base.id, "tags": tags})
    try:
        doc = get_knowledge_gateway().create_local(
            req, author_id=user.id, author_name=user.display_name
        )
    except WeKnoraError as exc:
        raise HTTPException(502, str(exc)) from exc
    return doc.model_dump(mode="json")


@app.patch("/api/knowledge/{doc_id}")
async def update_knowledge(
    doc_id: str, req: UpdateKnowledgeRequest, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        return get_knowledge_gateway().update(doc_id, req).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "document not found") from None


@app.delete("/api/knowledge/{doc_id}")
async def delete_knowledge(
    doc_id: str, user: PublicUser = Depends(require_user)
) -> dict[str, str]:
    try:
        doc = get_knowledge_gateway().get(doc_id)
        if user.role != "admin" and doc.author_id and doc.author_id != user.id:
            raise HTTPException(403, "only author or admin can delete")
        get_knowledge_gateway().delete(doc_id)
        return {"status": "deleted"}
    except KeyError:
        raise HTTPException(404, "document not found") from None


@app.get("/api/memory")
async def list_memory(
    scope: str | None = Query(default=None),
    scope_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    # include_inactive：显式查看 superseded/expired 条目（演变追溯），默认仅生效条目
    return [
        m.model_dump(mode="json")
        for m in get_memory().list(
            scope=scope, scope_id=scope_id, q=q, user_id=user.id,
            include_inactive=include_inactive,
        )
    ]


@app.post("/api/prompts/enhance")
async def enhance_prompt(
    req: EnhancePromptRequest, user: PublicUser = Depends(require_user)
) -> dict[str, str]:
    """按场景用 LLM 优化用户输入框中的提示词，前端「增强提示词」按钮专用。"""
    from sortie_deck.prompt_enhance import enhance_prompt as _enhance

    try:
        enhanced = await _enhance(req)
    except RuntimeError as exc:
        if str(exc) == "LLM_NOT_CONFIGURED":
            raise HTTPException(409, "LLM 未配置，无法优化提示词") from None
        logger.warning(
            "提示词优化失败: scene=%s len=%d err=%s: %s", req.scene, len(req.text), type(exc).__name__, exc
        )
        raise HTTPException(502, "提示词优化失败，请稍后重试") from None
    except Exception as exc:  # noqa: BLE001 — LLM 网络等异常统一转 502
        # err 里带上异常类型：httpx.ReadTimeout 等异常 message 为空，只有类型可定位
        logger.warning(
            "提示词优化失败: scene=%s len=%d err=%s: %s", req.scene, len(req.text), type(exc).__name__, exc
        )
        raise HTTPException(502, "提示词优化失败，请稍后重试") from None
    return {"text": enhanced}


@app.post("/api/memory")
async def create_memory(
    req: CreateMemoryRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    # workspace 全员共享，写入影响所有任务，仅管理员可创建
    if req.scope == "workspace" and user.role != "admin":
        raise HTTPException(403, "workspace 记忆仅管理员可创建")
    entry = get_memory().create(req, author_id=user.id, author_name=user.display_name)

    # 有 LLM 环境时简介标记为生成中，异步生成完成后落 done / failed；
    # 无 LLM 环境保持空状态，前端直接展示 content 截断兜底
    def _llm_enabled() -> bool:
        try:
            from sortie_deck.llm import LlmClient

            return LlmClient(timeout=15.0).enabled
        except Exception:  # noqa: BLE001 — 状态判定失败按无 LLM 处理
            return False

    if _llm_enabled():
        entry = get_memory().set_summary_status(entry.id, "pending")

    # 异步生成一句话简介（Claude Code MEMORY.md 模式的索引行）；失败降级为
    # content 截断（display_summary 兜底），绝不阻塞写入响应
    async def _fill_summary(entry_id: str, content: str) -> None:
        try:
            from sortie_deck.llm import LlmClient

            llm = LlmClient(timeout=15.0)
            if not llm.enabled:
                get_memory().set_summary_status(entry_id, "failed")
                return  # 无 LLM 环境保持截断简介
            data = await llm.chat_json(
                system=(
                    "为一条 Agent 记忆生成不超过 30 字的中文简介，"
                    "保留约束与适用场景，直接给出简介文本，JSON 格式 {\"summary\": \"...\"}。"
                ),
                user=content,
            )
            s = str(data.get("summary") or "").strip()
            if s:
                get_memory().set_summary(entry_id, s)
                return
            # 模型返回了空简介：标记失败让用户可手动重试
            get_memory().set_summary_status(entry_id, "failed")
        except Exception:  # noqa: BLE001 — 简介是增强项，失败保持截断
            logger.warning("记忆简介生成失败，标记 failed 可重试: id=%s", entry_id, exc_info=True)
            try:
                get_memory().set_summary_status(entry_id, "failed")
            except KeyError:
                pass

    if _llm_enabled():
        asyncio.create_task(_fill_summary(entry.id, entry.content))
    return entry.model_dump(mode="json")


class MemoryLookupRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


@app.post("/api/memory/lookup")
async def lookup_memories(
    req: MemoryLookupRequest, user: PublicUser = Depends(require_user)
) -> list[dict[str, Any]]:
    """按 id 批量取记忆（任务详情页「记忆」面板用）。

    可见性与 /api/memory 列表一致：他人 user 域私密记忆不返回。
    """
    if not req.ids:
        return []
    known = {e.id: e for e in get_memory().list(user_id=user.id)}
    return [
        known[i].model_dump(mode="json")
        for i in req.ids
        if i in known
    ]


@app.post("/api/memory/{mem_id}/summary/regenerate")
async def regenerate_memory_summary(
    mem_id: str, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    """重新生成记忆简介：置 pending 并异步调用 LLM，返回最新 entry 供前端展示 loading。"""
    try:
        entry = get_memory().get(mem_id)
        # 权限与 PATCH 一致：workspace 收敛管理员，user 域仅本人或管理员
        if entry.scope == "workspace" and user.role != "admin":
            raise HTTPException(403, "workspace 记忆仅管理员可修改")
        if entry.scope == "user" and entry.scope_id != user.id and user.role != "admin":
            raise HTTPException(403, "forbidden")
    except KeyError:
        raise HTTPException(404, "memory not found") from None
    from sortie_deck.llm import LlmClient

    llm = LlmClient(timeout=15.0)
    if not llm.enabled:
        raise HTTPException(409, "LLM 未配置，无法生成简介")
    get_memory().set_summary_status(mem_id, "pending")

    async def _fill(entry_id: str, content: str) -> None:
        try:
            data = await llm.chat_json(
                system=(
                    "为一条 Agent 记忆生成不超过 30 字的中文简介，"
                    "保留约束与适用场景，直接给出简介文本，JSON 格式 {\"summary\": \"...\"}。"
                ),
                user=content,
            )
            s = str(data.get("summary") or "").strip()
            if s:
                get_memory().set_summary(entry_id, s)
                return
            get_memory().set_summary_status(entry_id, "failed")
        except Exception:  # noqa: BLE001 — 简介是增强项，失败保持截断
            logger.warning("记忆简介重试失败，标记 failed: id=%s", entry_id, exc_info=True)
            try:
                get_memory().set_summary_status(entry_id, "failed")
            except KeyError:
                pass

    asyncio.create_task(_fill(mem_id, entry.content))
    return get_memory().get(mem_id).model_dump(mode="json")


@app.patch("/api/memory/{mem_id}")
async def update_memory(
    mem_id: str, req: UpdateMemoryRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        entry = get_memory().get(mem_id)
        # workspace 为全局共享数据，修改/删除收敛到管理员，防止非管理员篡改全员约定
        if entry.scope == "workspace" and user.role != "admin":
            raise HTTPException(403, "workspace 记忆仅管理员可修改")
        if entry.scope == "user" and entry.scope_id != user.id and user.role != "admin":
            raise HTTPException(403, "forbidden")
        return get_memory().update(mem_id, req).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "memory not found") from None


@app.delete("/api/memory/{mem_id}")
async def delete_memory(mem_id: str, user: PublicUser = Depends(require_user)) -> dict[str, str]:
    try:
        entry = get_memory().get(mem_id)
        # workspace 为全局共享数据，删除收敛到管理员（与创建/修改一致）
        if entry.scope == "workspace" and user.role != "admin":
            raise HTTPException(403, "workspace 记忆仅管理员可删除")
        if (
            user.role != "admin"
            and entry.author_id
            and entry.author_id != user.id
            and not (entry.scope == "user" and entry.scope_id == user.id)
        ):
            raise HTTPException(403, "forbidden")
        get_memory().delete(mem_id)
        return {"status": "deleted"}
    except KeyError:
        raise HTTPException(404, "memory not found") from None


@app.get("/api/avatars/presets")
async def avatar_presets(_user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    from sortie_deck.avatars import list_avatar_presets

    return {"presets": list_avatar_presets()}


class UpdateRoleAvatarRequest(BaseModel):
    avatar: str = ""


@app.patch("/api/initiatives/{initiative_id}/roles/{role_id}")
async def patch_role_avatar(
    initiative_id: str,
    role_id: str,
    req: UpdateRoleAvatarRequest,
    user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    try:
        ini = await orch().update_role_avatar(
            initiative_id, role_id, req.avatar, author_name=user.display_name
        )
        return ini.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/initiatives/{initiative_id}/roles/{role_id}/avatar")
async def upload_role_avatar(
    initiative_id: str,
    role_id: str,
    user: PublicUser = Depends(require_user),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    from sortie_deck.media import save_avatar_image

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > 4 * 1024 * 1024:
        raise HTTPException(400, "avatar too large (max 4MB)")
    content_type = (file.content_type or "").lower()
    name = file.filename or "avatar.png"
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(400, "only image uploads allowed")
    if not any(name.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
        # still allow if content-type is image/*
        if not content_type.startswith("image/"):
            raise HTTPException(400, "unsupported image type")
    url = save_avatar_image(raw, filename=name)
    try:
        ini = await orch().update_role_avatar(
            initiative_id, role_id, url, author_name=user.display_name
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"avatar": url, "initiative": ini.model_dump(mode="json")}


@app.get("/api/media/avatars/{filename}")
async def get_uploaded_avatar(filename: str) -> FileResponse:
    from sortie_deck.media import avatars_dir

    safe = Path(filename).name
    path = avatars_dir() / safe
    if not path.is_file():
        raise HTTPException(404, "not found")
    media = "image/webp" if path.suffix.lower() == ".webp" else "image/png"
    return FileResponse(path, media_type=media)


@app.post("/api/agents/{agent_id}/avatar")
async def upload_agent_avatar(
    agent_id: str,
    user: PublicUser = Depends(require_user),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    from sortie_deck.media import save_avatar_image

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > 4 * 1024 * 1024:
        raise HTTPException(400, "avatar too large (max 4MB)")
    name = file.filename or "avatar.png"
    content_type = (file.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(400, "only image uploads allowed")
    url = save_avatar_image(raw, filename=name)
    try:
        from sortie_deck.agents import UpdateAgentRequest

        agent = get_agents().update(
            agent_id,
            UpdateAgentRequest(avatar=url),
            editor_id=user.id,
            is_admin=user.role == "admin",
        )
    except KeyError as exc:
        raise HTTPException(404, "agent not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {"avatar": url, "agent": agent.model_dump(mode="json")}


@app.post("/api/media/avatars")
async def upload_avatar_temp(
    user: PublicUser = Depends(require_user),
    file: UploadFile = File(...),
) -> dict[str, str]:
    """Upload avatar before agent create; returns URL to embed in create payload."""
    from sortie_deck.media import save_avatar_image

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > 4 * 1024 * 1024:
        raise HTTPException(400, "avatar too large (max 4MB)")
    url = save_avatar_image(raw, filename=file.filename or "avatar.png")
    return {"avatar": url}


def _persona_spec_view(payload: dict[str, Any]) -> dict[str, Any]:
    """补齐 persona_spec：旧数据只有自由文本时，按需解析一份只读结构化视图。"""
    if not payload.get("persona_spec"):
        spec = parse_persona(str(payload.get("persona") or ""))
        if spec.role or spec.skills or spec.constraints:
            payload["persona_spec"] = spec.model_dump(mode="json")
    return payload


def _agent_json(agent: Any) -> dict[str, Any]:
    return _persona_spec_view(agent.model_dump(mode="json"))


@app.get("/api/agents")
async def list_agents(
    q: str | None = Query(default=None),
    slot: str | None = Query(default=None),
    user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    return [
        _agent_json(a)
        for a in get_agents().list(q=q, slot=slot, user_id=user.id)
    ]


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str, _user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    try:
        return _agent_json(get_agents().get(agent_id))
    except KeyError:
        raise HTTPException(404, "agent not found") from None


@app.post("/api/agents")
async def create_agent(
    req: CreateAgentRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        agent = get_agents().create(req, author_id=user.id, author_name=user.display_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _agent_json(agent)


@app.patch("/api/agents/{agent_id}")
async def update_agent(
    agent_id: str, req: UpdateAgentRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        agent = get_agents().update(
            agent_id, req, editor_id=user.id, is_admin=user.role == "admin"
        )
    except KeyError:
        raise HTTPException(404, "agent not found") from None
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _agent_json(agent)


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str, user: PublicUser = Depends(require_user)) -> dict[str, str]:
    try:
        get_agents().delete(agent_id, editor_id=user.id, is_admin=user.role == "admin")
        return {"status": "deleted"}
    except KeyError:
        raise HTTPException(404, "agent not found") from None
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


class CreateRoleRequest(BaseModel):
    role_key: str
    title: str = ""
    summary: str = ""
    coding: bool = False
    executor: str | None = None
    handoff_targets: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ForgeProposeRequest(BaseModel):
    capability: str
    title: str = ""
    role_key: str | None = None
    summary: str = ""
    tags: list[str] = Field(default_factory=list)


class ForgeUpdateRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    persona: str | None = None
    # 结构化人设：给了就以它为准重新编译 persona（六段式）
    persona_spec: dict[str, Any] | None = None
    role_key: str | None = None
    tags: list[str] | None = None
    contract: dict[str, Any] | None = None


class ForgeApproveRequest(BaseModel):
    publish_agent: bool = False
    coding: bool = False
    toolkit_ids: list[str] = Field(default_factory=list)


@app.get("/api/roles")
async def list_roles(
    q: str | None = Query(default=None),
    source: str | None = Query(default=None),
    _user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    return [s.model_dump(mode="json") for s in get_role_registry().list(q=q, source=source)]


@app.post("/api/roles")
async def create_role(
    req: CreateRoleRequest, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    key = normalize_role_key(req.role_key) or req.role_key
    try:
        spec = get_role_registry().register(
            RoleSpec(
                role_key=key,
                title=req.title.strip() or key,
                summary=req.summary.strip(),
                source="custom",
                coding=req.coding,
                executor=(req.executor or None),
                handoff_targets=list(req.handoff_targets),
                tags=[t.strip() for t in req.tags if t.strip()],
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return spec.model_dump(mode="json")


@app.delete("/api/roles/{role_key}")
async def delete_role(role_key: str, _user: PublicUser = Depends(require_user)) -> dict[str, str]:
    try:
        get_role_registry().delete(role_key)
    except KeyError:
        raise HTTPException(404, "role not found") from None
    return {"status": "deleted"}


def _forge_draft_json(draft: Any) -> dict[str, Any]:
    return _persona_spec_view(draft.model_dump(mode="json"))


@app.get("/api/forge/drafts")
async def list_forge_drafts(
    status: str | None = Query(default=None),
    _user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    return [_forge_draft_json(d) for d in get_agent_forge().list(status=status)]


@app.post("/api/forge/drafts")
async def propose_forge_draft(
    req: ForgeProposeRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    draft = await get_agent_forge().propose(
        ForgeRequest(**req.model_dump(), requested_by=user.display_name)
    )
    return _forge_draft_json(draft)


@app.get("/api/forge/drafts/{draft_id}")
async def get_forge_draft(
    draft_id: str, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        return _forge_draft_json(get_agent_forge().get(draft_id))
    except KeyError:
        raise HTTPException(404, "draft not found") from None


@app.patch("/api/forge/drafts/{draft_id}")
async def update_forge_draft(
    draft_id: str, req: ForgeUpdateRequest, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        draft = get_agent_forge().update(draft_id, req.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(404, "draft not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _forge_draft_json(draft)


@app.post("/api/forge/drafts/{draft_id}/approve")
async def approve_forge_draft(
    draft_id: str, req: ForgeApproveRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        draft, role_spec, published = get_agent_forge().approve(
            draft_id,
            reviewer=user.display_name,
            publish_agent=req.publish_agent,
            toolkit_ids=list(req.toolkit_ids),
            coding=req.coding,
        )
    except KeyError:
        raise HTTPException(404, "draft not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "draft": _forge_draft_json(draft),
        "role": role_spec.model_dump(mode="json") if role_spec else None,
        "agent": _agent_json(published) if published else None,
    }


@app.post("/api/forge/drafts/{draft_id}/reject")
async def reject_forge_draft(
    draft_id: str,
    note: str = Query(default=""),
    user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    try:
        draft = get_agent_forge().reject(draft_id, note=note, reviewer=user.display_name)
    except KeyError:
        raise HTTPException(404, "draft not found") from None
    return draft.model_dump(mode="json")


@app.get("/api/toolkit")
async def list_toolkit(
    kind: str | None = Query(default=None),
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
    featured: bool | None = Query(default=None),
    _user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    return [
        i.model_dump(mode="json")
        for i in get_toolkit().list(kind=kind, category=category, q=q, featured=featured)
    ]


@app.get("/api/toolkit/mounts")
async def toolkit_mounts(_user: PublicUser = Depends(require_user)) -> dict[str, int]:
    """各工具条目被多少个 Agent 挂载（基于 agents 的 toolkit_ids 统计）。

    注意：必须注册在 /api/toolkit/{item_id} 之前，否则 "mounts" 会被当作 item_id。
    """
    counts: dict[str, int] = {}
    for agent in get_agents().list():
        for tid in agent.toolkit_ids or []:
            counts[tid] = counts.get(tid, 0) + 1
    return counts


@app.get("/api/toolkit/{item_id}")
async def get_toolkit_item(item_id: str, _user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    try:
        return get_toolkit().get(item_id).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "toolkit item not found") from None


@app.post("/api/toolkit")
async def create_toolkit_item(
    req: CreateToolkitRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    return get_toolkit().create(req, author_id=user.id, author_name=user.display_name).model_dump(
        mode="json"
    )


@app.patch("/api/toolkit/{item_id}")
async def update_toolkit_item(
    item_id: str, req: UpdateToolkitRequest, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        return get_toolkit().update(item_id, req).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "toolkit item not found") from None


@app.delete("/api/toolkit/{item_id}")
async def delete_toolkit_item(
    item_id: str, _user: PublicUser = Depends(require_user)
) -> dict[str, str]:
    try:
        get_toolkit().delete(item_id)
        return {"status": "deleted"}
    except KeyError:
        raise HTTPException(404, "toolkit item not found") from None


@app.post("/api/toolkit/import")
async def import_toolkit(
    req: ImportToolkitRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    from sortie_deck.org_policy import assert_import_allowed

    try:
        assert_import_allowed(orch().org_policy)
        items = get_toolkit().import_payload(
            req, author_id=user.id, author_name=user.display_name
        )
        return {
            "imported": len(items),
            "items": [i.model_dump(mode="json") for i in items],
        }
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, json.JSONDecodeError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/toolkit/reindex")
async def reindex_toolkit_index(_user: PublicUser = Depends(require_admin)) -> dict[str, Any]:
    """全量重建工具箱的 WeKnora 镜像索引（方案 A：混合检索 + rerank）。

    本地 toolkit.json 为准：多退少补。backend 未开 / KB 未配置时返回 skipped。
    """
    from sortie_deck.toolkit_index import sync_all

    try:
        return sync_all(get_toolkit())
    except Exception as exc:  # noqa: BLE001 — WeKnora 异常转 502 便于前端提示
        raise HTTPException(502, f"reindex failed: {exc}") from exc


# ---------------------------------------------------------------------------
# V4 工具箱：MCP 测试连接 / 技能安装 / 市场精选 / skill-install 优化 / 挂载计数
# ---------------------------------------------------------------------------


class McpTestRequest(BaseModel):
    """连接测试入参：与 ToolkitItem 的 mcp 字段对齐，config 走 bearer_token/headers。"""

    transport: Literal["stdio", "sse", "http"] = "stdio"
    url: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class InstallSkillRequest(BaseModel):
    url: str
    category: str = ""
    load: Literal["always", "defer"] = "defer"


class OptimizeSkillRequest(BaseModel):
    name: str
    category: str = ""
    body: str


class ConfirmSkillDraftRequest(BaseModel):
    keep: Literal["original", "optimized"] = "optimized"
    load: Literal["always", "defer"] = "defer"


class MarketInstallRequest(BaseModel):
    key: str


def _skill_draft_store():
    from sortie_deck.skill_install import SkillDraftStore

    global _skill_drafts
    if _skill_drafts is None:
        _skill_drafts = SkillDraftStore(settings.data_dir / "skill_drafts.json")
    return _skill_drafts


_skill_drafts: Any = None


@app.post("/api/toolkit/mcp/test")
async def test_mcp_connector(
    req: McpTestRequest, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    """连接器连通性测试：按 transport 建临时会话，initialize + tools/list 后立即关闭。"""
    from sortie_deck.mcp_runtime import _create_session
    from sortie_deck.toolkit import ToolkitItem

    url = req.url.strip()
    command = req.command.strip()
    # 显式校验：避免空 url 的 http/sse 落回 stdio 分支、空命令静默跳过
    if req.transport in {"sse", "http"} and not url:
        return {"ok": False, "error": "缺少端点 URL", "log": []}
    if req.transport == "stdio" and not command:
        return {"ok": False, "error": "缺少启动命令", "log": []}
    item = ToolkitItem(
        id="tk_test",
        kind="mcp",
        name="connector-test",
        transport=req.transport,
        url=url,
        command=command,
        args=list(req.args),
        env=dict(req.env),
        config=dict(req.config),
    )
    session = _create_session(item)
    try:
        await asyncio.wait_for(session.start(), timeout=15)
        tools = [t.name for t in session.tools]
        return {"ok": True, "tools": tools, "log": list(session.log)}
    except Exception as exc:  # noqa: BLE001 — 任何连接失败都转成结构化结果而非 500
        log = getattr(session, "log", [])
        logger.warning("mcp test failed (transport=%s url=%s): %s", req.transport, req.url, exc)
        return {"ok": False, "error": str(exc), "log": list(log)}
    finally:
        with suppress(Exception):
            await session.close()


@app.post("/api/toolkit/install_skill")
async def install_skill_from_web(
    req: InstallSkillRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    """从 GitHub 仓库一键安装技能（仅接受仓库地址，不支持 MD 直链）。"""
    from sortie_deck.skill_install import install_from_github

    try:
        item_req = await install_from_github(req.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    if req.category.strip():
        item_req.category = req.category.strip()
    item_req.load = req.load
    item = get_toolkit().create(item_req, author_id=user.id, author_name=user.display_name)
    return item.model_dump(mode="json")


@app.get("/api/market/skills")
async def list_market_skills(_user: PublicUser = Depends(require_user)) -> list[dict[str, Any]]:
    """市场精选目录：内置静态数据 + 按 store 实时标注 installed。"""
    from sortie_deck.skill_install import MARKET_SKILLS

    installed = {i.name for i in get_toolkit().list(kind="skill")}
    return [
        {**entry, "installed": entry["name"] in installed}
        for entry in MARKET_SKILLS
    ]


@app.post("/api/market/skills/install")
async def install_market_skill(
    req: MarketInstallRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    """安装市场精选技能：已存在时直接返回既有条目（幂等）。"""
    from sortie_deck.skill_install import market_request

    try:
        item_req = market_request(req.key)
    except KeyError:
        raise HTTPException(404, f"market skill not found: {req.key}") from None
    existing = [
        i
        for i in get_toolkit().list(kind="skill")
        if i.name == item_req.name
    ]
    if existing:
        return existing[0].model_dump(mode="json")
    item = get_toolkit().create(item_req, author_id=user.id, author_name=user.display_name)
    return item.model_dump(mode="json")


@app.post("/api/toolkit/optimize_skill")
async def optimize_skill(
    req: OptimizeSkillRequest, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    """手写 SKILL.md：后台调用 skill-install 优化，双版本草稿落盘待用户选版。"""
    from sortie_deck.models import utc_now
    from sortie_deck.skill_install import SkillDraft, optimize_skill_md

    if not req.name.strip() or not req.body.strip():
        raise HTTPException(400, "name 与 body 均不能为空")
    optimized, backend = await optimize_skill_md(req.name.strip(), req.body.strip())
    draft = SkillDraft(
        name=req.name.strip(),
        category=req.category.strip(),
        original=req.body.strip(),
        optimized=optimized,
        backend=backend,
        created_at=utc_now().isoformat(),
    )
    _skill_draft_store().save(draft)
    return draft.model_dump(mode="json")


@app.post("/api/toolkit/optimize_skill/{draft_id}/confirm")
async def confirm_skill_draft(
    draft_id: str, req: ConfirmSkillDraftRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    """选版确认：以保留版本创建技能条目，并删除草稿（未选版本从服务器本地存储清除）。"""
    from sortie_deck.skill_install import SkillDraftStore

    store: SkillDraftStore = _skill_draft_store()
    try:
        draft = store.get(draft_id)
    except KeyError:
        raise HTTPException(404, "draft not found") from None
    body = draft.optimized if req.keep == "optimized" else draft.original
    summary = ""
    meta_line = next(
        (ln for ln in body.splitlines() if ln.lower().startswith("description:")), ""
    )
    if meta_line:
        summary = meta_line.split(":", 1)[1].strip()
    item = get_toolkit().create(
        CreateToolkitRequest(
            kind="skill",
            name=draft.name,
            summary=summary[:120],
            body=body,
            category=draft.category,
            tags=["skill", "handwritten"],
            source="handwritten" if req.keep == "original" else f"handwritten+skill-install({draft.backend})",
            load=req.load,
        ),
        author_id=user.id,
        author_name=user.display_name,
    )
    # 未选版本随草稿一并从本地存储删除
    store.delete(draft_id)
    return item.model_dump(mode="json")


@app.get("/api/executors")
async def list_executors(_user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    from sortie_deck.plugins.entrypoints import probe_executor

    registry = orch().registry
    probes = [probe_executor(registry.get(name)) for name in registry.names()]
    return {
        "executors": registry.names(),
        "probes": probes,
        "deploy_executor": settings.deploy_executor,
    }


@app.get("/api/harnesses")
async def list_harnesses(refresh: bool = Query(default=False)) -> dict[str, Any]:
    """Available agent harness providers + probe status (TTL-cached)."""
    from sortie_deck.harness_providers import (
        HARNESS_LABELS,
        get_harness_registry,
    )

    registry = get_harness_registry()
    items: list[dict[str, Any]] = []
    for name in registry.names():
        probe = registry.probe(name, refresh=refresh)
        items.append(
            {
                "name": name,
                "label": HARNESS_LABELS.get(name, name),
                "probe": probe.as_dict(),
                "available": probe.ok,
            }
        )
    return {"harnesses": items, "default": settings.default_harness}


class UpdateHarnessRequest(BaseModel):
    harness: str


@app.patch("/api/initiatives/{initiative_id}/harness")
async def patch_initiative_harness(
    initiative_id: str,
    req: UpdateHarnessRequest,
    _user: PublicUser = Depends(_mission_control_guard),
) -> dict[str, Any]:
    """Draft-only harness change; started missions require HITL switch_harness."""
    try:
        ini = await orch().update_harness(initiative_id, req.harness)
        return ini.model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


class UpdateMaterialsRequest(BaseModel):
    materials: list[ExternalMaterial] = Field(default_factory=list)


@app.patch("/api/initiatives/{initiative_id}/materials")
async def patch_initiative_materials(
    initiative_id: str,
    req: UpdateMaterialsRequest,
    _user: PublicUser = Depends(_mission_control_guard),
) -> dict[str, Any]:
    """Replace a mission's external materials (recomposes the brief).

    Pinned materials ride along as persistent context in every stage prompt.
    """
    try:
        ini = orch().set_materials(initiative_id, req.materials)
        return ini.model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None


@app.get("/api/pipelines")
async def list_pipelines(_user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    return {"tracks": list_tracks(), "catalog": stage_catalog()}


@app.get("/api/pipelines/suggest")
async def suggest_pipeline(
    brief: str = Query(default=""),
    title: str = Query(default=""),
    _user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    from sortie_deck.pipeline_llm import plan_pipeline_smart

    proposal = await plan_pipeline_smart(
        title=title, brief=brief, use_llm=bool(settings.pipeline_llm)
    )
    return {
        "track": proposal.get("track_hint"),
        "reason": proposal.get("rationale"),
        "stages": proposal.get("stages", []),
        "description": proposal.get("rationale", ""),
        "proposal": proposal,
    }


@app.get("/api/pipelines/catalog")
async def get_stage_catalog(_user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    return {"stages": stage_catalog()}


@app.get("/api/initiatives")
async def list_initiatives(
    status: str | None = Query(default=None),
    user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    items = _visible_initiatives(orch().list_initiatives(), user)
    if status:
        items = [i for i in items if i.status.value == status]
    return [i.model_dump(mode="json") for i in items]


@app.post("/api/initiatives")
async def create_initiative(
    req: CreateInitiativeRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    if not req.creator_name or req.creator_name == "Operator":
        req.creator_name = user.display_name
    # 多租户归属：记录创建者用户 id（workspace/删除等敏感操作校验用）
    req.created_by = user.id
    ini = await orch().create_initiative(req)
    await asyncio.sleep(0.15)
    fresh = orch().get(ini.id) or ini
    return fresh.model_dump(mode="json")


class BriefingMessageBody(BaseModel):
    text: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    materials: list[ExternalMaterial] = Field(default_factory=list)


@app.post("/api/briefing/sessions")
async def start_briefing(user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    # 会话发起人即任务归属人：确认建队时写入 created_by（见 MissionBriefingAgent._commit）
    sess = get_briefing().start(owner_id=user.id)
    return sess.model_dump(mode="json")


@app.get("/api/briefing/sessions/{session_id}")
async def get_briefing_session(
    session_id: str, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        return get_briefing().store.get(session_id).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "briefing session not found") from None


@app.post("/api/briefing/sessions/{session_id}/messages")
async def post_briefing_message(
    session_id: str,
    body: BriefingMessageBody,
    request: Request,
    user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    from sortie_deck.mission_briefing import BriefingAttachment

    atts = [BriefingAttachment.model_validate(a) for a in (body.attachments or [])]
    # Run planning as a task so a client abort ("stop" button) cancels it.
    task = asyncio.create_task(
        get_briefing().handle(
            session_id,
            body.text,
            user_name=user.display_name or user.username,
            user_id=user.id,
            attachments=atts,
            materials=list(body.materials or []),
        )
    )
    while True:
        done, _ = await asyncio.wait({task}, timeout=0.4)
        if done:
            break
        if await request.is_disconnected():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise HTTPException(499, "briefing interrupted") from None
    try:
        sess = task.result()
    except asyncio.CancelledError:
        raise HTTPException(499, "briefing interrupted") from None
    except KeyError:
        raise HTTPException(404, "briefing session not found") from None
    return sess.model_dump(mode="json")


class BriefingReposBody(BaseModel):
    role_repos: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


@app.patch("/api/briefing/sessions/{session_id}/repos")
async def set_briefing_repos(
    session_id: str,
    body: BriefingReposBody,
    _user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    """Attach per-slot git/github repos & branches to the pending briefing draft.

    Called before the briefing confirm; each slot's dev agent clones & reads
    these repos in its workspace before iterating.
    """
    try:
        sess = get_briefing().set_role_repos(session_id, body.role_repos)
    except KeyError:
        raise HTTPException(404, "briefing session not found") from None
    return sess.model_dump(mode="json")


@app.get("/api/initiatives/{initiative_id}")
async def get_initiative(
    initiative_id: str, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    _ensure_mission_access(ini, user)
    return ini.model_dump(mode="json")


@app.delete("/api/initiatives/{initiative_id}")
async def delete_initiative(
    initiative_id: str, user: PublicUser = Depends(require_user)
) -> dict[str, str]:
    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    # 解散：仅创建者或 admin（admin 对他人任务只读，但保留解散能力）
    _ensure_mission_delete(ini, user)
    try:
        await orch().delete_initiative(initiative_id)
        return {"status": "deleted"}
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None


class AddMissionShareRequest(BaseModel):
    # 被分享的目标用户 id（来自 /api/users/search 结果）
    user_id: str


@app.get("/api/initiatives/{initiative_id}/shares")
async def list_mission_shares(
    initiative_id: str, user: PublicUser = Depends(require_user)
) -> list[dict[str, Any]]:
    """任务共享成员列表：创建者与被共享用户均可见。"""
    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    _ensure_mission_access(ini, user)
    return [s.model_dump(mode="json") for s in ini.shared_with]


@app.post("/api/initiatives/{initiative_id}/shares")
async def add_mission_share(
    initiative_id: str,
    req: AddMissionShareRequest,
    user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    """共享任务给指定账号用户（仅创建者可操作；重复共享幂等返回）。"""
    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    _ensure_mission_share_owner(ini, user)
    target = get_auth().get_by_id(req.user_id)
    if not target:
        raise HTTPException(404, "user not found")
    if target.id == ini.created_by:
        raise HTTPException(400, "cannot share to the mission owner")
    if not _is_mission_shared_with(ini, target.id):
        ini.shared_with.append(
            MissionShare(
                user_id=target.id,
                username=target.username,
                display_name=target.display_name,
                added_by=user.id,
            )
        )
        orch().store.upsert(ini)
        logger.info("任务共享已添加: initiative=%s user=%s by=%s", initiative_id, target.id, user.id)
    return ini.model_dump(mode="json")


@app.delete("/api/initiatives/{initiative_id}/shares/{share_user_id}")
async def remove_mission_share(
    initiative_id: str,
    share_user_id: str,
    user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    """取消共享：仅创建者可移除任意成员，被共享用户可移除自己（退出共享）。"""
    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    is_owner = bool(ini.created_by and ini.created_by == user.id)
    if not is_owner and share_user_id != user.id:
        raise HTTPException(403, "仅任务创建者可移除其他共享成员")
    if _is_mission_shared_with(ini, share_user_id):
        ini.shared_with = [s for s in ini.shared_with if s.user_id != share_user_id]
        orch().store.upsert(ini)
        logger.info("任务共享已取消: initiative=%s user=%s by=%s", initiative_id, share_user_id, user.id)
    return ini.model_dump(mode="json")


@app.get("/api/initiatives/{initiative_id}/memories")
async def mission_memories(
    initiative_id: str, user: PublicUser = Depends(_mission_access_guard)
) -> dict[str, Any]:
    """任务记忆面板数据：生成时绑定的记忆 + Agent 执行中实际读取的记录。"""
    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    bound_ids = [str(i) for i in (ini.meta or {}).get("memory_ids", [])]
    known = {e.id: e for e in get_memory().list(user_id=user.id)}
    bound = [known[i].model_dump(mode="json") for i in bound_ids if i in known]
    # 执行中读取记录：Agent 取全文后追加 usage.log（best-effort，可能不存在）
    read_ids: list[str] = []
    try:
        from sortie_deck.mission_memory import get_mission_memory

        log = get_mission_memory().usage_log(initiative_id)
        if log.exists():
            read_ids = [ln.strip() for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001 — 使用记录只是增强
        read_ids = []
    return {"bound": bound, "read_ids": read_ids}


@app.post("/api/initiatives/{initiative_id}/start")
async def start_initiative(
    initiative_id: str, user: PublicUser = Depends(_mission_control_guard)
) -> dict[str, Any]:
    try:
        ini = await orch().start_pipeline(initiative_id, author_name=user.display_name)
        await asyncio.sleep(0.2)
        return (orch().get(ini.id) or ini).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None
    except ForgeReviewRequiredError as exc:
        # 409：任务本身合法，只是缺一步人工审核（审核通过后可直接重试 /start）
        raise HTTPException(
            409,
            detail={
                "code": exc.code,
                "message": exc.message,
                "draft_ids": exc.draft_ids,
            },
        ) from exc
    except PipelineNotConfirmedError as exc:
        raise HTTPException(
            400,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/initiatives/{initiative_id}/pipeline")
async def patch_pipeline(
    initiative_id: str,
    req: UpdatePipelineRequest,
    user: PublicUser = Depends(_mission_control_guard),
) -> dict[str, Any]:
    try:
        ini = await orch().update_pipeline(initiative_id, req, author_name=user.display_name)
        return ini.model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/initiatives/{initiative_id}/pipeline/confirm")
async def confirm_pipeline_route(
    initiative_id: str,
    req: ConfirmPipelineRequest | None = None,
    user: PublicUser = Depends(_mission_control_guard),
) -> dict[str, Any]:
    try:
        ini = await orch().confirm_pipeline(
            initiative_id, req or ConfirmPipelineRequest(), author_name=user.display_name
        )
        return ini.model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/initiatives/{initiative_id}/hitl")
async def submit_hitl(
    initiative_id: str, req: HitlSubmitRequest, user: PublicUser = Depends(_mission_control_guard)
) -> dict[str, Any]:
    try:
        # 「撤退」常误打到 /hitl；stop 在任意状态下都走专用路径
        if req.action == HitlAction.STOP:
            ini = await orch().stop(
                initiative_id, author_name=req.author_name or user.display_name
            )
            return ini.model_dump(mode="json")
        decision = HitlDecision(
            action=req.action,
            instruction=req.instruction,
            next_stage=req.next_stage,
            note=req.note,
            harness=req.harness,
        )
        ini = await orch().submit_hitl(
            initiative_id, decision, author_name=req.author_name or user.display_name
        )
        return ini.model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/initiatives/{initiative_id}/stop")
async def stop_initiative(
    initiative_id: str, user: PublicUser = Depends(_mission_control_guard)
) -> dict[str, Any]:
    try:
        ini = await orch().stop(initiative_id, author_name=user.display_name)
        return ini.model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None


def _squad_payload(initiative_id: str) -> dict[str, Any]:
    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    messages = orch().list_room_messages(initiative_id)
    return {
        "initiative_id": initiative_id,
        "squad_id": ini.room_id,
        "room_id": ini.room_id,
        "participants": [p.model_dump(mode="json") for p in ini.participants],
        "messages": [m.model_dump(mode="json") for m in messages],
    }


@app.get("/api/initiatives/{initiative_id}/squad")
@app.get("/api/initiatives/{initiative_id}/room")
async def get_squad(
    initiative_id: str, _user: PublicUser = Depends(_mission_access_guard)
) -> dict[str, Any]:
    return _squad_payload(initiative_id)


@app.post("/api/initiatives/{initiative_id}/squad/join")
@app.post("/api/initiatives/{initiative_id}/room/join")
async def join_squad(
    initiative_id: str, req: JoinRoomRequest, user: PublicUser = Depends(_mission_control_guard)
) -> dict[str, Any]:
    try:
        name = req.name or user.display_name
        ini = await orch().join_room(initiative_id, name, req.title or user.role)
        return ini.model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None


@app.post("/api/initiatives/{initiative_id}/squad/discuss")
@app.post("/api/initiatives/{initiative_id}/room/discuss")
async def set_squad_discussing(
    initiative_id: str,
    req: DiscussParticipateRequest,
    user: PublicUser = Depends(_mission_control_guard),
) -> dict[str, Any]:
    try:
        ini = await orch().set_discussing(
            initiative_id,
            discussing=req.discussing,
            participant_id=req.participant_id,
            role=req.role,
            name=req.name or (None if req.participant_id or req.role else user.display_name),
            actor_name=user.display_name,
        )
        return ini.model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/initiatives/{initiative_id}/squad/messages")
@app.post("/api/initiatives/{initiative_id}/room/messages")
async def post_squad_message(
    initiative_id: str,
    req: PostRoomMessageRequest,
    user: PublicUser = Depends(_mission_control_guard),
) -> dict[str, Any]:
    try:
        msg, ini = await orch().post_human_message(
            initiative_id,
            req.text,
            author_name=req.author_name or user.display_name,
            author_id=req.author_id or user.id,
        )
        return {
            "message": msg.model_dump(mode="json"),
            "initiative": ini.model_dump(mode="json") if ini else None,
        }
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/initiatives/{initiative_id}/artifacts/{artifact_path:path}")
async def read_artifact(
    initiative_id: str, artifact_path: str, _user: PublicUser = Depends(_mission_access_guard)
) -> dict[str, Any]:
    from sortie_deck.artifacts import ArtifactPathError

    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    rel = artifact_path
    if not rel.startswith(initiative_id):
        rel = f"{initiative_id}/{artifact_path}"
    try:
        content = orch().artifacts.read_text(rel)
    except ArtifactPathError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(404, "Artifact not found") from None
    return {"path": rel, "content": content}


@app.get("/api/initiatives/{initiative_id}/workspace")
async def initiative_workspace(
    initiative_id: str, user: PublicUser = Depends(_mission_access_guard)
) -> dict[str, Any]:
    from sortie_deck.workspace import mission_workspace

    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    return mission_workspace(
        initiative_id=initiative_id,
        data_dir=orch().cfg.data_dir,
        repo_root=orch().cfg.data_dir.parent,
    )


class RevealPathBody(BaseModel):
    path: str
    select_file: bool = False


@app.post("/api/system/reveal")
async def reveal_path(body: RevealPathBody, _user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    """Open a local folder/file in the OS file manager (dev workbench helper)."""
    from pathlib import Path

    from sortie_deck.workspace import allowed_roots, is_under_allowed, reveal_in_os

    raw = (body.path or "").strip()
    if not raw:
        raise HTTPException(400, "path required")
    target = Path(raw).expanduser()
    cfg = orch().cfg
    roots = allowed_roots(cfg.data_dir, cfg.data_dir.parent)
    if not is_under_allowed(target if target.exists() else target.parent, roots):
        # Allow revealing soon-to-exist dirs under allowed roots
        if not is_under_allowed(target, roots):
            raise HTTPException(403, "path outside allowed roots")
    if not target.exists():
        # Create empty mission dirs so reveal still works early in a run
        try:
            if target.suffix:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not is_under_allowed(target.parent, roots):
                    raise HTTPException(403, "path outside allowed roots")
                reveal_in_os(target.parent)
                return {"ok": True, "path": str(target.parent), "created": False, "missing": True}
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"cannot create path: {exc}") from exc
    try:
        reveal_in_os(target)
    except OSError as exc:
        raise HTTPException(500, f"reveal failed: {exc}") from exc
    return {"ok": True, "path": str(target.resolve())}


@app.get("/api/initiatives/{initiative_id}/events")
async def initiative_events(
    initiative_id: str,
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> EventSourceResponse:
    user = get_auth().resolve(_bearer(authorization) or token)
    if not user:
        raise HTTPException(401, "login required")
    o = orch()
    ini = o.get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    _ensure_mission_access(ini, user)

    # 小群事件只推给任务所有权人：共享用户与 admin 能看任务过程，但不该看到小群
    is_owner = getattr(ini, "created_by", None) == user.id
    queue = o.subscribe(initiative_id)

    async def gen():
        try:
            ini = o.get(initiative_id)
            if ini:
                yield {"event": "snapshot", "data": json.dumps(ini.model_dump(mode="json"))}
                msgs = o.list_room_messages(initiative_id)
                yield {
                    "event": "room_snapshot",
                    "data": json.dumps([m.model_dump(mode="json") for m in msgs]),
                }
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if not is_owner and event.get("type") in {"subroom", "subroom_message"}:
                        continue
                    yield {"event": event.get("type", "message"), "data": json.dumps(event, default=str)}
                except TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            o.unsubscribe(initiative_id, queue)

    return EventSourceResponse(gen())


def _bridges() -> Any:
    from sortie_deck.bridges import BridgeStore

    return BridgeStore(settings.bridges_db)


@app.get("/api/bridges")
async def list_bridges(_user: PublicUser = Depends(require_admin)) -> dict[str, Any]:
    return {"mappings": [m.model_dump() for m in _bridges().list()]}


@app.post("/api/bridges/map")
async def map_bridge(
    body: dict[str, Any], _user: PublicUser = Depends(require_admin)
) -> dict[str, Any]:
    from sortie_deck.bridges import BridgeMapping

    mapping = BridgeMapping(
        provider=str(body.get("provider") or ""),
        channel_id=str(body.get("channel_id") or ""),
        initiative_id=str(body.get("initiative_id") or ""),
        label=str(body.get("label") or ""),
    )
    if mapping.provider not in {"slack", "feishu"} or not mapping.channel_id or not mapping.initiative_id:
        raise HTTPException(400, "provider, channel_id, initiative_id required")
    if not orch().get(mapping.initiative_id):
        raise HTTPException(404, "Initiative not found")
    return _bridges().upsert(mapping).model_dump()


@app.post("/api/bridges/slack")
async def slack_bridge(request: Request) -> dict[str, Any]:
    from sortie_deck.bridges import slack_text_from_payload, verify_slack_signature

    body = await request.body()
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = json.loads(body.decode("utf-8") or "{}")
    else:
        # slash command form-encoded
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        payload = form
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    secret = settings.slack_signing_secret or ""
    if secret:
        ok = verify_slack_signature(
            signing_secret=secret,
            timestamp=request.headers.get("x-slack-request-timestamp", ""),
            body=body,
            signature=request.headers.get("x-slack-signature", ""),
        )
        if not ok:
            raise HTTPException(401, "invalid slack signature")
    channel, author, text = slack_text_from_payload(payload)
    if not channel or not text:
        return {"ok": True, "ignored": True}
    mapping = _bridges().resolve("slack", channel)
    if not mapping:
        return {"ok": False, "error": "channel not mapped"}
    try:
        msg, ini = await orch().post_human_message(
            mapping.initiative_id, text, author_name=author
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "message_id": msg.id,
        "initiative_id": mapping.initiative_id,
        "status": ini.status.value if ini else None,
    }


@app.post("/api/bridges/feishu")
async def feishu_bridge(request: Request) -> dict[str, Any]:
    from sortie_deck.bridges import feishu_text_from_payload

    payload = await request.json()
    # URL verification
    if payload.get("type") == "url_verification" or payload.get("challenge"):
        token = settings.feishu_verification_token
        if token and payload.get("token") and payload.get("token") != token:
            raise HTTPException(401, "invalid feishu token")
        return {"challenge": payload.get("challenge")}
    channel, author, text = feishu_text_from_payload(payload)
    if not channel or not text:
        return {"code": 0, "ignored": True}
    mapping = _bridges().resolve("feishu", channel)
    if not mapping:
        return {"code": 0, "error": "chat not mapped"}
    try:
        await orch().post_human_message(mapping.initiative_id, text, author_name=author)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return {"code": 0}


@app.post("/api/bridges/dingtalk")
async def dingtalk_bridge(request: Request) -> dict[str, Any]:
    from sortie_deck.bridges import dingtalk_text_from_payload

    payload = await request.json()
    channel, author, text = dingtalk_text_from_payload(payload)
    if not channel or not text:
        return {"errcode": 0, "ignored": True}
    mapping = _bridges().resolve("dingtalk", channel)
    if not mapping:
        return {"errcode": 0, "errmsg": "conversation not mapped"}
    try:
        await orch().post_human_message(mapping.initiative_id, text, author_name=author)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return {"errcode": 0}


@app.get("/api/subrooms")
async def list_subrooms(
    initiative_id: str | None = Query(default=None),
    user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    """Sub-room records (transcripts stay behind their own endpoint).

    仅任务所有权人（created_by）可读：共享用户与 admin 只要不是 owner 一律 403。
    """
    from sortie_deck.subroom import get_subroom_service

    service = get_subroom_service()
    if service is None:
        return []
    owned = _owned_mission_ids(user)
    if initiative_id and initiative_id not in owned:
        raise HTTPException(403, "小群仅任务创建者可见")
    rows = service.store.list(initiative_id=initiative_id)
    return [r.model_dump(mode="json") for r in rows if r.initiative_id in owned]


@app.get("/api/subrooms/{subroom_id}/transcript")
async def get_subroom_transcript(
    subroom_id: str, user: PublicUser = Depends(require_user)
) -> list[dict[str, Any]]:
    from sortie_deck.subroom import get_subroom_service

    service = get_subroom_service()
    if service is None:
        raise HTTPException(404, "sub-rooms are not wired")
    try:
        sub = service.store.get(subroom_id)
    except KeyError:
        raise HTTPException(404, "sub-room not found") from None
    # 会话正文与列表同口径：非任务所有权人（含共享用户 / admin）不给读
    if sub.initiative_id not in _owned_mission_ids(user):
        raise HTTPException(403, "小群仅任务创建者可见")
    messages = service.transcript(subroom_id)
    return [m.model_dump(mode="json") for m in messages]


class AnswerDecisionRequest(BaseModel):
    answer: str
    by: str = ""
    channel: str = "ui"


@app.get("/api/decisions")
async def list_decisions(
    initiative_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    _user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    from sortie_deck.decisions import get_decision_service

    service = get_decision_service()
    if service is None:
        return []
    return [
        d.model_dump(mode="json")
        for d in service.list(initiative_id=initiative_id, status=status)
    ]


@app.get("/api/decisions/{decision_id}")
async def get_decision(
    decision_id: str, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    from sortie_deck.decisions import get_decision_service

    service = get_decision_service()
    if service is None:
        raise HTTPException(404, "decision centre is not wired")
    try:
        return service.get(decision_id).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "decision not found") from None


@app.post("/api/decisions/{decision_id}/answer")
async def answer_decision(
    decision_id: str,
    req: AnswerDecisionRequest,
    user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    """Record a human answer; idempotent and authorised against the assignee."""
    outcome = await orch().resolve_decision(
        decision_id,
        answer=req.answer,
        by=req.by or user.display_name,
        channel=req.channel,
        is_admin=user.role == "admin",
    )
    return outcome.model_dump(mode="json")


@app.post("/api/decisions/sweep")
async def sweep_decisions(_user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    """Expire overdue decisions and report the fallback that was applied."""
    expired = await orch().sweep_decisions()
    return {"expired": [d.model_dump(mode="json") for d in expired]}


@app.get("/api/org-policy")
async def get_org_policy(_user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    policy = orch().org_policy
    return {"policy": policy.model_dump() if policy else None}


WEB_DIST = Path(__file__).resolve().parents[3] / "apps" / "web" / "dist"
if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIST / "index.html")


def run() -> None:
    import uvicorn

    uvicorn.run(
        "sortie_deck.api.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
