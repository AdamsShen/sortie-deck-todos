"""用户数据导出：把当前账号的 6 类数据聚合为单个 JSON 载荷。

职责边界：本模块只做「过滤 + 脱敏 + 组装」，不直接解析鉴权头、也不写文件。
调用方（``api/app.py`` 的 ``GET /api/export/user``）负责鉴权与 HTTP 下载头，
这样单测可以注入假存储，不必起服务。

导出结构（PRD E-1，字段只增不改）::

    {
      "meta": {"schema_version": 1, "exported_at": "...", "owner": "usr_ab12",
               "app": "sortie-deck"},
      "profile": {"id": "usr_ab12", "username": "ada", "display_name": "Ada",
                  "role": "member", "status": "active", "created_at": "..."},
      "api_keys": [{"id": "key_...", "name": "...", "prefix": "sk_sortie_...",
                    "created_at": "...", "last_used_at": null,
                    "expires_at": "...", "revoked": false}],
      "missions": [{"id": "ini_...", "title": "...", "status": "draft",
                    "current_stage": "intake", "created_at": "...", "updated_at": "..."}],
      "knowledge": [{"id": "kbs_...", "name": "...", "description": "...",
                     "doc_count": 3, "created_at": "...", "updated_at": "..."}],
      "memory": [{"id": "mem_...", "content": "...", "tags": ["..."],
                  "created_at": "...", "updated_at": "..."}],
      "rooms": [{"id": "ini_...", "title": "...", "created_at": "...",
                 "message_count": 12}],
    }

敏感信息（``password_hash``、API Key 明文、登录令牌）在组装阶段按白名单剔除：
每个分区只挑约定字段，绝不 ``model_dump()`` 整个内部对象。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sortie_deck.auth import PublicUser
from sortie_deck.models import utc_now

#: 导出结构版本：字段只增不改，结构变更时递增（PRD E-6）
EXPORT_SCHEMA_VERSION = 1

#: 单次导出载荷上限（字节）：20 MB，超限由路由返回 413（PRD D-4）
MAX_EXPORT_BYTES = 20 * 1024 * 1024

APP_NAME = "sortie-deck"

#: 房间消息计数上限：RoomStore.list 默认只取最近 500 条，这里取足够大的值
#: 以统计全部消息（PRD rooms.message_count 需为真实条数）。
_ROOM_MESSAGE_LIMIT = 1_000_000

#: 文件名里的用户名只保留 ASCII 安全字符，避免 Content-Disposition 注入/乱码
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def _iso(value: Any) -> str | None:
    """把 datetime / epoch 秒统一转成 ISO-8601 字符串，空值保持 None。

    API Key 表用 epoch 秒存时间（``float``），其余模型用 ``datetime``；
    导出 JSON 统一成字符串，前端可直接展示（PRD E-4）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    return str(value)


def _status_text(status: Any) -> str:
    """枚举状态（InitiativeStatus 等 str Enum）→ 纯字符串值。"""
    return str(getattr(status, "value", status))


def _is_shared_with(initiative: Any, user_id: str) -> bool:
    """任务是否显式共享给该用户（``shared_with`` 为 MissionShare 列表）。"""
    return any(
        getattr(share, "user_id", None) == user_id
        for share in (getattr(initiative, "shared_with", None) or [])
    )


def _my_missions(initiatives: list[Any], user_id: str) -> list[Any]:
    """本人可见的任务：自己创建 + 显式共享给自己。

    不复用 ``api/app.py::_visible_initiatives``：那个口径对 admin 全量放行，
    导出必须严格限定「本人」，避免把他人任务带进导出文件（PRD B-3）。
    """
    return [
        ini
        for ini in initiatives
        if getattr(ini, "created_by", None) == user_id
        or _is_shared_with(ini, user_id)
    ]


def _profile(user: PublicUser, auth_store: Any) -> dict[str, Any]:
    """本人资料白名单字段；``created_at`` 取自内部 User（PublicUser 不含该字段）。"""
    record = auth_store.get_by_id(user.id) if auth_store is not None else None
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "created_at": _iso(getattr(record, "created_at", None)),
    }


def _api_keys(api_key_store: Any, user_id: str) -> list[dict[str, Any]]:
    """本人 API Key 元数据；不返回 ``key_hash``/明文（存储本身也不含明文）。"""
    if api_key_store is None:
        return []
    return [
        {
            "id": key.id,
            "name": key.name,
            "prefix": key.prefix,
            "created_at": _iso(key.created_at),
            "last_used_at": _iso(key.last_used_at),
            "expires_at": _iso(key.expires_at),
            "revoked": key.revoked_at is not None,
        }
        for key in api_key_store.list_for_user(user_id)
    ]


def _missions(initiatives: list[Any]) -> list[dict[str, Any]]:
    """任务元数据：不含阶段日志与产物正文（体积控制，PRD H3）。"""
    return [
        {
            "id": ini.id,
            "title": ini.title,
            "status": _status_text(ini.status),
            "current_stage": ini.current_stage,
            "created_at": _iso(ini.created_at),
            "updated_at": _iso(ini.updated_at),
        }
        for ini in initiatives
    ]


def _knowledge(
    knowledge_base_store: Any,
    knowledge_store: Any,
    user_id: str,
) -> list[dict[str, Any]]:
    """本人创建的知识库元数据；``doc_count`` 按 ``base_id`` 匹配文档计数。

    全局库（created_by 为空）不属于任何个人，不进入导出。
    """
    if knowledge_base_store is None:
        return []
    # doc_count 口径：base_id == 库 id 的文档数（含他人写入该库的文档，
    # 因为库本身属于当前用户，条数应与库内实际内容一致）。
    docs = knowledge_store.list() if knowledge_store is not None else []
    counts = Counter(getattr(doc, "base_id", "") for doc in docs)
    return [
        {
            "id": base.id,
            "name": base.name,
            "description": base.description,
            "doc_count": counts.get(base.id, 0),
            "created_at": _iso(base.created_at),
            "updated_at": _iso(base.updated_at),
        }
        for base in knowledge_base_store.list()
        if getattr(base, "created_by", "") == user_id
    ]


