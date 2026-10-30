"""``sortie mission`` 子命令：给脚本 / CI / 本地 Claude Code 用的任务入口。

两种运行模式：
- **embedded**（默认，未配置 url 时）：进程内直连 ``Orchestrator``，不起 HTTP，
  适合本地开发机。身份用 ``--owner`` 指定（默认第一个 admin）。
- **remote**（配置了 url/key）：走 HTTP API，适合 CI 或连远程服务。

输出约定（对齐 agent-friendly CLI）：
- ``--json`` 时 stdout 只有数据，日志/提示全部走 stderr；
- 退出码语义化：0 成功 / 2 用法 / 3 认证 / 4 不存在 / 5 冲突 / 6 参数 / 7 可重试；
- 不带 id 时按「显式 --id → 本地指针 → 服务端最新」解析，并打印来源。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5
EXIT_VALIDATION = 6
EXIT_TRANSIENT = 7

CONFIG_DIR = Path.home() / ".sortie"
CONFIG_PATH = CONFIG_DIR / "config.json"
POINTER_PATH = CONFIG_DIR / "last_mission.json"


# ---------------------------------------------------------------------------
# 本地配置与指针
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, payload: dict[str, Any], *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if private:
        path.chmod(0o600)


def load_config() -> dict[str, Any]:
    import os

    cfg = _read_json(CONFIG_PATH)
    url = (os.environ.get("SORTIE_URL") or cfg.get("url") or "").strip().rstrip("/")
    key = (os.environ.get("SORTIE_API_KEY") or cfg.get("key") or "").strip()
    return {"url": url, "key": key}


def cmd_login(args: Any) -> int:
    _write_json(CONFIG_PATH, {"url": args.url.rstrip("/"), "key": args.key}, private=True)
    print(f"已保存凭据到 {CONFIG_PATH}", file=sys.stderr)
    return EXIT_OK


def cmd_logout(args: Any) -> int:
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
    print("已删除本地凭据", file=sys.stderr)
    return EXIT_OK


def remember_mission(mission_id: str) -> None:
    cfg = load_config()
    _write_json(POINTER_PATH, {"url": cfg.get("url", ""), "mission_id": mission_id})


def _pointer_mission_id() -> str:
    cfg = load_config()
    ptr = _read_json(POINTER_PATH)
    if not ptr.get("mission_id"):
        return ""
    # 换了服务端就别复用旧指针
    if (ptr.get("url") or "") != cfg.get("url", ""):
        return ""
    return str(ptr["mission_id"])


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


class Printer:
    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def note(self, message: str) -> None:
        """提示信息永远走 stderr，保证 --json 时 stdout 干净。"""
        print(message, file=sys.stderr)

    def data(self, payload: Any, *, human: str | None = None) -> None:
        if self.as_json:
            print(json.dumps(payload, ensure_ascii=False, default=str))
        elif human is not None:
            print(human)
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _fail(code: str, message: str) -> int:
    print(json.dumps({"error": {"code": code, "message": message}}), file=sys.stderr)
    mapping = {
        "usage": EXIT_USAGE,
        "auth_required": EXIT_AUTH,
        "forbidden": EXIT_AUTH,
        "not_found": EXIT_NOT_FOUND,
        "conflict": EXIT_CONFLICT,
        "validation": EXIT_VALIDATION,
        "transient": EXIT_TRANSIENT,
    }
    return mapping.get(code, EXIT_VALIDATION)


# ---------------------------------------------------------------------------
# embedded 模式
# ---------------------------------------------------------------------------


def _resolve_principal(owner: str | None):
    from sortie_deck.service import Principal, get_auth_store

    auth = get_auth_store()
    if owner:
        user = auth.get_by_username(owner)
        if not user:
            raise LookupError(f"找不到用户: {owner}")
        from sortie_deck.service import _public  # noqa: PLC0415 — 内部转换

        return Principal(user=_public(user), source="cli")
    admins = [u for u in auth.list_users() if u.role == "admin"]
    if not admins:
        raise LookupError("没有 admin 账号，请用 --owner 指定")
    return Principal(user=admins[0], source="cli")


async def _drain_tasks() -> None:
    """取消并回收本事件循环里的其余任务。

    嵌入式模式下 mission 是通过 ``asyncio.create_task`` 跑的，进程退出前若不先
    收掉，``shutdown()`` 关掉 sqlite 连接后它们再写库会抛 "Connection closed"，
    任务也会停在半截（看起来像 failed）。
    """
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _with_orchestrator(fn):
    from sortie_deck.observability import init_tracing, shutdown_tracing
    from sortie_deck.orchestrator import Orchestrator
    from sortie_deck.runtime import configure_logging, ensure_runtime_dirs
    from sortie_deck.settings import settings

    configure_logging("WARNING")
    ensure_runtime_dirs(settings)
    init_tracing(settings)
    orch = Orchestrator()
    await orch.startup()
    try:
        return await fn(orch)
    finally:
        await _drain_tasks()
        await orch.shutdown()
        await asyncio.sleep(0.05)
        shutdown_tracing()


# ---------------------------------------------------------------------------
# remote 模式
# ---------------------------------------------------------------------------


class RemoteClient:
    def __init__(self, cfg: dict[str, Any]) -> None:
        import httpx

        self._client = httpx.Client(
            base_url=cfg["url"],
            headers={"Authorization": f"Bearer {cfg['key']}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    def __enter__(self) -> RemoteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        import httpx

        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ConnectionError(str(exc)) from exc
        if resp.status_code == 401:
            raise PermissionError(resp.text)
        if resp.status_code == 404:
            # 服务端错误体是 {"detail": "..."}，直接抛原文会让 CLI 打出嵌套 JSON
            detail = resp.text
            with contextlib.suppress(Exception):
                detail = str(resp.json().get("detail") or resp.text)
            raise LookupError(detail)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def start_mission(self, args: Any) -> dict[str, Any]:
        created = self.request(
            "POST",
            "/api/initiatives",
            json={
                "title": args.title or args.brief.strip().splitlines()[0][:80],
                "brief": args.brief,
                "template": args.track or "default",
                "pipeline_track": args.track,
                "coding_executor": "auto",
                "harness": args.harness,
            },
        )
        mission_id = created["id"]
        self.request("POST", f"/api/initiatives/{mission_id}/pipeline/confirm", json={})
        return self.request("POST", f"/api/initiatives/{mission_id}/start")

    def get(self, mission_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/initiatives/{mission_id}")

    def list_mine(self, *, status: str | None, limit: int | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"mine": "true"}
        if status:
            params["status"] = status
        if limit:
            params["limit"] = limit
        return self.request("GET", "/api/initiatives", params=params)

    def hitl(self, mission_id: str, action: str, instruction: str | None) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/initiatives/{mission_id}/hitl",
            json={"action": action, "instruction": instruction},
        )


# ---------------------------------------------------------------------------
# 任务渲染
# ---------------------------------------------------------------------------


def _mission_brief(ini: Any) -> dict[str, Any]:
    payload = ini if isinstance(ini, dict) else ini.model_dump(mode="json")
    hitl = payload.get("pending_hitl") or {}
    artifacts = payload.get("artifacts") or []
    return {
        "id": payload.get("id"),
        "title": payload.get("title"),
        "status": payload.get("status"),
        "stage": payload.get("current_stage"),
        "updated_at": payload.get("updated_at"),
        "hitl": {
            "stage": hitl.get("stage"),
            "prompt": hitl.get("prompt"),
            "options": [str(a) for a in (hitl.get("allowed_actions") or [])],
            "risk": hitl.get("risk"),
            "irreversible": hitl.get("irreversible"),
            "summary": hitl.get("artifact_summary"),
        }
        if hitl
        else None,
        "artifacts": len(artifacts),
    }


def _render_status(brief: dict[str, Any]) -> str:
    lines = [
        f"{brief['id']}  {brief['status']}  @ {brief['stage']}",
        f"  {brief['title']}",
        f"  产物: {brief['artifacts']} 个 · 更新: {brief['updated_at']}",
    ]
    hitl = brief.get("hitl")
    if hitl:
        lines.append(f"  门禁: {hitl['stage']} — {hitl['prompt']}")
        if hitl.get("summary"):
            lines.append(f"  摘要: {hitl['summary']}")
        if hitl.get("risk"):
            lines.append(f"  风险: {hitl['risk']}" + ("（不可逆）" if hitl.get("irreversible") else ""))
        if hitl.get("options"):
            lines.append(f"  可选: {' / '.join(hitl['options'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def _resolve_target(args: Any, remote: RemoteClient | None, principal: Any, printer: Printer) -> str:
    """解析目标任务：--id > 本地指针 > 服务端最新，并打印来源。"""
    explicit = getattr(args, "mission_id", None)
    if explicit:
        printer.note(f"→ 使用指定任务 {explicit}")
        return explicit

    pointer = _pointer_mission_id()
    if pointer:
        printer.note(f"→ 使用本地记录的最新任务 {pointer}（来自 {POINTER_PATH}）")
        return pointer

    if remote is not None:
        rows = remote.list_mine(status=None, limit=1)
        if rows:
            printer.note(f"→ 使用服务端最新任务 {rows[0]['id']}（我创建的、最近更新）")
            return str(rows[0]["id"])
    else:
        from sortie_deck.service import MissionService

        latest = MissionService().latest(principal)
        if latest is not None:
            printer.note(f"→ 使用服务端最新任务 {latest.id}（我创建的、最近更新）")
            return latest.id

    raise LookupError("没有可用任务：请先 sortie mission start，或用 --id 指定")


def cmd_mission_list(args: Any) -> int:
    printer = Printer(args.json)
    cfg = load_config()
    if cfg["url"]:
        try:
            with RemoteClient(cfg) as remote:
                rows = remote.list_mine(status=args.status, limit=args.limit)
            if args.json:
                printer.data(rows)
            else:
                for row in rows:
                    print(f"{row['id']}  {row['status']:<12} @ {row['current_stage']:<16} {row['title']}")
            return EXIT_OK
        except PermissionError:
            return _fail("auth_required", "凭据无效")
        except (ConnectionError, RuntimeError) as exc:
            return _fail("transient", str(exc))

    async def run(orch):
        from sortie_deck.service import MissionService

        principal = _resolve_principal(args.owner)
        rows = MissionService(orch).list_mine(principal, status=args.status, limit=args.limit)
        payload = [m.model_dump(mode="json") for m in rows]
        if args.json:
            printer.data(payload)
        else:
            for row in payload:
                print(f"{row['id']}  {row['status']:<12} @ {row['current_stage']:<16} {row['title']}")
        return EXIT_OK

    return asyncio.run(_with_orchestrator(run))


def cmd_mission_start(args: Any) -> int:
    printer = Printer(args.json)
    if not (args.brief or "").strip():
        return _fail("usage", "--brief 不能为空")
    if args.dry_run:
        printer.data(
            {
                "dry_run": True,
                "brief": args.brief,
                "track": args.track,
                "harness": args.harness,
                "owner": args.owner,
            },
            human=f"[dry-run] 将创建任务: {args.brief}（track={args.track or 'auto'}）",
        )
        return EXIT_OK

    cfg = load_config()
    if cfg["url"]:
        try:
            with RemoteClient(cfg) as remote:
                ini = remote.start_mission(args)
        except PermissionError:
            return _fail("auth_required", "凭据无效")
        except (ConnectionError, RuntimeError) as exc:
            return _fail("transient", str(exc))
        remember_mission(ini["id"])
        printer.note(f"→ 已创建 {ini['id']}")
        if args.wait:
            return _wait_remote(remote_cfg=cfg, mission_id=ini["id"], printer=printer)
        printer.data(_mission_brief(ini), human=_render_status(_mission_brief(ini)))
        return EXIT_OK

    async def run(orch):
        from sortie_deck.service import MissionService, ServiceError

        principal = _resolve_principal(args.owner)
        try:
            ini = await MissionService(orch).start_mission(
                principal,
                brief=args.brief,
                title=args.title,
                track=args.track,
                harness=args.harness,
            )
        except ServiceError as exc:
            return _fail(exc.code, exc.message)
        remember_mission(ini.id)
        printer.note(f"→ 已创建 {ini.id}")
        if not args.wait:
            # 嵌入式模式没有后台进程接管：任务跑在本进程里，进程一退就被取消，
            # 状态会停在 failed。所以这里必须等到门禁/终态；想让 start 立即返回，
            # 请用 sortie login 连远程服务，由云端 worker 持有任务。
            printer.note(
                "→ 嵌入式模式：任务由本进程执行，已等待到门禁/终态"
                "（远程模式可立即返回）"
            )
        return await _wait_embedded(orch, principal, ini.id, printer)

    return asyncio.run(_with_orchestrator(run))


def cmd_mission_status(args: Any) -> int:
    printer = Printer(args.json)
    cfg = load_config()
    if cfg["url"]:
        try:
            with RemoteClient(cfg) as remote:
                target = _resolve_target(args, remote, None, printer)
                ini = remote.get(target)
        except LookupError as exc:
            return _fail("not_found", str(exc))
        except PermissionError:
            return _fail("auth_required", "凭据无效")
        except (ConnectionError, RuntimeError) as exc:
            return _fail("transient", str(exc))
        brief = _mission_brief(ini)
        printer.data(brief, human=_render_status(brief))
        return EXIT_OK

    async def run(orch):
        from sortie_deck.service import MissionService

        principal = _resolve_principal(args.owner)
        svc = MissionService(orch)
        try:
            target = _resolve_target(args, None, principal, printer)
            ini = svc.get(principal, target)
        except LookupError as exc:
            return _fail("not_found", str(exc))
        brief = _mission_brief(ini)
        printer.data(brief, human=_render_status(brief))
        return EXIT_OK

    return asyncio.run(_with_orchestrator(run))


def _hitl_action(args: Any) -> tuple[str, str | None]:
    if args.stop:
        return "stop", None
    if args.reject:
        return "reject", args.instruction
    if args.instruction:
        return "edit_instruction", args.instruction
    return "approve", None


def cmd_mission_hitl(args: Any) -> int:
    printer = Printer(args.json)
    action, instruction = _hitl_action(args)
    cfg = load_config()

    if cfg["url"]:
        try:
            with RemoteClient(cfg) as remote:
                target = _resolve_target(args, remote, None, printer)
                ini = remote.hitl(target, action, instruction)
        except LookupError as exc:
            return _fail("not_found", str(exc))
        except PermissionError:
            return _fail("auth_required", "凭据无效")
        except RuntimeError as exc:
            return _fail("conflict", str(exc))
        except ConnectionError as exc:
            return _fail("transient", str(exc))
        brief = _mission_brief(ini)
        printer.data(brief, human=f"{action} 已提交 → {brief['status']} @ {brief['stage']}")
        return EXIT_OK

    async def run(orch):
        from sortie_deck.service import MissionService, ServiceError

        principal = _resolve_principal(args.owner)
        svc = MissionService(orch)
        try:
            target = _resolve_target(args, None, principal, printer)
            ini = await svc.hitl(
                principal, target, action=action, instruction=instruction
            )
        except (LookupError, ServiceError) as exc:
            code = getattr(exc, "code", "not_found")
            return _fail(code, getattr(exc, "message", str(exc)))
        brief = _mission_brief(ini)
        printer.data(brief, human=f"{action} 已提交 → {brief['status']} @ {brief['stage']}")
        return EXIT_OK

    return asyncio.run(_with_orchestrator(run))


def _artifact_candidates(mission_id: str, raw: str) -> list[str]:
    """把 ``--cat`` 的参数展开成 artifact store 里的候选相对路径。

    与 REST / MCP 共用同一套解析，避免"照着列表抄也 404"。
    """
    from sortie_deck.service import artifact_candidates

    return artifact_candidates(mission_id, raw)


def _match_by_name(refs: list[dict[str, Any]], raw: str) -> list[str]:
    """按文件名匹配产物（仅当用户只给了文件名时使用）。"""
    name = raw.strip().lstrip("/")
    if "/" in name or not name:
        return []
    hits = [
        str(ref.get("path", ""))
        for ref in refs
        if str(ref.get("path", "")).endswith("/" + name) or str(ref.get("kind", "")) == name
    ]
    return list(dict.fromkeys(hits))


def cmd_mission_artifacts(args: Any) -> int:
    printer = Printer(args.json)
    cfg = load_config()
    if cfg["url"]:
        try:
            with RemoteClient(cfg) as remote:
                target = _resolve_target(args, remote, None, printer)
                ini = remote.get(target)
                if args.cat:
                    refs = ini.get("artifacts") or []
                    candidates = _artifact_candidates(target, args.cat)
                    candidates += _match_by_name(refs, args.cat)
                    last: Exception | None = None
                    for cand in dict.fromkeys(candidates):
                        try:
                            content = remote.request(
                                "GET", f"/api/initiatives/{target}/artifacts/{cand}"
                            )
                            printer.data(content, human=content.get("content", ""))
                            return EXIT_OK
                        except LookupError as exc:
                            last = exc
                    if len(_match_by_name(refs, args.cat)) > 1:
                        names = ", ".join(_match_by_name(refs, args.cat))
                        return _fail(
                            "conflict", f"同名产物有多个，请写完整路径：{names}"
                        )
                    return _fail("not_found", f"产物不存在: {args.cat}（{last or ''}）")
                if not args.json:
                    for ref in ini.get("artifacts") or []:
                        print(f"{ref['stage']:<20} {ref['kind']:<28} {ref['path']}")
                    return EXIT_OK
                printer.data(ini.get("artifacts") or [])
                return EXIT_OK
        except LookupError as exc:
            return _fail("not_found", str(exc))
        except PermissionError:
            return _fail("auth_required", "凭据无效")
        except (ConnectionError, RuntimeError) as exc:
            return _fail("transient", str(exc))

    async def run(orch):
        from sortie_deck.service import MissionService, ServiceError

        principal = _resolve_principal(args.owner)
        svc = MissionService(orch)
        try:
            target = _resolve_target(args, None, principal, printer)
            refs = svc.artifacts(principal, target)
            if args.cat:
                flattened = [r.model_dump(mode="json") for r in refs]
                candidates = _artifact_candidates(target, args.cat)
                candidates += _match_by_name(flattened, args.cat)
                for cand in dict.fromkeys(candidates):
                    try:
                        content = svc.read_artifact(principal, target, cand)
                    except (LookupError, ServiceError):
                        continue
                    printer.data({"path": cand, "content": content}, human=content)
                    return EXIT_OK
                return _fail("not_found", f"产物不存在: {args.cat}")
        except (LookupError, ServiceError) as exc:
            code = getattr(exc, "code", "not_found")
            return _fail(code, getattr(exc, "message", str(exc)))
        payload = [r.model_dump(mode="json") for r in refs]
        if args.json:
            printer.data(payload)
        else:
            for ref in payload:
                print(f"{ref['stage']:<20} {ref['kind']:<28} {ref['path']}")
        return EXIT_OK

    return asyncio.run(_with_orchestrator(run))


def cmd_mission_watch(args: Any) -> int:
    printer = Printer(args.json)
    cfg = load_config()
    try:
        if cfg["url"]:
            with RemoteClient(cfg) as remote:
                target = _resolve_target(args, remote, None, printer)
                return _watch_loop(
                    lambda: remote.get(target),
                    printer,
                    lambda: None,
                )
        return asyncio.run(
            _with_orchestrator(lambda orch: _watch_embedded(orch, args, printer))
        )
    except KeyboardInterrupt:
        return 130
    except LookupError as exc:
        return _fail("not_found", str(exc))


def _watch_loop(fetch, printer: Printer, cleanup) -> int:
    import time

    last_key = None
    while True:
        try:
            ini = fetch()
        except Exception as exc:  # noqa: BLE001 — 网络抖动不终止 watch
            printer.note(f"读取失败，2s 后重试: {exc}")
            time.sleep(2)
            continue
        brief = _mission_brief(ini)
        key = (brief["status"], brief["stage"], bool(brief["hitl"]))
        if key != last_key:
            last_key = key
            if printer.as_json:
                printer.data(brief)
            else:
                print(_render_status(brief), flush=True)
        if brief["status"] in {"done", "failed", "stopped"}:
            cleanup()
            return EXIT_OK if brief["status"] == "done" else EXIT_CONFLICT
        time.sleep(2)


async def _watch_embedded(orch, args: Any, printer: Printer) -> int:
    from sortie_deck.service import MissionService

    principal = _resolve_principal(args.owner)
    svc = MissionService(orch)
    target = _resolve_target(args, None, principal, printer)
    last_key = None
    while True:
        ini = svc.get(principal, target)
        brief = _mission_brief(ini)
        key = (brief["status"], brief["stage"], bool(brief["hitl"]))
        if key != last_key:
            last_key = key
            if printer.as_json:
                printer.data(brief)
            else:
                print(_render_status(brief), flush=True)
        if brief["status"] in {"done", "failed", "stopped"}:
            return EXIT_OK if brief["status"] == "done" else EXIT_CONFLICT
        await asyncio.sleep(2)


def _wait_remote(*, remote_cfg: dict[str, Any], mission_id: str, printer: Printer) -> int:
    with RemoteClient(remote_cfg) as remote:
        return _watch_loop(lambda: remote.get(mission_id), printer, lambda: None)


async def _wait_embedded(orch, principal: Any, mission_id: str, printer: Printer) -> int:
    from sortie_deck.service import MissionService

    svc = MissionService(orch)
    last_key = None
    while True:
        ini = svc.get(principal, mission_id)
        brief = _mission_brief(ini)
        key = (brief["status"], brief["stage"], bool(brief["hitl"]))
        if key != last_key:
            last_key = key
            if printer.as_json:
                printer.data(brief)
            else:
                print(_render_status(brief), flush=True)
        # --wait 的语义是"停在需要人处理的地方"，不自动批门禁
        if brief["status"] in {"waiting_hitl", "done", "failed", "stopped"}:
            return EXIT_OK if brief["status"] in {"waiting_hitl", "done"} else EXIT_CONFLICT
        await asyncio.sleep(1.5)
