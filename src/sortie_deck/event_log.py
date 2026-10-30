"""mission 事件的持久化日志（多进程共享）。

为什么需要它：编排跑在 worker 进程，SSE 连在 API 进程，
进程内 asyncio.Queue 跨不过进程边界。事件落 sqlite 后任何进程都能按 seq
续读，断线重连也不会丢事件。

写入是同步 sqlite（与 store / run_queue 的既有风格一致）：单次 INSERT 在
WAL 下通常 <1ms，且事件频率是「每阶段若干条」而非高频流水。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 单条事件落库上限：超过则丢弃快照体，由消费方回读 store 补齐。
# 「update」事件带整个 mission dump，任务 timeline 很长时会膨胀到 MB 级。
MAX_EVENT_BYTES = 512 * 1024


class EventLog:
    """按 (initiative_id, seq) 追加读取的事件表。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mission_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                initiative_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mission_events_ini "
            "ON mission_events(initiative_id, seq)"
        )
        self._conn.commit()

    def append(self, initiative_id: str, event: dict[str, Any]) -> int:
        """追加一条事件，返回其全局递增 seq（0 表示写入失败）。"""
        payload = self._encode(initiative_id, event)
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO mission_events (initiative_id, payload, created_at) "
                    "VALUES (?, ?, ?)",
                    (initiative_id, payload, time.time()),
                )
                self._conn.commit()
                return int(cur.lastrowid or 0)
        except sqlite3.Error as exc:
            # 事件只是可观测性通道，落库失败不能拖垮 mission 执行
            logger.warning("事件落库失败 initiative=%s type=%s: %s", initiative_id, event.get("type"), exc)
            return 0

    def _encode(self, initiative_id: str, event: dict[str, Any]) -> str:
        raw = json.dumps(event, ensure_ascii=False, default=str)
        if len(raw.encode("utf-8")) <= MAX_EVENT_BYTES:
            return raw
        # 超大快照：只留结构，消费方（SSE）据 snapshot_omitted 回读 store
        slim = {k: v for k, v in event.items() if k != "initiative"}
        slim["snapshot_omitted"] = True
        slim["initiative_id"] = initiative_id
        logger.info("事件快照过大已省略 initiative=%s type=%s", initiative_id, event.get("type"))
        return json.dumps(slim, ensure_ascii=False, default=str)

    def since(
        self, initiative_id: str, after_seq: int, limit: int = 200
    ) -> list[tuple[int, dict[str, Any]]]:
        """读取 seq > after_seq 的事件（按 seq 升序，最多 limit 条）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, payload FROM mission_events "
                "WHERE initiative_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (initiative_id, int(after_seq), int(limit)),
            ).fetchall()
        out: list[tuple[int, dict[str, Any]]] = []
        for seq, payload in rows:
            try:
                out.append((int(seq), json.loads(payload)))
            except json.JSONDecodeError:
                logger.warning("事件解析失败已跳过 initiative=%s seq=%s", initiative_id, seq)
        return out

    def latest_seq(self, initiative_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(seq) FROM mission_events WHERE initiative_id = ?",
                (initiative_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def prune(self, retention_days: int) -> int:
        """清理超过保留期的事件，返回删除条数（0 = 不清理）。"""
        if retention_days <= 0:
            return 0
        cutoff = time.time() - float(retention_days) * 86400
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM mission_events WHERE created_at < ?", (cutoff,)
                )
                self._conn.commit()
                return int(cur.rowcount or 0)
        except sqlite3.Error as exc:
            logger.warning("事件清理失败: %s", exc)
            return 0

    def delete_mission(self, initiative_id: str) -> None:
        """任务解散时一并删除其事件，避免残留。"""
        try:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM mission_events WHERE initiative_id = ?", (initiative_id,)
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("事件删除失败 initiative=%s: %s", initiative_id, exc)

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except sqlite3.Error:
            pass