def _memory(memory_store: Any, user_id: str) -> list[dict[str, Any]]:
    """本人记忆条目（含正文）；``user_id`` 必传，否则会读到他人的 user 域记忆。"""
    if memory_store is None:
        return []
    entries = memory_store.list(scope="user", user_id=user_id)
    return [
        {
            "id": entry.id,
            "content": entry.content,
            "tags": list(entry.tags or []),
            "created_at": _iso(entry.created_at),
            "updated_at": _iso(entry.updated_at),
        }
        for entry in entries
    ]


def _room_message_count(room_store: Any, initiative_id: str) -> int:
    """房间消息条数：优先用 ``RoomStore.count`` 按行计数，避免整文件构造消息对象。

    旧存储实现没有 ``count`` 时回退到 ``list(limit=_ROOM_MESSAGE_LIMIT)``，
    保证不会因默认 500 条截断而少算（PRD rooms.message_count）。
    """
    count = getattr(room_store, "count", None)
    if callable(count):
        return int(count(initiative_id))
    return len(room_store.list(initiative_id, limit=_ROOM_MESSAGE_LIMIT))


def _rooms(missions: list[Any], room_store: Any) -> list[dict[str, Any]]:
    """本人参与的房间（每个可访问任务的主房间），只给消息条数不给正文。

    任务可见性即房间参与权：没有独立的房间成员表，创建者/被共享用户就是
    参与者。房间 id 直接复用任务 id（一个任务当前只有一个主房间）。
    """
    if room_store is None:
        return []
    out: list[dict[str, Any]] = []
    for ini in missions:
        out.append(
            {
                "id": ini.id,
                "title": ini.title,
                "created_at": _iso(ini.created_at),
                "message_count": _room_message_count(room_store, ini.id),
            }
        )
    return out


def build_user_export(
    user: PublicUser,
    *,
    auth_store: Any,
    api_key_store: Any,
    initiatives: list[Any],
    knowledge_base_store: Any,
    knowledge_store: Any,
    memory_store: Any,
    room_store: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    """组装当前用户的导出载荷（纯函数；存储以参数注入，便于单测）。

    输入：``user`` 为已鉴权的 ``PublicUser``；各 ``*_store`` 为对应存储单例。
    输出：可直接 ``json.dumps`` 的 dict，顶层仅 7 个 key（PRD E-1）。
    """
    missions = _my_missions(initiatives, user.id)
    return {
        "meta": {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "exported_at": (now or utc_now()).isoformat(),
            "owner": user.id,
            "app": APP_NAME,
        },
        "profile": _profile(user, auth_store),
        "api_keys": _api_keys(api_key_store, user.id),
        "missions": _missions(missions),
        "knowledge": _knowledge(knowledge_base_store, knowledge_store, user.id),
        "memory": _memory(memory_store, user.id),
        "rooms": _rooms(missions, room_store),
    }


#: 导出载荷的顶层分区顺序（PRD E-1：恰好这 7 个 key，不增不减）
EXPORT_TOP_LEVEL_KEYS = (
    "meta",
    "profile",
    "api_keys",
    "missions",
    "knowledge",
    "memory",
    "rooms",
)


class ExportTooLargeError(Exception):
    """导出载荷超过 ``MAX_EXPORT_BYTES``（PRD D-4）；由路由转换为 HTTP 413。

    单独定义而不是在序列化后判断长度：超大账号在一次性 ``json.dumps`` 时
    会额外占用 2–3 倍内存，可能先 OOM 成 500，拿不到明确报错。
    """


def serialize_export(payload: dict[str, Any]) -> bytes:
    """序列化为 UTF-8 JSON 字节串；``allow_nan=False`` 保证输出是合法 JSON。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def serialize_export_limited(
    payload: dict[str, Any],
    *,
    limit: int | None = None,
) -> bytes:
    """按顶层分区增量序列化，累计超过 ``limit`` 立即抛 ``ExportTooLargeError``。

    与 ``serialize_export`` 输出等价（同一组 key、同一紧凑格式），但避免先
    生成完整字符串再拷贝一次；超过上限时在分区边界提前中止（PRD D-4）。

    输入：``build_user_export`` 的返回值。
    输出：UTF-8 JSON 字节串；超限抛 ``ExportTooLargeError``。
    """
    limit = MAX_EXPORT_BYTES if limit is None else limit
    chunks = bytearray(b"{")
    for index, key in enumerate(EXPORT_TOP_LEVEL_KEYS):
        if index:
            chunks += b","
        chunks += json.dumps(key, ensure_ascii=False).encode("utf-8")
        chunks += b":"
        chunks += json.dumps(
            payload[key],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(chunks) > limit:
            raise ExportTooLargeError(
                f"导出载荷超过上限 {limit} 字节（已序列化分区：{key}）"
            )
    chunks += b"}"
    return bytes(chunks)


def export_filename(user: PublicUser, now: datetime | None = None) -> str:
    """下载文件名：``sortie-data-{username}-{YYYYMMDD-HHmmss}.json``（PRD C-1）。"""
    stamp = (now or utc_now()).strftime("%Y%m%d-%H%M%S")
    username = _UNSAFE_FILENAME_CHARS.sub("_", user.username).strip("_") or "user"
    return f"sortie-data-{username}-{stamp}.json"
