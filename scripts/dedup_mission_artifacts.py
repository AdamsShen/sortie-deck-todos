"""一次性清理：任务产物索引里同一路径的重复登记。

背景：并行阶段合并 upstream 时，同一文件会同时以裸 key（``tech_design.md``）
和阶段作用域 key（``eng_backend_tech_design::tech_design.md``）出现；旧版
同步逻辑只在函数入口拍一次 path 快照，导致同一路径被登记两次，前端产物区
出现重复文件。修复见 ``orchestrator._sync_artifacts_from_upstream``；本脚本
用于清理存量任务（保留首次登记，顺序不变）。

用法：
    python scripts/dedup_mission_artifacts.py            # dry-run，只打印计划
    python scripts/dedup_mission_artifacts.py --apply    # 实际写回

注意：服务运行中也可执行（WAL 允许并发写），但正在执行的阶段可能在脚本之后
用内存里的旧列表覆盖本行；建议在任务空闲时运行。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sortie_deck.repositories.sqlite_initiatives import SqliteInitiativeStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(ROOT / "data"), help="data 目录")
    parser.add_argument("--apply", action="store_true", help="实际写回（默认 dry-run）")
    args = parser.parse_args()

    db = Path(args.data_dir) / "initiatives.sqlite"
    if not db.is_file():
        print(f"没有找到 {db}")
        return 1

    store = SqliteInitiativeStore(db)
    total_removed = 0
    touched = 0
    for ini in store.list():
        seen: set[str] = set()
        kept = []
        for ref in ini.artifacts:
            path = str(ref.path).replace("\\", "/")
            if path in seen:
                continue
            seen.add(path)
            kept.append(ref)
        removed = len(ini.artifacts) - len(kept)
        if not removed:
            continue
        touched += 1
        total_removed += removed
        print(
            f"{'APPLY' if args.apply else 'DRY  '} {ini.id} {ini.title[:24]!r} "
            f"去掉 {removed} 条重复产物（{len(ini.artifacts)} → {len(kept)}）"
        )
        if args.apply:
            ini.artifacts = kept
            store.upsert(ini)

    print(
        f"\n涉及 {touched} 个任务，共 {total_removed} 条重复登记。"
        + ("" if args.apply else " 加 --apply 执行。")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
