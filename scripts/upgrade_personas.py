"""把本地 ``data/agents.json`` 里的内置专家同步到最新的人设文件。

**同步判据（2026-10-28 修正）**

旧实现用「正文里是否含 ``## 角色``」判断「是否已升级」。六段式快照一旦生成就
永远命中该条件，于是 ``packages/templates/personas/<slot>.md`` 后续的任何修改
都再也同步不进 ``data/agents.json``（product.md 在 2026-10-27 20:01 的更新就是
这样丢掉的）。

现在改为**内容比对**：用 ``load_persona_spec(slot)`` + seed 的 ``role_line`` 渲染
出「当前应有的正文」，与快照逐字比较，只有不一致才升级。

**如何避免覆盖人工修改**

- ``persona`` 与 ``persona_spec`` 的渲染结果不一致 → 工坊用自由文本改过，跳过；
- 侧车文件 ``data/agents.persona-sync.json`` 记录了上次同步写入的正文哈希，若当前
  正文哈希与之不符 → 同步之后又被人工改过，跳过；
- ``author_id`` 非空（用户/工坊自建的专家）→ 不归本脚本管，跳过；
- 以上三类确实要用 md 覆盖时，显式加 ``--force``。

默认 dry-run；``--apply`` 才写盘，写盘前备份为 ``agents.json.bak-<时间戳>``。

用法::

    .venv/bin/python scripts/upgrade_personas.py            # 预览
    .venv/bin/python scripts/upgrade_personas.py --apply    # 执行（自动备份）
    .venv/bin/python scripts/upgrade_personas.py --force    # 连人工改过的一起覆盖
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from sortie_deck.agents import _SEED
from sortie_deck.persona import PersonaSpec, render_persona
from sortie_deck.templates import load_persona_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_FILENAME = "agents.persona-sync.json"


def _digest(text: str) -> str:
    """人设正文的稳定指纹，用于判断「同步之后有没有被改过」。"""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _render_seed(seed: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """按「底座人设 + 专家特化首句」渲染出当前应有的正文与结构化 spec。"""
    spec = load_persona_spec(str(seed["slot"]))
    if seed.get("role_line"):
        # 只覆盖「角色」首句，用于表达该专家在流水线中的特化定位
        spec.role = str(seed["role_line"])
    return render_persona(spec).strip(), spec.model_dump(mode="json")


def _spec_conflict(row: dict[str, Any]) -> bool:
    """``persona`` 与 ``persona_spec`` 渲染结果不一致 → 正文被自由文本改过。"""
    spec = row.get("persona_spec")
    if not isinstance(spec, dict) or not spec:
        return False
    try:
        rendered = render_persona(PersonaSpec.model_validate(spec)).strip()
    except Exception:  # spec 结构损坏时按「已人工修改」处理，宁可不覆盖
        return True
    return rendered != str(row.get("persona") or "").strip()


def _edited_after_sync(row: dict[str, Any], record: dict[str, Any] | None) -> bool:
    """侧车记录过上次写入的正文哈希，与当前正文不符说明同步后又被改过。"""
    if not record:
        return False
    recorded = str(record.get("persona_hash") or "")
    if not recorded:
        return False
    return recorded != _digest(str(row.get("persona") or ""))


def _first_diff(old: str, new: str) -> str:
    """给 dry-run 输出用：定位第一处差异所在行，便于人工核对。"""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    for idx, (a, b) in enumerate(zip(old_lines, new_lines), start=1):
        if a != b:
            return f"第 {idx} 行：{a.strip()[:40] or '(空)'} → {b.strip()[:40] or '(空)'}"
    longer, shorter = (new_lines, old_lines) if len(new_lines) > len(old_lines) else (old_lines, new_lines)
    line = longer[len(shorter)].strip()[:40] if len(longer) > len(shorter) else ""
    return f"行数 {len(old_lines)} → {len(new_lines)}，首个新增/删除行：{line or '(空)'}"


def upgrade(
    rows: list[dict[str, Any]],
    sync: dict[str, Any] | None = None,
    *,
    force: bool = False,
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """按内容差异升级内置专家。

    返回 ``(changed, skipped)``：``changed`` 为正文被重写的行，``skipped`` 为
    ``(行, 跳过原因)``。``sync`` 是侧车状态，会就地补写已同步条目的哈希。
    """
    seeds = {seed["slug"]: seed for seed in _SEED}
    sync = sync if sync is not None else {}
    now = datetime.now().astimezone().isoformat()
    changed: list[dict[str, Any]] = []
    skipped: list[tuple[dict[str, Any], str]] = []

    for row in rows:
        seed = seeds.get(str(row.get("slug") or ""))
        if not seed:
            continue
        rendered, spec_dict = _render_seed(seed)
        current = str(row.get("persona") or "").strip()
        record = sync.get(str(row.get("slug")))

        if rendered == current:
            # 已与人设文件一致：只补齐侧车指纹，不动正文
            sync[str(row["slug"])] = {
                "source_hash": _digest(rendered),
                "persona_hash": _digest(current),
                "synced_at": (record or {}).get("synced_at") or now,
            }
            continue

        if not force:
            if row.get("author_id"):
                skipped.append((row, f"自建专家（author_id={row.get('author_id')}），不归本脚本管"))
                continue
            if _spec_conflict(row):
                skipped.append((row, "persona 与 persona_spec 不一致，疑似工坊手工改过"))
                continue
            if _edited_after_sync(row, record):
                skipped.append((row, "上次同步后正文被改过（侧车哈希不符）"))
                continue

        row["persona"] = rendered
        row["persona_spec"] = spec_dict
        row["version"] = int(row.get("version") or 1) + 1
        row["updated_at"] = now
        sync[str(row["slug"])] = {
            "source_hash": _digest(rendered),
            "persona_hash": _digest(rendered),
            "synced_at": now,
        }
        changed.append(row)

    return changed, skipped


def _load_sync(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true", help="连疑似人工改过的内置专家一起覆盖")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    path = data_dir / "agents.json"
    sync_path = data_dir / SYNC_FILENAME
    if not path.exists():
        print(f"未找到 {path}，无需迁移")
        return

    rows = json.loads(path.read_text(encoding="utf-8"))
    before = {str(r.get("slug")): str(r.get("persona") or "") for r in rows if r.get("slug")}
    sync = _load_sync(sync_path)
    changed, skipped = upgrade(rows, sync, force=args.force)

    print(f"可升级 {len(changed)} / {len(rows)} 个角色")
    for row in changed:
        slug = str(row.get("slug"))
        old = before.get(slug, "")
        new = str(row.get("persona") or "")
        print(f"  - {slug}  {row.get('title')}  {len(old)} → {len(new)} 字")
        print(f"      {_first_diff(old, new)}")
    for row, reason in skipped:
        print(f"  ! 跳过 {row.get('slug')}：{reason}")

    if not args.apply:
        if changed:
            print("dry-run：加 --apply 才会写盘")
        return

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"agents.json.bak-{stamp}")
    shutil.copy2(path, backup)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    sync_path.write_text(json.dumps(sync, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {path}，备份 {backup.name}，同步指纹 {sync_path.name}")


if __name__ == "__main__":
    main()
