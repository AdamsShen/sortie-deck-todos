from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
)
from sortie_deck.knowledge import (
    CreateKnowledgeRequest,
    KnowledgeStore,
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
    HitlAction,
    HitlDecision,
    HitlSubmitRequest,
    JoinRoomRequest,
    PipelineNotConfirmedError,
    PostRoomMessageRequest,
    UpdatePipelineRequest,
)
from sortie_deck.orchestrator import Orchestrator, bind_catalogs, get_orchestrator
from sortie_deck.pipeline import list_tracks, stage_catalog
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
_knowledge: KnowledgeStore | None = None
_knowledge_gw: KnowledgeGateway | None = None
_memory: MemoryStore | None = None
_agents: AgentCatalog | None = None
_toolkit: ToolkitStore | None = None
_briefing: Any = None


def get_auth() -> AuthStore:
    global _auth
    if _auth is None:
        _auth = AuthStore(settings.users_db, token_secret=settings.resolved_token_secret())
    return _auth


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
    from sortie_deck.runtime import (
        configure_logging,
        ensure_runtime_dirs,
        validate_settings,
    )

    configure_logging(settings.log_level)
    ensure_runtime_dirs(settings)
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
    except PermissionError:
        raise HTTPException(401, "invalid username or password") from None


@app.post("/api/auth/logout")
async def logout(authorization: str | None = Header(default=None)) -> dict[str, str]:
    token = _bearer(authorization)
    if token:
        get_auth().logout(token)
    return {"status": "ok"}


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


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: str, _admin: PublicUser = Depends(require_admin)) -> dict[str, str]:
    try:
        get_auth().delete_user(user_id)
        return {"status": "deleted"}
    except KeyError:
        raise HTTPException(404, "user not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/dashboard")
