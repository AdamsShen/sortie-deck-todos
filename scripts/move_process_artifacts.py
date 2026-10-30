"""把历史任务产物目录里的过程文件搬到 logs/<stage>/（产物区只留交付物）。

背景：harness 阶段过去把 reply.md / consult_log.md / 工具审计 JSON 一起写进
`artifacts/<stage>/`，而产物目录是面向用户展示的交付区。现在新任务会把过程文件
写到 `missions/<id>/logs/<stage>/`；本脚本负责收拾存量任务，并清理产物索引里
指向已搬走文件的引用。

用法：
    python scripts/move_process_artifacts.py            # dry-run
    python scripts/move_process_artifacts.py --apply    # 实际搬迁（sqlite 先备份）
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 与 sortie_deck.graph._PROCESS_ARTIFACT_NAMES 保持一致
PROCESS_NAMES = frozenset(
    {"reply.md", "consult_log.md", "toolkit_meta.json", "skill_runs.json", "mcp_calls.json"}
)


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        # 同名已存在：留档到 legacy/，避免直接覆盖历史
        legacy = dst.parent / "legacy" / f"{dst.name}.{time.strftime('%Y%m%dT%H%M%S')}"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        src.replace(legacy)
        return
    src.replace(dst)


def _cleanup_mission(mission: Path, *, apply: bool) -> list[str]:
    artifacts = mission / "artifacts"
    if not artifacts.is_dir():
        return []
    logs = mission / "logs"
    actions: list[str] = []
    for stage_dir in sorted(p for p in artifacts.iterdir() if p.is_dir() and not p.name.startswith(".")):
        stage_logs = logs / stage_dir.name
        for path in sorted(stage_dir.iterdir()):
            if path.is_dir() and path.name == ".runs":
                target = stage_logs / "runs"
                actions.append(f"{mission.name}/{stage_dir.name}/.runs → logs/{stage_dir.name}/runs")
                if apply:
                    target.mkdir(parents=True, exist_ok=True)
                    for child in path.iterdir():
                        _move(child, target / child.name)
                    path.rmdir()
                continue
            if not path.is_file():
                continue
            if path.name in PROCESS_NAMES:
                actions.append(f"{mission.name}/{stage_dir.name}/{path.name} → logs/{stage_dir.name}/")
                if apply:
                    _move(path, stage_logs / path.name)
            elif path.name.endswith(".from-workspace"):
                actions.append(
                    f"{mission.name}/{stage_dir.name}/{path.name} → logs/{stage_dir.name}/legacy/"
                )
                if apply:
                    _move(path, stage_logs / "legacy" / path.name)
        if apply and stage_dir.is_dir() and not any(stage_dir.iterdir()):
            stage_dir.rmdir()
    return actions


def _cleanup_index(data_dir: Path, *, apply: bool) -> tuple[int, int]:
    """清理产物索引：删除指向不存在文件或过程文件的引用。返回 (任务数, 删除引用数)。

    兼容两种后端：sqlite（当前默认）与仍在使用中的 initiatives.json（旧实例
    启动后尚未迁移时）。两边都清，避免重启前后行为不一致。
    """
    missions = data_dir / "missions"

    def _keep(refs: list[dict]) -> tuple[list[dict], int]:
        keep, dropped = [], 0
        for ref in refs:
            path = str(ref.get("path") or "")
            name = Path(path).name
            if name in PROCESS_NAMES or not (missions / path).exists():
                dropped += 1
                continue
            keep.append(ref)
        return keep, dropped

    touched = removed = 0

    sqlite_path = data_dir / "initiatives.sqlite"
    if sqlite_path.exists():
        from sortie_deck.repositories.sqlite_initiatives import SqliteInitiativeStore

        store = SqliteInitiativeStore(sqlite_path)
        for ini in store.list():
            refs = [a.model_dump(mode="json") for a in ini.artifacts]
            keep, dropped = _keep(refs)
            if not dropped:
                continue
            touched += 1
            removed += dropped
            if apply:
                from sortie_deck.models import ArtifactRef

                ini.artifacts = [ArtifactRef.model_validate(r) for r in keep]
                store.upsert(ini)
        if apply and removed:
            backup = sqlite_path.with_name(
                f"{sqlite_path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}"
            )
            shutil.copy2(sqlite_path, backup)

    json_path = data_dir / "initiatives.json"
    if json_path.exists():
        import json

        data = json.loads(json_path.read_text(encoding="utf-8") or "{}")
        rows = data.values() if isinstance(data, dict) else data
        json_touched = json_removed = 0
        for ini in rows:
            refs = list(ini.get("artifacts") or [])
            keep, dropped = _keep(refs)
            if not dropped:
                continue
            json_touched += 1
            json_removed += dropped
            ini["artifacts"] = keep
        if apply and json_removed:
            backup = json_path.with_name(
                f"{json_path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}"
            )
            shutil.copy2(json_path, backup)
            json_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        # 已有 sqlite 数据时以 sqlite 为准，避免重复计数
        if json_touched and not touched:
            touched, removed = json_touched, json_removed

    return touched, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--apply", action="store_true", help="实际搬迁（默认 dry-run）")
    args = parser.parse_args()

    missions = Path(args.data_dir) / "missions"
    if not missions.is_dir():
        print(f"没有找到 {missions}")
        return 1

    actions: list[str] = []
    for mission in sorted(missions.iterdir()):
        if mission.is_dir():
            actions.extend(_cleanup_mission(mission, apply=args.apply))
    for line in actions:
        print(f"{'MOVE' if args.apply else 'DRY '} {line}")

    touched, removed = _cleanup_index(Path(args.data_dir), apply=args.apply)
    print(
        f"\n文件搬迁 {len(actions)} 项；索引清理：{touched} 个任务、{removed} 条引用。"
        + ("" if args.apply else " 加 --apply 执行。")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
