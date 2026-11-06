"""服务层：把 HTTP 路由里的身份/权限/编排调用抽出来，供 CLI 与 MCP 复用。

为什么需要这一层：权限判定目前长在 ``api/app.py`` 的路由函数里，直接
``raise HTTPException``。任何非 HTTP 入口（CLI、MCP）要么重写一遍，要么
把 HTTP 语义硬塞进命令行——两条路都会导致三份权限代码各自漂移。

这里只做「薄抽取」：现有路由保持原样，新增入口统一走本模块。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sortie_deck.api_keys import ApiKeyStore
from sortie_deck.auth import AuthStore, PublicUser
from sortie_deck.models import (
    CreateInitiativeRequest,
    ExternalMaterial,
    ForgeReviewRequiredError,
    HitlAction,
    HitlDecision,
    Initiative,
    InitiativeStatus,
    PipelineNotConfirmedError,
    RepoConfig,
)
from sortie_deck.orchestrator import Orchestrator, get_orchestrator
from sortie_deck.settings import settings

logger = logging.getLogger(__name__)

PrincipalSource = Literal["web", "cli", "mcp"]


# ---------------------------------------------------------------------------
# 错误：一个异常，三套出口（HTTP 状态码 / CLI 退出码 / MCP 错误文本）
# ---------------------------------------------------------------------------

# code -> (http_status, cli_exit_code)
_ERROR_MAP: dict[str, tuple[int, int]] = {
    "auth_required": (401, 3),
    "forbidden": (403, 3),
    "not_found": (404, 4),
    "conflict": (409, 5),
    "validation": (400, 6),
    "pipeline_not_confirmed": (400, 6),
    "forge_review_required": (409, 5),
    "rate_limited": (429, 7),
    "unavailable": (503, 7),
}


class ServiceError(Exception):
    """跨入口的统一业务错误。

    ``code`` 是稳定契约，调用方按它分支；``message`` 面向人。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


    @property
    def http_status(self) -> int:
        return _ERROR_MAP.get(self.code, (400, 6))[0]

    @property
    def exit_code(self) -> int:
        return _ERROR_MAP.get(self.code, (400, 6))[1]

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


def _clean_role_repos(
    raw: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, list[RepoConfig]]:
    """把外部传入的 role_repos 规范成 ``{slot: [RepoConfig]}``，丢掉空 URL。

    凭据改造后：只认 ``source`` + ``credential_id``（见 git_cred），
    历史字段 ``token`` 不再读取——避免明文凭据继续经请求体流入任务 payload。
    """
    out: dict[str, list[RepoConfig]] = {}
    for slot, rows in (raw or {}).items():
        items: list[RepoConfig] = []
        for row in rows or []:
            if isinstance(row, RepoConfig):
                if row.token:
                    logger.warning("忽略 RepoConfig.token（已废弃），请改用 source/credential_id")
                # 新模型（有 repo_id）必须写分支；历史 payload 保持兼容
                if row.repo_id and not (row.branch or "").strip():
                    raise ServiceError("validation", f"{slot}: 请填写仓库分支（{row.url}）")
                items.append(row.model_copy(update={"token": None}))
                continue
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            branch = str(row.get("branch") or "").strip()
            repo_id = str(row.get("repo_id") or "").strip() or None
            if repo_id and not branch:
                raise ServiceError("validation", f"{slot}: 请填写仓库分支（{url}）")
            if row.get("token"):
                logger.warning("忽略请求中的 repo token（已废弃），请改用 source/credential_id")
            items.append(
                RepoConfig(
                    url=url,
                    branch=branch,
                    name=str(row.get("name") or "").strip(),
                    source="workspace" if row.get("source") == "workspace" else "custom",
                    repo_id=str(row.get("repo_id") or "").strip() or None,
                    credential_id=str(row.get("credential_id") or "").strip() or None,
                )
            )
        if items:
            out[str(slot)] = items
    return out