async def dashboard(user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    items = orch().list_initiatives()
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
    _user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    return [d.model_dump(mode="json") for d in get_knowledge_gateway().list_catalog(q)]


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
    user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    return [
        m.model_dump(mode="json")
        for m in get_memory().list(scope=scope, scope_id=scope_id, q=q, user_id=user.id)
    ]


@app.post("/api/memory")
async def create_memory(
    req: CreateMemoryRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    return get_memory().create(req, author_id=user.id, author_name=user.display_name).model_dump(
        mode="json"
    )


@app.patch("/api/memory/{mem_id}")
async def update_memory(
    mem_id: str, req: UpdateMemoryRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        entry = get_memory().get(mem_id)
        if entry.scope == "user" and entry.scope_id != user.id and user.role != "admin":
            raise HTTPException(403, "forbidden")
        return get_memory().update(mem_id, req).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "memory not found") from None


@app.delete("/api/memory/{mem_id}")
async def delete_memory(mem_id: str, user: PublicUser = Depends(require_user)) -> dict[str, str]:
    try:
        entry = get_memory().get(mem_id)
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


@app.get("/api/agents")
async def list_agents(
    q: str | None = Query(default=None),
    slot: str | None = Query(default=None),
    user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    return [
        a.model_dump(mode="json")
        for a in get_agents().list(q=q, slot=slot, user_id=user.id)
    ]


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str, _user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    try:
        return get_agents().get(agent_id).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "agent not found") from None


@app.post("/api/agents")
async def create_agent(
    req: CreateAgentRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    return get_agents().create(req, author_id=user.id, author_name=user.display_name).model_dump(
        mode="json"
    )


@app.patch("/api/agents/{agent_id}")
async def update_agent(
    agent_id: str, req: UpdateAgentRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        return (
            get_agents()
            .update(agent_id, req, editor_id=user.id, is_admin=user.role == "admin")
            .model_dump(mode="json")
        )
    except KeyError:
        raise HTTPException(404, "agent not found") from None
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


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


@app.get("/api/forge/drafts")
async def list_forge_drafts(
    status: str | None = Query(default=None),
    _user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    return [d.model_dump(mode="json") for d in get_agent_forge().list(status=status)]


@app.post("/api/forge/drafts")
async def propose_forge_draft(
    req: ForgeProposeRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    draft = await get_agent_forge().propose(
        ForgeRequest(**req.model_dump(), requested_by=user.display_name)
    )
    return draft.model_dump(mode="json")


@app.get("/api/forge/drafts/{draft_id}")
async def get_forge_draft(
    draft_id: str, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        return get_agent_forge().get(draft_id).model_dump(mode="json")
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
    return draft.model_dump(mode="json")


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
        "draft": draft.model_dump(mode="json"),
        "role": role_spec.model_dump(mode="json") if role_spec else None,
        "agent": published.model_dump(mode="json") if published else None,
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
    user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    """Draft-only harness change; started missions require HITL switch_harness."""
    try:
        ini = await orch().update_harness(initiative_id, req.harness)
        return ini.model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


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
    _user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    items = orch().list_initiatives()
    if status:
        items = [i for i in items if i.status.value == status]
    return [i.model_dump(mode="json") for i in items]


@app.post("/api/initiatives")
async def create_initiative(
    req: CreateInitiativeRequest, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    if not req.creator_name or req.creator_name == "Operator":
        req.creator_name = user.display_name
    ini = await orch().create_initiative(req)
    await asyncio.sleep(0.15)
    fresh = orch().get(ini.id) or ini
    return fresh.model_dump(mode="json")


class BriefingMessageBody(BaseModel):
    text: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)


@app.post("/api/briefing/sessions")
async def start_briefing(user: PublicUser = Depends(require_user)) -> dict[str, Any]:
    sess = get_briefing().start()
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
    user: PublicUser = Depends(require_user),
) -> dict[str, Any]:
    from sortie_deck.mission_briefing import BriefingAttachment

    atts = [BriefingAttachment.model_validate(a) for a in (body.attachments or [])]
    try:
        sess = await get_briefing().handle(
            session_id,
            body.text,
            user_name=user.display_name or user.username,
            attachments=atts,
        )
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
    initiative_id: str, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    return ini.model_dump(mode="json")


@app.delete("/api/initiatives/{initiative_id}")
async def delete_initiative(
    initiative_id: str, _user: PublicUser = Depends(require_user)
) -> dict[str, str]:
    try:
        await orch().delete_initiative(initiative_id)
        return {"status": "deleted"}
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None


@app.post("/api/initiatives/{initiative_id}/start")
async def start_initiative(
    initiative_id: str, user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    try:
        ini = await orch().start_pipeline(initiative_id, author_name=user.display_name)
        await asyncio.sleep(0.2)
        return (orch().get(ini.id) or ini).model_dump(mode="json")
    except KeyError:
        raise HTTPException(404, "Initiative not found") from None
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
    user: PublicUser = Depends(require_user),
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
    user: PublicUser = Depends(require_user),
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
    initiative_id: str, req: HitlSubmitRequest, user: PublicUser = Depends(require_user)
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
    initiative_id: str, user: PublicUser = Depends(require_user)
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
    initiative_id: str, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    return _squad_payload(initiative_id)


@app.post("/api/initiatives/{initiative_id}/squad/join")
@app.post("/api/initiatives/{initiative_id}/room/join")
async def join_squad(
    initiative_id: str, req: JoinRoomRequest, user: PublicUser = Depends(require_user)
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
    user: PublicUser = Depends(require_user),
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
    user: PublicUser = Depends(require_user),
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
    initiative_id: str, artifact_path: str, _user: PublicUser = Depends(require_user)
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
    initiative_id: str, _user: PublicUser = Depends(require_user)
) -> dict[str, Any]:
    from sortie_deck.workspace import mission_workspace

    ini = orch().get(initiative_id)
    if not ini:
        raise HTTPException(404, "Initiative not found")
    cfg = orch().cfg
    return mission_workspace(
        initiative_id=initiative_id,
        artifacts_dir=cfg.artifacts_dir,
        worktrees_dir=cfg.worktrees_dir,
        repo_root=cfg.data_dir.parent,
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
    if not o.get(initiative_id):
        raise HTTPException(404, "Initiative not found")

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
    _user: PublicUser = Depends(require_user),
) -> list[dict[str, Any]]:
    """Sub-room records (transcripts stay behind their own endpoint)."""
    from sortie_deck.subroom import get_subroom_service

    service = get_subroom_service()
    if service is None:
        return []
    return [
        r.model_dump(mode="json") for r in service.store.list(initiative_id=initiative_id)
    ]


@app.get("/api/subrooms/{subroom_id}/transcript")
async def get_subroom_transcript(
    subroom_id: str, _user: PublicUser = Depends(require_user)
) -> list[dict[str, Any]]:
    from sortie_deck.subroom import get_subroom_service

    service = get_subroom_service()
    if service is None:
        raise HTTPException(404, "sub-rooms are not wired")
    try:
        messages = service.transcript(subroom_id)
    except KeyError:
        raise HTTPException(404, "sub-room not found") from None
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
