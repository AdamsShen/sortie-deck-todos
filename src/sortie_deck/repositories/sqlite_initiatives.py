from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from sortie_deck.models import Initiative, utc_now


class SqliteInitiativeStore:
    """SQLite-backed initiative repository (JSON document per row).

    并发说明：行级 upsert，可以支撑「多 mission 并行执行」与「多进程共享」。
    WAL 让读写不互相阻塞，busy_timeout 兜住多进程瞬时争抢（否则会直接
    抛 "database is locked"）。连接仍用进程内锁串行化——sqlite3 的
    check_same_thread=False 只允许跨线程使用，不保证交错事务安全。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS initiatives (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def upsert(self, initiative: Initiative) -> Initiative:
        initiative.updated_at = utc_now()
        payload = initiative.model_dump_json()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO initiatives (id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  payload=excluded.payload,
                  updated_at=excluded.updated_at
                """,
                (initiative.id, payload, initiative.updated_at.isoformat()),
            )
            self._conn.commit()
        return initiative

    def get(self, initiative_id: str) -> Initiative | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM initiatives WHERE id = ?", (initiative_id,)
            ).fetchone()
        if not row:
            return None
        return Initiative.model_validate(json.loads(row[0]))

    def list(self) -> list[Initiative]:
        with self._lock:
            rows = self._conn.execute("SELECT payload FROM initiatives").fetchall()
        items = [Initiative.model_validate(json.loads(r[0])) for r in rows]
        return sorted(items, key=lambda i: i.created_at, reverse=True)

    def delete(self, initiative_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM initiatives WHERE id = ?", (initiative_id,))
            self._conn.commit()
