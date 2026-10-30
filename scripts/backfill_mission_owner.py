#!/usr/bin/env python3
"""历史任务归属回填：把 created_by 为空的任务归属到指定账号。

背景：旧版本只有 REST POST /api/initiatives 会写 created_by；规划官简报建队
与 CLI（sortie smoke）创建的任务没有归属。旧可见性规则把「无归属」当作全员
可见，普通成员因此能看到他人任务。现在权限已收紧为「无归属仅 admin 可见」，
本脚本负责把存量数据补齐，并可用 --share-with 把回填后的任务显式共享给成员。

用法：
    python scripts/backfill_mission_owner.py --dry-run          # 只打印计划
    python scripts/backfill_mission_owner.py                    # 归属给第一个 admin
    python scripts/backfill_mission_owner.py --owner admin --share-with chenmo
    python scripts/backfill_mission_owner.py --storage sqlite   # sqlite 后端

归属推断顺序：missions/<id>/chat/planner.state.json 的 owner_id → --owner 指定账号。
写盘前会生成 *.bak-<时间戳> 备份；--dry-run 不做任何写入。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 允许在仓库任意目录执行脚本
    sys.path.insert(0, str(REPO_ROOT))

from sortie_deck.models import MissionShare  # noqa: E402


def _die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    raw = path.read_text(encoding="utf-8") or "null"
    return json.loads(raw) or default


def _load_users(data_dir: Path) -> dict[str, dict[str, Any]]:
    """返回 username -> 用户记录（users.json 结构：{"users": [...]}）。"""
    raw = _load_json(data_dir / "users.json", {})
    users = raw.get("users", []) if isinstance(raw, dict) else raw
    return {u["username"]: u for u in users if u.get("username")}


def _resolve_owner(users: dict[str, dict[str, Any]], username: str | None) -> dict[str, Any]:
    if username:
        user = users.get(username)
        if not user:
            _die(f"owner user not found: {username}")
        return user
    for user in users.values():
        if user.get("role") == "admin":
            return user
    _die("no admin account found; pass --owner <username>")


def _resolve_shares(
    users: dict[str, dict[str, Any]], raw: str | None
) -> list[dict[str, Any]]:
    names = [n.strip() for n in (raw or "").split(",") if n.strip()]
    out = []
    for name in names:
        user = users.get(name)
        if not user:
            _die(f"share target not found: {name}")
        out.append(user)
    return out


def _briefing_owners(data_dir: Path) -> dict[str, str]:
    """missions/<id>/chat/planner.state.json 中 mission_id -> owner_id。

    旧版从全局 briefings.json 读取；该文件已按新布局约定移除，会话状态
    改为随任务目录存放，这里直接扫描任务目录。
    """
    from sortie_deck.mission_briefing import planner_owners

    return planner_owners(data_dir)


def _share_entry(user: dict[str, Any], added_by: str) -> dict[str, Any]:
    return MissionShare(
        user_id=user["id"],
        username=user["username"],
        display_name=user.get("display_name") or user["username"],
        added_by=added_by,
    ).model_dump(mode="json")


def _apply_shares(
    ini: dict[str, Any], owner_id: str, share_users: list[dict[str, Any]]
) -> list[str]:
    """给任务追加显式共享（幂等）：返回本次新增的用户名。"""
    shared = ini.setdefault("shared_with", [])
    existing = {s.get("user_id") for s in shared}
    added: list[str] = []
    for user in share_users:
        if user["id"] == owner_id or user["id"] in existing:
            continue
        shared.append(_share_entry(user, owner_id))
        existing.add(user["id"])
        added.append(user["username"])
    return added


def _backfill_json(
    path: Path,
    owner: dict[str, Any],
    share_users: list[dict[str, Any]],
    brief_owners: dict[str, str],
    *,
    dry_run: bool,
) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    changed: list[str] = []
    for ini_id, ini in data.items():
        if ini.get("created_by"):
            continue
        target_id = brief_owners.get(ini_id) or owner["id"]
        ini["created_by"] = target_id
        added = _apply_shares(ini, target_id, share_users)
        title = str(ini.get("title") or "")[:32]
        suffix = f" shared={','.join(added)}" if added else ""
        changed.append(f"{ini_id} | {title} | created_by={target_id}{suffix}")
    if changed and not dry_run:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"backup: {backup}")
    return changed


def _backfill_sqlite(
    path: Path,
    owner: dict[str, Any],
    share_users: list[dict[str, Any]],
    brief_owners: dict[str, str],
    *,
    dry_run: bool,
) -> list[str]:
    from sortie_deck.repositories.sqlite_initiatives import SqliteInitiativeStore

    store = SqliteInitiativeStore(path)
    changed: list[str] = []
    touched = []
    for ini in store.list():
        if ini.created_by:
            continue
        target_id = brief_owners.get(ini.id) or owner["id"]
        ini.created_by = target_id
        payload = ini.model_dump(mode="json")
        added = _apply_shares(payload, target_id, share_users)
        # model_dump 后的分享需要回写模型，直接重建避免字段遗漏
        ini.shared_with = [MissionShare.model_validate(s) for s in payload["shared_with"]]
        touched.append(ini)
        suffix = f" shared={','.join(added)}" if added else ""
        changed.append(f"{ini.id} | {ini.title[:32]} | created_by={target_id}{suffix}")
    if touched and not dry_run:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
        for ini in touched:
            store.upsert(ini)
        print(f"backup: {backup}")
    return changed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--data-dir",
        default=str(REPO_ROOT / "data"),
        help="数据目录（默认 <repo>/data）",
    )
    parser.add_argument("--storage", choices=["json", "sqlite"], default="json")
    parser.add_argument("--owner", default=None, help="归属用户名（默认第一个 admin）")
    parser.add_argument(
        "--share-with",
        default=None,
        help="逗号分隔的用户名：把本次回填的任务显式共享给这些账号",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    users = _load_users(data_dir)
    if not users:
        _die(f"no users found under {data_dir}")
    owner = _resolve_owner(users, args.owner)
    share_users = _resolve_shares(users, args.share_with)
    brief_owners = _briefing_owners(data_dir)

    if args.storage == "sqlite":
        path = data_dir / "initiatives.sqlite"
        if not path.exists() or path.stat().st_size == 0:
            _die(f"sqlite store not found or empty: {path}")
        changed = _backfill_sqlite(path, owner, share_users, brief_owners, dry_run=args.dry_run)
    else:
        path = data_dir / "initiatives.json"
        if not path.exists():
            _die(f"json store not found: {path}")
        changed = _backfill_json(path, owner, share_users, brief_owners, dry_run=args.dry_run)

    print(f"owner: {owner['username']} ({owner['id']})")
    print(f"storage: {args.storage} | pending backfill: {len(changed)}")
    for line in changed:
        print(f"  - {line}")
    if args.dry_run:
        print("dry-run: no files written")


if __name__ == "__main__":
    main()
