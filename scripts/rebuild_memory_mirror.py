"""把存量 active 记忆条目一次性回填到 WeKnora 镜像（幂等，可重复执行）。

用法（需先配置 WeKnora 连接与检索开关）：
    TDT_MEMORY_RETRIEVAL=weknora \
    TDT_WEKNORA_BASE_URL=http://127.0.0.1:8080 \
    TDT_WEKNORA_API_KEY=... \
    TDT_WEKNORA_MEMORY_KB_ID=kb_xxx \
    python scripts/rebuild_memory_mirror.py
"""

from __future__ import annotations

import sys

from sortie_deck.memory import MemoryStore
from sortie_deck.memory_mirror import get_memory_mirror
from sortie_deck.settings import settings


def main() -> int:
    if (settings.memory_retrieval or "local").strip().lower() != "weknora":
        print("TDT_MEMORY_RETRIEVAL 未设置为 weknora，退出")  # noqa: T201
        return 1
    store = MemoryStore(settings.memory_db)
    entries = store.list()  # 仅 active 条目
    mirror = get_memory_mirror()
    stats = mirror.backfill(entries)
    print(f"回填完成: {stats}（active 条目共 {len(entries)} 条）")  # noqa: T201
    return 0 if stats["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
