from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RunJob:
    id: str
    initiative_id: str
    kind: str  # start | resume
    payload: dict[str, Any]
    worker_id: str | None = None


class SqliteRunQueue:
    """Durable run queue with lease-based claim for multi-worker safety."""

    def __init__(self, path: Path, *, lease_seconds: int = 120) -> None:
        self.path = path
        self.lease_seconds = max(5, int(lease_seconds))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_jobs (
                id TEXT PRIMARY KEY,
                initiative_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                worker_id TEXT,
                lease_until REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS initiative_locks (
                initiative_id TEXT PRIMARY KEY,
                worker_id TEXT NOT NULL,
                lease_until REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def enqueue(
        self,
        initiative_id: str,
        *,
        kind: str = "start",
        payload: dict[str, Any] | None = None,
    ) -> str:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO run_jobs
              (id, initiative_id, kind, payload, status, worker_id, lease_until, created_at, updated_at, error)
            VALUES (?, ?, ?, ?, 'pending', NULL, NULL, ?, ?, NULL)
            """,
            (job_id, initiative_id, kind, json.dumps(payload or {}), now, now),
        )
        self._conn.commit()
        return job_id

    def claim(self, worker_id: str) -> RunJob | None:
        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Release expired locks
            self._conn.execute(
                "DELETE FROM initiative_locks WHERE lease_until < ?", (now,)
            )
            row = self._conn.execute(
                """
                SELECT id, initiative_id, kind, payload FROM run_jobs
                WHERE status = 'pending'
                   OR (status = 'running' AND lease_until IS NOT NULL AND lease_until < ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                self._conn.commit()
                return None
            job_id, initiative_id, kind, payload_raw = row
            lock = self._conn.execute(
                "SELECT worker_id, lease_until FROM initiative_locks WHERE initiative_id = ?",
                (initiative_id,),
            ).fetchone()
            # 同一个 mission 同时只允许一个 job 在跑。这里**不再按 worker_id 放行**：
            # 两个副本若配了同名 worker（历史上默认就叫 worker-1），旧判断会让
            # 后来者直接覆盖租约，等于互斥形同虚设。租约过期由上面的 DELETE 处理，
            # 所以「锁存在且未过期」必然代表有人在跑，与是谁无关。
            if lock and lock[1] >= now:
                self._conn.commit()
                return None
            lease_until = now + self.lease_seconds
            self._conn.execute(
                """
                INSERT INTO initiative_locks (initiative_id, worker_id, lease_until)
                VALUES (?, ?, ?)
                ON CONFLICT(initiative_id) DO UPDATE SET
                  worker_id=excluded.worker_id,
                  lease_until=excluded.lease_until
                """,
                (initiative_id, worker_id, lease_until),
            )
            self._conn.execute(
                """
                UPDATE run_jobs
                SET status='running', worker_id=?, lease_until=?, updated_at=?
                WHERE id=?
                """,
                (worker_id, lease_until, now, job_id),
            )
            self._conn.commit()
            return RunJob(
                id=job_id,
                initiative_id=initiative_id,
                kind=kind,
                payload=json.loads(payload_raw or "{}"),
                worker_id=worker_id,
            )
        except Exception:
            self._conn.rollback()
            raise

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        now = time.time()
        lease_until = now + self.lease_seconds
        row = self._conn.execute(
            "SELECT initiative_id FROM run_jobs WHERE id=? AND worker_id=?",
            (job_id, worker_id),
        ).fetchone()
        if not row:
            return
        self._conn.execute(
            "UPDATE run_jobs SET lease_until=?, updated_at=? WHERE id=?",
            (lease_until, now, job_id),
        )
        self._conn.execute(
            "UPDATE initiative_locks SET lease_until=? WHERE initiative_id=?",
            (lease_until, row[0]),
        )
        self._conn.commit()

    def complete(self, job_id: str) -> None:
        now = time.time()
        row = self._conn.execute(
            "SELECT initiative_id FROM run_jobs WHERE id=?", (job_id,)
        ).fetchone()
        self._conn.execute(
            "UPDATE run_jobs SET status='done', updated_at=?, lease_until=NULL WHERE id=?",
            (now, job_id),
        )
        if row:
            self._conn.execute(
                "DELETE FROM initiative_locks WHERE initiative_id=?", (row[0],)
            )
        self._conn.commit()

    def fail(self, job_id: str, error: str) -> None:
        now = time.time()
        row = self._conn.execute(
            "SELECT initiative_id FROM run_jobs WHERE id=?", (job_id,)
        ).fetchone()
        self._conn.execute(
            """
            UPDATE run_jobs
            SET status='failed', error=?, updated_at=?, lease_until=NULL
            WHERE id=?
            """,
            (error[:2000], now, job_id),
        )
        if row:
            self._conn.execute(
                "DELETE FROM initiative_locks WHERE initiative_id=?", (row[0],)
            )
        self._conn.commit()

    def pending_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM run_jobs WHERE status IN ('pending','running')"
        ).fetchone()
        return int(row[0] if row else 0)
