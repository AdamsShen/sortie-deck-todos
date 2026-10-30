"""一次性回填：把历史任务 workspace 里的真产物覆盖到 artifacts/<stage>/。

背景：harness 阶段过去只用回复文本合成契约产物，真文件留在 workspace，任务
终态时 workspace 被删除。修复后新任务会直写产物目录；本脚本用于抢救仍保留
workspace 的存量任务（见 docs/plans/2026-10-30-artifact-direct-write.md）。

用法：
    python scripts/backfill_stage_artifacts.py --dry-run   # 只打印计划（默认）
    python scripts/backfill_stage_artifacts.py --apply     # 实际覆盖，备份 .from-workspace
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sortie_deck.contracts import REQUIRED  # noqa: E402


def _looks_like_wrapper(name: str, content: str) -> bool:
    """判断产物是否为旧的回复包装（只覆盖包装，不碰已是真内容或人工改过的文件）。"""
    head = content.lstrip()[:400]
    if head.startswith("# Harness stage reply"):
        return True
    if head.startswith(f"# {name} — stage `"):
        return True
    if name.endswith(".json"):
        try:
            data = json.loads(content)
        except ValueError:
            return False
        return isinstance(data, dict) and {"stage", "status", "summary"} <= set(data)
    return False


def _stage_for_name(stage_dirs: list[Path], name: str) -> Path | None:
    """按契约名反查阶段目录；同名产物出现在多个阶段时放弃（无法判断归属）。"""
    hits = [d for d in stage_dirs if name in REQUIRED.get(d.name, [])]
    return hits[0] if len(hits) == 1 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(ROOT / "data"), help="data 目录")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run）")
    args = parser.parse_args()

    missions_dir = Path(args.data_dir) / "missions"
    if not missions_dir.is_dir():
        print(f"没有找到 {missions_dir}")
        return 1

    planned = applied = skipped = 0
    for mission in sorted(missions_dir.iterdir()):
        workspace = mission / "workspace"
        artifacts = mission / "artifacts"
        if not workspace.is_dir() or not artifacts.is_dir():
            continue
        stage_dirs = [d for d in artifacts.iterdir() if d.is_dir() and not d.name.startswith(".")]
        for path in sorted(workspace.rglob("*")):
            if not path.is_file() or path.name in {"README.md"}:
                continue
            if any(part in {"repos", ".git", "node_modules"} for part in path.parts):
                continue
            target_stage = _stage_for_name(stage_dirs, path.name)
            if target_stage is None:
                continue
            target = target_stage / path.name
            try:
                old = target.read_text(encoding="utf-8") if target.exists() else ""
            except OSError:
                skipped += 1
                continue
            if old and not _looks_like_wrapper(path.name, old):
                skipped += 1
                continue
            planned += 1
            print(f"{'APPLY' if args.apply else 'DRY  '} {mission.name}/{target_stage.name}/{path.name}"
                  f" ← workspace ({path.stat().st_size} bytes)")
            if not args.apply:
                continue
            if target.exists():
                backup = target.with_suffix(target.suffix + ".from-workspace")
                backup.write_text(old, encoding="utf-8")
            target.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            applied += 1

    print(
        f"\n计划 {planned} 个文件，实际覆盖 {applied} 个，跳过（已非包装/无法读取）{skipped} 个。"
        + ("" if args.apply else " 加 --apply 执行。")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