def _validate_repo_credentials(
    clean: dict[str, list[RepoConfig]], *, actor_user_id: str
) -> None:
    """绑定前校验「仓库 ↔ 凭据」绑定（不联网；网络探活由 preflight 负责）。

    - 有 repo_id：回查注册表，用仓库当前 credential_id；
    - workspace 仓库只允许工作区凭据；个人仓库可选工作区或个人凭据；
    - 凭据必须存在、active，且 host/scheme 与仓库 URL 完全一致；
    - 无 repo_id 的历史 payload：保留旧的 host 匹配兜底。
    """
    from sortie_deck.git_cred import (
        GitCredentialError,
        host_of,
        scheme_of,
        stores_from_settings,
        validate_repo_credential,
    )

    if not clean:
        return
    try:
        creds, workspace_repos, user_repos = stores_from_settings()
    except GitCredentialError as exc:
        raise ServiceError("validation", f"git 凭据存储不可用：{exc}") from exc

    for slot, rows in clean.items():
        for repo in rows:
            try:
                if repo.repo_id:
                    if repo.source == "workspace":
                        row = workspace_repos.get(repo.repo_id)
                        if row is None or not row.enabled:
                            raise GitCredentialError(f"工作区仓库不存在或已停用: {repo.repo_id}")
                        validate_repo_credential(
                            row.url,
                            row.credential_id or "",
                            credentials=creds,
                            allow_user=False,
                        )
                    else:
                        row = user_repos.get_for_owner(actor_user_id, repo.repo_id)
                        if row is None:
                            raise GitCredentialError(f"个人仓库不存在: {repo.repo_id}")
                        validate_repo_credential(
                            row.url,
                            row.credential_id or "",
                            credentials=creds,
                            owner_id=actor_user_id,
                        )
                    continue
                # ---- 历史 payload（无 repo_id）----
                if repo.source == "workspace":
                    validate_repo_credential(
                        repo.url,
                        repo.credential_id or "",
                        credentials=creds,
                        allow_user=False,
                    )
                    continue
                host = host_of(repo.url)
                if not host:
                    logger.warning("%s: 无法从 URL 解析 host，跳过个人凭据校验（%s）", slot, repo.url)
                    continue
                if actor_user_id and creds.find_for_host(
                    owner_id=actor_user_id, host=host, scheme=scheme_of(repo.url)
                ) is None:
                    raise GitCredentialError(
                        f"未配置 {host} 的个人 git 凭据，请先在「设置 → Git 凭据」中添加"
                    )
            except GitCredentialError as exc:
                raise ServiceError("validation", f"{slot}: {exc}") from exc


async def preflight_role_repos(
    clean: dict[str, list[RepoConfig]], *, actor_user_id: str
) -> None:
    """绑定前逐个仓库 `git ls-remote` 探活；失败直接报错，避免拖到 coding 阶段才炸。

    必须由 async 调用方（API 路由 / MCP 工具）在自身事件循环里 await，
    不要放进同步的 bind_repos：那里无法安全跑协程。
    """
    from sortie_deck.git_cred import preflight_git_access, resolver_from_settings

    if not clean:
        return
    resolver = resolver_from_settings() if actor_user_id else None
    for slot, rows in clean.items():
        for repo in rows:
            ok, reason = await preflight_git_access(
                repo, resolver=resolver, actor_user_id=actor_user_id
            )
            if not ok:
                raise ServiceError("validation", f"{slot}: {repo.url} 预检失败：{reason}")


def _clean_materials(raw: list[dict[str, Any]] | None) -> list[ExternalMaterial]:
    """把外部传入的材料规范成 ExternalMaterial；``content`` 等价于 ``text_excerpt``。"""
    out: list[ExternalMaterial] = []
    for row in raw or []:
        if isinstance(row, ExternalMaterial):
            out.append(row)
            continue
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        text = str(row.get("text_excerpt") or row.get("content") or "")
        out.append(
            ExternalMaterial(
                name=name,
                kind=str(row.get("kind") or "other"),
                mime=str(row.get("mime") or ""),
                size=len(text.encode("utf-8")),
                pinned=bool(row.get("pinned", False)),
                text_excerpt=text,
                extract_status="ok" if text else "pending",
            )
        )
    return out


