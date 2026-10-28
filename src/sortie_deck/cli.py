from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import NoReturn

from sortie_deck.models import CreateInitiativeRequest, HitlAction, HitlDecision
from sortie_deck.orchestrator import Orchestrator


def _fail(message: str, code: int = 1) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _resolve_owner_id(owner_username: str | None) -> str:
    """解析 CLI 建队任务的归属用户 id。

    CLI 没有登录态，若不显式归属会建出 created_by 为空的孤儿任务
    （对所有登录账号可见）。默认归属第一个 admin，也可用 --owner 指定。
    """
    from sortie_deck.auth import AuthStore
    from sortie_deck.settings import settings

    auth = AuthStore(settings.users_db, settings.resolved_token_secret())
    if owner_username:
        user = auth.get_by_username(owner_username)
        if not user:
            _fail(f"owner user not found: {owner_username}")
        return user.id
    admin = next((u for u in auth.list_users() if u.role == "admin"), None)
    if not admin:
        _fail("no admin account found; pass --owner <username>")
    return admin.id


async def _smoke(brief: str, auto_approve: bool, owner_username: str | None = None) -> int:
    from sortie_deck import observability as obs
    from sortie_deck.settings import settings

    # CLI 不走 FastAPI lifespan：追踪必须在这里显式初始化，否则本地跑 smoke
    # 永远验证不了 span；收尾不 shutdown 的话 BatchSpanProcessor 队列里的会丢。
    obs.init_tracing(settings)
    owner_id = _resolve_owner_id(owner_username)
    orch = Orchestrator()
    await orch.startup()
    try:
        ini = await orch.create_and_start(
            CreateInitiativeRequest(
                title="CLI smoke",
                brief=brief,
                coding_executor="mock",
                # 冒烟契约是 mock 管线：显式固定 harness，否则会走默认真实 CLI
                harness="mock",
                auto_start=True,
                created_by=owner_id,
            )
        )
        print(f"created {ini.id} thread={ini.thread_id}")
        rail = (ini.meta or {}).get("pipeline_rail") or []
        if rail:
            labels = [
                (s.get("label") or s.get("id") if isinstance(s, dict) else str(s)) for s in rail
            ]
            print("pipeline stages:", " → ".join(labels))
        else:
            stages = (ini.meta or {}).get("pipeline_stages") or []
            print("pipeline stages:", " → ".join(str(s) for s in stages) or "(none)")

        # 30s 窗口：intake 走真实 LLM（pipeline_llm 默认开）时单阶段就可能几秒，
        # 原来的 12×0.4s=4.8s 会稳定超时，既看不到终态也看不到完整 trace
        for step in range(60):
            await asyncio.sleep(0.5)
            ini = orch.get(ini.id)
            assert ini
            print(f"[{step}] status={ini.status.value} stage={ini.current_stage}")
            if ini.status.value == "waiting_hitl" and ini.pending_hitl:
                print(f"  HITL at {ini.pending_hitl.stage}: {ini.pending_hitl.prompt}")
                if not auto_approve:
                    print(json.dumps(ini.model_dump(mode="json"), indent=2, ensure_ascii=False)[:2000])
                    return 0
                action = HitlAction.APPROVE
                instruction = None
                if ini.pending_hitl.stage == "qa_verify":
                    report_paths = [a for a in ini.artifacts if a.kind == "test_report.json"]
                    passed = False
                    if report_paths:
                        raw = orch.artifacts.read_json(report_paths[-1].path)
                        passed = bool(raw.get("passed"))
                    print(f"  qa passed={passed}")
                    if not passed and step >= 3:
                        instruction = "pass qa"
                        action = HitlAction.EDIT_INSTRUCTION
                ini = await orch.submit_hitl(
                    ini.id, HitlDecision(action=action, instruction=instruction)
                )
            if ini.status.value in {"done", "stopped", "failed"}:
                print("final:", ini.status.value)
                print("artifacts:", [a.path for a in ini.artifacts])
                return 0 if ini.status.value == "done" else 1
        print("error: smoke timed out before terminal status", file=sys.stderr)
        return 1
    finally:
        await orch.shutdown()
        # 编排跑在 asyncio.create_task 里：状态翻到 waiting_hitl 与任务真正返回之间
        # 还有几个 await。先让出事件循环，否则最后几个 span（含 mission.run）会在
        # provider 关闭之后才结束，被 BatchSpanProcessor 直接丢弃。
        await asyncio.sleep(0.05)
        obs.shutdown_tracing()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="sortie",
        description="Sortie Deck CLI — smoke the mission loop or start the API.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    smoke = sub.add_parser(
        "smoke",
        help="Run interrupt/resume smoke against the mock pipeline (auto-confirm + HITL).",
    )
    smoke.add_argument(
        "--brief",
        default="Add a preview health endpoint for the office portal",
        help="Mission brief used for pipeline planning",
    )
    smoke.add_argument(
        "--no-auto-approve",
        action="store_true",
        help="Stop at the first HITL instead of auto-approving",
    )
    smoke.add_argument(
        "--owner",
        default=None,
        help="Username that owns the created mission (default: first admin account)",
    )

    sub.add_parser("doctor", help="Check runtime readiness (secret, data dir, port)")

    knowledge = sub.add_parser(
        "knowledge",
        help="Enterprise KB helpers (import / catalog / retrieve pack for Cursor)",
    )
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_cmd", required=True)
    k_pack = knowledge_sub.add_parser(
        "pack",
        help="Retrieve (WeKnora hybrid-search or local) and write Cursor knowledge pack",
    )
    k_pack.add_argument("--brief", required=True, help="Mission brief / PRD text to match")
    k_pack.add_argument(
        "--workspace",
        default=".",
        help="Target workspace (writes .cursor/rules/sortie-knowledge.mdc)",
    )
    k_pack.add_argument("--limit", type=int, default=6)
    knowledge_sub.add_parser("status", help="Show KB backend / WeKnora connectivity")
    knowledge_sub.add_parser("sync", help="Sync local catalog from WeKnora directory")
    k_import = knowledge_sub.add_parser("import", help="Import text/url/file into enterprise KB")
    k_import.add_argument("--title", default=None)
    k_import.add_argument("--body", default=None, help="Markdown body (manual entry)")
    k_import.add_argument("--url", default=None)
    k_import.add_argument("--file", dest="file_path", default=None)
    k_import.add_argument("--folder", dest="folder_path", default="")
    k_import.add_argument("--tags", default="", help="Comma-separated tags")

    api = sub.add_parser("api", help="Start the FastAPI workbench backend (uvicorn)")
    api.add_argument("--host", default=None, help="Bind host (default: TDT_HOST)")
    api.add_argument("--port", type=int, default=None, help="Bind port (default: TDT_PORT)")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "doctor":
            from sortie_deck.runtime import run_doctor
            from sortie_deck.settings import settings

            report = run_doctor(settings)
            for check in report.checks:
                mark = "ok" if check.ok else "FAIL"
                print(f"[{mark}] {check.name}: {check.detail}")
            raise SystemExit(0 if report.ok else 1)
        if args.cmd == "knowledge":
            from pathlib import Path

            from sortie_deck.knowledge_gateway import (
                ImportKnowledgeRequest,
                gateway_from_settings,
            )
            from sortie_deck.knowledge_pack import load_pack_for_cursor
            from sortie_deck.settings import settings

            gw = gateway_from_settings(settings)
            if args.knowledge_cmd == "status":
                print(gw.status())
                raise SystemExit(0)
            if args.knowledge_cmd == "sync":
                print(gw.sync_catalog())
                raise SystemExit(0)
            if args.knowledge_cmd == "import":
                tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
                doc = gw.import_doc(
                    ImportKnowledgeRequest(
                        title=args.title,
                        body=args.body,
                        url=args.url,
                        file_path=args.file_path,
                        folder_path=args.folder_path or "",
                        tags=tags,
                    ),
                    author_id="cli",
                    author_name="CLI",
                )
                print(doc.model_dump(mode="json"))
                raise SystemExit(0)
            if args.knowledge_cmd == "pack":
                ws = Path(args.workspace).resolve()
                meta = load_pack_for_cursor(
                    gw.store, ws, args.brief, limit=args.limit, gateway=gw
                )
                hits = gw.retrieve(args.brief, limit=args.limit)
                print(f"workspace: {ws}")
                print(f"wrote: {meta}")
                for hit in hits:
                    print(f"  - {hit.score:.4f} {hit.title} [{hit.source}]")
                raise SystemExit(0)
        if args.cmd == "smoke":
            code = asyncio.run(
                _smoke(args.brief, auto_approve=not args.no_auto_approve, owner_username=args.owner)
            )
            raise SystemExit(code)
        if args.cmd == "api":
            import uvicorn

            from sortie_deck.runtime import (
                configure_logging,
                ensure_runtime_dirs,
                validate_settings,
            )
            from sortie_deck.settings import settings

            configure_logging(settings.log_level)
            ensure_runtime_dirs(settings)
            validate_settings(settings)
            uvicorn.run(
                "sortie_deck.api.app:app",
                host=args.host or settings.host,
                port=args.port or settings.port,
            )
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        _fail(str(exc))


if __name__ == "__main__":
    main()
