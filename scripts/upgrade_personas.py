"""把本地 ``data/agents.json`` 里未改过的内置专家升级为六段式人设。

只处理「slug 属于内置种子、且正文还不是六段式」的记录：人工改过的人设不会被
覆盖。默认 dry-run，加 ``--apply`` 才写盘，写盘前自动备份为
``agents.json.bak-<时间戳>``。

用法::

    python scripts/upgrade_personas.py            # 预览
    python scripts/upgrade_personas.py --apply    # 执行（自动备份）
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

from sortie_deck.agents import _SEED
from sortie_deck.persona import render_persona
from sortie_deck.templates import load_persona_spec

REPO_ROOT = Path(__file__).resolve().parents[1]


def upgrade(rows: list[dict]) -> list[dict]:
    seeds = {seed["slug"]: seed for seed in _SEED}
    changed: list[dict] = []
    for row in rows:
        seed = seeds.get(str(row.get("slug") or ""))
        if not seed:
            continue
        persona = str(row.get("persona") or "")
        if "## 角色" in persona:
            continue
        spec = load_persona_spec(seed["slot"])
        if seed.get("role_line"):
            spec.role = str(seed["role_line"])
        row["persona"] = render_persona(spec)
        row["persona_spec"] = spec.model_dump(mode="json")
        row["version"] = int(row.get("version") or 1) + 1
        row["updated_at"] = datetime.now().astimezone().isoformat()
        changed.append(row)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    path = Path(args.data_dir) / "agents.json"
    if not path.exists():
        print(f"未找到 {path}，无需迁移")
        return
    rows = json.loads(path.read_text(encoding="utf-8"))
    changed = upgrade(rows)
    print(f"可升级 {len(changed)} / {len(rows)} 个角色")
    for row in changed:
        print(f"  - {row.get('slug')}  {row.get('title')}")
    if not args.apply or not changed:
        if changed:
            print("dry-run：加 --apply 才会写盘")
        return

    backup = path.with_name(f"agents.json.bak-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(path, backup)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {path}，备份 {backup.name}")


if __name__ == "__main__":
    main()
