from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from sortie_deck.models import Initiative, utc_now


class InitiativeStore:
    """JSON 文件存储（默认后端，单机单进程场景）。

    并发说明：文件本身是「整文件读-改-写」，两个协程交错 upsert 会互相覆盖。
    这里用进程内 RLock 串行化。**跨进程不安全**，多 worker 部署必须用
    ``TDT_STORAGE=sqlite``（见 runtime.validate_settings）。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def _load(self) -> dict:
        raw = self.path.read_text(encoding="utf-8") or "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 上次写入被强杀可能留下半截文件：保留现场再退回空表，
            # 否则进程永远起不来且没有任何提示。
            broken = self.path.with_suffix(self.path.suffix + ".corrupt")
            self.path.replace(broken)
            return {}

    def _save(self, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self.path)

    def upsert(self, initiative: Initiative) -> Initiative:
        initiative.updated_at = utc_now()
        with self._lock:
            data = self._load()
            data[initiative.id] = json.loads(initiative.model_dump_json())
            self._save(data)
        return initiative

    def get(self, initiative_id: str) -> Initiative | None:
        with self._lock:
            raw = self._load().get(initiative_id)
        if not raw:
            return None
        return Initiative.model_validate(raw)

    def list(self) -> list[Initiative]:
        with self._lock:
            data = self._load()
        items = [Initiative.model_validate(v) for v in data.values()]
        return sorted(items, key=lambda i: i.created_at, reverse=True)

    def delete(self, initiative_id: str) -> None:
        with self._lock:
            data = self._load()
            data.pop(initiative_id, None)
            self._save(data)
