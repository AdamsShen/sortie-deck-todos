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
from typing import Any, Literal

from sortie_deck.api_keys import ApiKeyStore
from sortie_deck.auth import AuthStore, PublicUser
from sortie_deck.models import (
    CreateInitiativeRequest,
    ForgeReviewRequiredError,
    HitlAction,
    HitlDecision,
    Initiative,
    InitiativeStatus,
    PipelineNotConfirmedError,
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