def artifact_candidates(mission_id: str, path: str) -> list[str]:
    """把调用方给的产物路径展开成 artifact store 里的候选 key。

    列表接口返回的是完整存储路径 ``<id>/artifacts/<stage>/<file>``，但用户和模型
    通常只写 ``<stage>/<file>`` 甚至 ``<file>``。REST / MCP / CLI 三个入口必须对
    同一份输入得出同样的结果，所以解析放在这里，不在各自入口里重复一套。
    """
    p = (path or "").strip().lstrip("/")
    if not p:
        return []
    if p.startswith(f"{mission_id}/"):
        return [p]
    if p.startswith("artifacts/"):
        return [f"{mission_id}/{p}"]
    return [f"{mission_id}/artifacts/{p}", f"{mission_id}/{p}"]


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    user: PublicUser
    source: PrincipalSource = "web"
    key_id: str | None = None

    @property
    def display_name(self) -> str:
        return self.user.display_name or self.user.username


class _ApiKeyRateLimiter:
    """按 API Key 的令牌桶（进程内）。

    多副本部署时每个副本各算各的，属于"软限流"：目的是挡住脚本死循环/
    失控 agent 打爆服务，不是精确配额。窗口 60s，容量取配置值。
    """

    def __init__(self, per_minute: int = 240) -> None:
        self.capacity = max(1, int(per_minute))
        self._buckets: dict[str, tuple[float, float]] = {}  # key_id -> (tokens, ts)
        self._lock = threading.Lock()

    def allow(self, key_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, ts = self._buckets.get(key_id, (float(self.capacity), now))
            elapsed = max(0.0, now - ts)
            tokens = min(float(self.capacity), tokens + elapsed * self.capacity / 60.0)
            if tokens < 1.0:
                self._buckets[key_id] = (tokens, now)
                return False
            self._buckets[key_id] = (tokens - 1.0, now)
            return True


_limiter = _ApiKeyRateLimiter(settings.api_key_rate_limit_per_minute)
_auth: AuthStore | None = None
_api_keys: ApiKeyStore | None = None


def get_auth_store() -> AuthStore:
    global _auth
    if _auth is None:
        _auth = AuthStore(settings.users_db, token_secret=settings.resolved_token_secret())
    return _auth


def get_api_key_store() -> ApiKeyStore:
    global _api_keys
    if _api_keys is None:
        _api_keys = ApiKeyStore(settings.api_keys_db)
    return _api_keys


def reset_stores() -> None:
    """测试辅助：清空进程级单例（换 data_dir 后必须重置）。"""
    global _auth, _api_keys
    if _api_keys is not None:
        _api_keys.close()
    _auth = None
    _api_keys = None


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    value = authorization.strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value or None


def resolve_principal(
    authorization: str | None, *, source: PrincipalSource = "web"
) -> Principal:
    """解析调用者：先按 API Key，再按登录 token。

    API Key 优先是因为它以 ``sk_sortie_`` 开头、不可能与登录 token 冲突，
    顺序只影响一次无谓的字典查找。
    """
    token = _bearer(authorization)
    if not token:
        raise ServiceError("auth_required", "缺少 Authorization 凭据")

    key = get_api_key_store().verify(token)
    if key is not None:
        if not _limiter.allow(key.id):
            raise ServiceError(
                "rate_limited",
                "该 API Key 请求过于频繁，请稍后重试",
            )
        user = get_auth_store().get_by_id(key.user_id)
        if user is None or getattr(user, "status", "active") != "active":
            raise ServiceError("auth_required", "API Key 对应的账号不可用")
        return Principal(user=_public(user), source=source, key_id=key.id)

    user = get_auth_store().resolve(token)
    if user is None:
        raise ServiceError("auth_required", "凭据无效或已过期")
    return Principal(user=user, source=source)


def _public(user: Any) -> PublicUser:
    """``AuthStore.get_by_id`` 返回 ``User``（含密码哈希），这里转成对外模型。"""
    if isinstance(user, PublicUser):
        return user
    return PublicUser(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        status=getattr(user, "status", "active"),
    )


# ---------------------------------------------------------------------------
# 权限（与 api/app.py 的 _ensure_* 语义保持一致）
# ---------------------------------------------------------------------------


def is_admin(principal: Principal) -> bool:
    return principal.user.role == "admin"


def is_owner(ini: Initiative, principal: Principal) -> bool:
    owner = getattr(ini, "created_by", None)
    return bool(owner) and owner == principal.user.id


def is_shared_with(ini: Initiative, principal: Principal) -> bool:
    return any(s.user_id == principal.user.id for s in getattr(ini, "shared_with", []) or [])


def ensure_access(ini: Initiative, principal: Principal) -> None:
    """可访问：创建者 / 被共享 / admin。"""
    if is_admin(principal) or is_shared_with(ini, principal) or is_owner(ini, principal):
        return
    if not getattr(ini, "created_by", None):
        raise ServiceError("forbidden", "该任务无归属信息，仅管理员可访问")
    raise ServiceError("forbidden", "无权访问该任务（未共享给当前账号）")


def ensure_control(ini: Initiative, principal: Principal) -> None:
    """可控制（启动/停止/门禁）：创建者或被共享用户，admin 只读。"""
    if is_owner(ini, principal) or is_shared_with(ini, principal):
        return
    raise ServiceError("forbidden", "仅任务创建者或被共享用户可控制任务运行；管理员为只读")


def ensure_delete(ini: Initiative, principal: Principal) -> None:
    """可解散：创建者或 admin。"""
    if is_admin(principal) or is_owner(ini, principal):
        return
    raise ServiceError("forbidden", "仅任务创建者或 admin 可解散任务")


def ensure_share_owner(ini: Initiative, principal: Principal) -> None:
    if is_owner(ini, principal):
        return
    raise ServiceError("forbidden", "仅任务创建者可将任务共享给他人")


# ---------------------------------------------------------------------------
# 任务服务：CLI 与 MCP 的唯一入口
# ---------------------------------------------------------------------------


class MissionService:
    """面向非 HTTP 入口的任务操作封装。

    所有方法都接受 ``Principal`` 并做权限校验；调用方不再自己判断"能不能"。
    """

    def __init__(self, orchestrator: Orchestrator | None = None) -> None:
        self._orch = orchestrator

    @property
    def orch(self) -> Orchestrator:
        return self._orch or get_orchestrator()

    # --- 查询 ---------------------------------------------------------

    def get(self, principal: Principal, mission_id: str) -> Initiative:
        ini = self.orch.get(mission_id)
        if ini is None:
            raise ServiceError("not_found", f"任务不存在: {mission_id}")
        ensure_access(ini, principal)
        return ini

    def list_mine(
        self,
        principal: Principal,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[Initiative]:
        """只返回「我创建或被共享」的任务（admin 也只返回自己的，避免误操作他人任务）。"""
        items = [
            ini
            for ini in self.orch.list_initiatives()
            if is_owner(ini, principal) or is_shared_with(ini, principal)
        ]
        if status:
            items = [ini for ini in items if ini.status.value == status]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items[:limit] if limit else items

    def latest(self, principal: Principal) -> Initiative | None:
        items = self.list_mine(principal)
        return items[0] if items else None

    def artifacts(self, principal: Principal, mission_id: str) -> list[Any]:
        return list(self.get(principal, mission_id).artifacts)

    def read_artifact(self, principal: Principal, mission_id: str, path: str) -> str:
        ini = self.get(principal, mission_id)  # 权限校验
        for rel in artifact_candidates(mission_id, path):
            try:
                return self.orch.artifacts.read_text(rel)
            except FileNotFoundError:
                continue
        # 只给文件名时按产物列表唯一匹配（列表里显示的是完整存储路径，用户/模型常只写文件名）
        name = (path or "").strip().lstrip("/")
        if name and "/" not in name:
            hits = [
                a.path
                for a in ini.artifacts
                if str(a.path).endswith("/" + name) or a.kind == name
            ]
            hits = list(dict.fromkeys(hits))
            if len(hits) == 1:
                try:
                    return self.orch.artifacts.read_text(hits[0])
                except FileNotFoundError:
                    pass
            if len(hits) > 1:
                raise ServiceError(
                    "conflict", f"同名产物有多个，请写完整路径: {', '.join(hits)}"
                )
        raise ServiceError("not_found", f"产物不存在: {path}")

    def events(self, principal: Principal, mission_id: str, since_seq: int = 0) -> list[Any]:
        self.get(principal, mission_id)  # 权限校验
        return self.orch.events.since(mission_id, since_seq)

    # --- 写 -----------------------------------------------------------

    async def start_mission(
        self,
        principal: Principal,
        *,
        brief: str,
        title: str | None = None,
        track: str | None = None,
        harness: str | None = None,
        coding_executor: str = "auto",
    ) -> Initiative:
        """建任务 + 自动确认航线 + 启动（MCP/CLI 的一步式入口）。"""
        if not (brief or "").strip():
            raise ServiceError("validation", "brief 不能为空")
        req = CreateInitiativeRequest(
            title=(title or brief.strip().splitlines()[0])[:80],
            brief=brief.strip(),
            template=track or "default",
            pipeline_track=track if track in {"express", "standard", "default", "research"} else None,
            coding_executor=coding_executor,  # type: ignore[arg-type]
            harness=harness,
            auto_start=True,
            auto_confirm_pipeline=True,
            creator_name=principal.display_name,
            created_by=principal.user.id,
        )
        try:
            return await self.orch.create_and_start(req)
        except PipelineNotConfirmedError as exc:
            raise ServiceError("pipeline_not_confirmed", str(exc)) from exc
        except ForgeReviewRequiredError as exc:
            raise ServiceError("forge_review_required", exc.message) from exc
        except ValueError as exc:
            raise ServiceError("validation", str(exc)) from exc

    async def confirm(self, principal: Principal, mission_id: str) -> Initiative:
        ensure_control(self.get(principal, mission_id), principal)
        return await self.orch.confirm_pipeline(mission_id, author_name=principal.display_name)

    async def start(self, principal: Principal, mission_id: str) -> Initiative:
        ini = self.get(principal, mission_id)
        ensure_control(ini, principal)
        try:
            return await self.orch.start_pipeline(
                mission_id, author_name=principal.display_name
            )
        except PipelineNotConfirmedError as exc:
            raise ServiceError("pipeline_not_confirmed", str(exc)) from exc
        except ForgeReviewRequiredError as exc:
            raise ServiceError("forge_review_required", exc.message) from exc
        except RuntimeError as exc:
            raise ServiceError("conflict", str(exc)) from exc

    async def create_mission(
        self,
        principal: Principal,
        *,
        brief: str,
        title: str | None = None,
        track: str | None = None,
        harness: str | None = None,
        role_repos: dict[str, list[dict[str, Any]]] | None = None,
        materials: list[dict[str, Any]] | None = None,
    ) -> Initiative:
        """建任务但**不启动**：先绑仓库、挂材料，再单独 start。

        一句话建任务（``start_mission``）会立刻开跑，仓库/材料只能落到已经
        跑起来的任务上，等于无效。所以需要这个"先装配、后开跑"的入口。
        航线在这里就确认好，后续 ``start`` 不需要再确认。
        """
        if not (brief or "").strip():
            raise ServiceError("validation", "brief 不能为空")
        clean_repos = _clean_role_repos(role_repos)
        _validate_repo_credentials(clean_repos, actor_user_id=principal.user.id)
        req = CreateInitiativeRequest(
            title=(title or brief.strip().splitlines()[0])[:80],
            brief=brief.strip(),
            template=track or "default",
            pipeline_track=track if track in {"express", "standard", "default", "research"} else None,
            harness=harness,
            auto_start=False,
            auto_confirm_pipeline=True,
            creator_name=principal.display_name,
            created_by=principal.user.id,
            role_repos=clean_repos,
            materials=_clean_materials(materials),
        )
        return await self.orch.create_initiative(req)

    def bind_repos(
        self, principal: Principal, mission_id: str, role_repos: dict[str, list[dict[str, Any]]]
    ) -> Initiative:
        """给**未启动**的任务绑仓库（启动后改不了，只能新建任务）。"""
        ini = self.get(principal, mission_id)
        ensure_control(ini, principal)
        if ini.status is not InitiativeStatus.DRAFT:
            raise ServiceError(
                "conflict",
                f"任务已是 {ini.status.value}，仓库需在启动前绑定；"
                "请用 create_mission 建草稿 → bind_repos → start_mission",
            )
        clean = _clean_role_repos(role_repos)
        _validate_repo_credentials(clean, actor_user_id=principal.user.id)
        meta = dict(ini.meta or {})
        meta["role_repos"] = {
            slot: [r.model_dump(mode="json") for r in rows] for slot, rows in clean.items()
        }
        ini.meta = meta
        return self.orch.store.upsert(ini)

    def attach_materials(
        self, principal: Principal, mission_id: str, materials: list[dict[str, Any]]
    ) -> Initiative:
        """追加/覆盖材料（同名覆盖，其余保留）；pinned 的会进每个阶段的提示词。"""
        ini = self.get(principal, mission_id)
        ensure_control(ini, principal)
        merged = {m.name: m for m in (ini.materials or [])}
        for item in _clean_materials(materials):
            merged[item.name] = item
        return self.orch.set_materials(mission_id, list(merged.values()))

    def forge_drafts(self, principal: Principal, mission_id: str) -> list[dict[str, Any]]:
        """本任务建队时生成、仍待审核的角色草稿（forge_review_required 的根因）。"""
        ini = self.get(principal, mission_id)
        ids = [str(x) for x in ((ini.meta or {}).get("forge_drafts") or []) if str(x).strip()]
        if not ids:
            return []
        from sortie_deck.agent_forge import ForgeStore

        store = ForgeStore(Path(self.orch.cfg.data_dir) / "forge.json")
        rows: list[dict[str, Any]] = []
        for draft_id in ids:
            try:
                draft = store.get(draft_id)
            except KeyError:
                continue
            if draft.status != "pending-review":
                continue
            rows.append(
                {
                    "draft_id": draft.id,
                    "role": draft.role_key,
                    "title": draft.title,
                    "summary": draft.summary,
                    "status": draft.status,
                    "issues": list(draft.issues or []),
                }
            )
        return rows

    def approve_forge_draft(
        self, principal: Principal, draft_id: str, *, publish_agent: bool = False
    ) -> dict[str, Any]:
        """审批角色草稿（注册角色，可选发布为 agent），解除建队门禁。"""
        from sortie_deck.agent_forge import get_agent_forge

        try:
            draft, role, agent = get_agent_forge().approve(
                draft_id, reviewer=principal.display_name, publish_agent=publish_agent
            )
        except KeyError as exc:
            raise ServiceError("not_found", f"草稿不存在: {draft_id}") from exc
        except ValueError as exc:
            raise ServiceError("conflict", str(exc)) from exc
        return {
            "draft_id": draft.id,
            "role": draft.role_key,
            "status": draft.status,
            "role_registered": role is not None,
            "agent_published": agent is not None,
        }

    async def hitl(
        self,
        principal: Principal,
        mission_id: str,
        *,
        action: HitlAction | str,
        instruction: str | None = None,
        note: str | None = None,
    ) -> Initiative:
        ini = self.get(principal, mission_id)
        ensure_control(ini, principal)
        act = HitlAction(action) if isinstance(action, str) else action

        if ini.status is not InitiativeStatus.WAITING_HITL:
            # 幂等：三端同时点，后到的明确告知"已被处理"，而不是重复执行一遍
            handled_by = str((ini.meta or {}).get("last_hitl_by") or "")
            raise ServiceError(
                "conflict",
                f"任务当前状态为 {ini.status.value}，没有待处理的门禁"
                + (f"（已由 {handled_by} 处理）" if handled_by else ""),
            )
        if act == HitlAction.STOP:
            return await self.orch.stop(
                mission_id, author_name=principal.display_name, source=principal.source
            )

        decision = HitlDecision(action=act, instruction=instruction, note=note, source=principal.source)
        try:
            return await self.orch.submit_hitl(
                mission_id, decision, author_name=principal.display_name
            )
        except RuntimeError as exc:
            raise ServiceError("conflict", str(exc)) from exc

    async def stop(self, principal: Principal, mission_id: str) -> Initiative:
        ensure_control(self.get(principal, mission_id), principal)
        return await self.orch.stop(
            mission_id, author_name=principal.display_name, source=principal.source
        )


_service: MissionService | None = None


def get_mission_service() -> MissionService:
    global _service
    if _service is None:
        _service = MissionService()
    return _service
