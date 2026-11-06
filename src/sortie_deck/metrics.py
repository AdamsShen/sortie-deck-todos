"""阶段指标落库（耗时 / 工具步数 / review 轮次）。

为什么需要：此前阶段数据只存在于 events.sqlite 的 ``stage_progress`` 事件流里，
查「某阶段跑了多久、调了多少次工具、自审几轮」只能按首末事件相减 + 数 activity
事件，无法在设置页直接查询。这里把每个阶段收尾时的汇总写成一行结构化记录。

写入是旁路：sqlite 失败只告警，绝不影响阶段执行（与 event_log 同一降级策略）。
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


class StageMetricsStore:
    """按 (initiative_id, stage, attempt) 追加的阶段指标表。

    同一任务同一阶段可能重跑（HITL 打回 / 失败重试），因此按追加多行设计：
    ``attempt`` 由写入时自动递增，查询方按 started_at 排序即可还原执行顺序。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stage_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                initiative_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                harness TEXT NOT NULL DEFAULT '',
                attempt INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                tool_calls INTEGER NOT NULL DEFAULT 0,
                review_rounds INTEGER NOT NULL DEFAULT 0,
                review_verdict TEXT NOT NULL DEFAULT '',
                meta TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stage_metrics_ini "
            "ON stage_metrics(initiative_id, started_at)"
        )
        self._conn.commit()

    def record(
        self,
        *,
        initiative_id: str,
        stage: str,
        role: str = "",
        harness: str = "",
        status: str = "",
        started_at: float,
        ended_at: float,
        duration_ms: int = 0,
        tool_calls: int = 0,
        review_rounds: int = 0,
        review_verdict: str = "",
        meta: dict[str, Any] | None = None,
    ) -> int:
        """写入一条阶段指标，返回自增 id（0 表示写入失败）。"""
        if not initiative_id or not stage:
            return 0
        # 调用方可以不传 duration_ms：按起止时间自算，store 内部保持自洽
        duration = int(duration_ms or 0)
        if duration <= 0:
            duration = int(max(0.0, float(ended_at) - float(started_at)) * 1000)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(attempt), 0) FROM stage_metrics "
                    "WHERE initiative_id = ? AND stage = ?",
                    (initiative_id, stage),
                ).fetchone()
                attempt = int(row[0] or 0) + 1
                cur = self._conn.execute(
                    """
                    INSERT INTO stage_metrics (
                        initiative_id, stage, role, harness, attempt, status,
                        started_at, ended_at, duration_ms, tool_calls,
                        review_rounds, review_verdict, meta, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        initiative_id,
                        stage,
                        role,
                        harness,
                        attempt,
                        status,
                        float(started_at),
                        float(ended_at),
                        max(0, duration),
                        max(0, int(tool_calls)),
                        max(0, int(review_rounds)),
                        review_verdict or "",
                        json.dumps(meta or {}, ensure_ascii=False, default=str),
                        time.time(),
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid or 0)
        except sqlite3.Error as exc:
            logger.warning(
                "阶段指标落库失败 initiative=%s stage=%s: %s", initiative_id, stage, exc
            )
            return 0

    def list_missions(self, initiative_ids: list[str]) -> dict[str, dict[str, Any]]:
        """按任务聚合指标：阶段数（去重）、累计耗时、工具步数、review 轮次、执行起止。

        执行起止取首阶段 MIN(started_at) 与末阶段 MAX(ended_at)，供列表展示「几点到几点」。
        它与 SUM(duration_ms) 口径不同（含阶段间隙与人工 HITL 等待），前端另算差值展示。
        """
        ids = [i for i in initiative_ids if i]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        try:
            with self._lock:
                rows = self._conn.execute(
                    f"""
                    SELECT initiative_id,
                           COUNT(DISTINCT stage) AS stage_count,
                           SUM(duration_ms) AS total_duration_ms,
                           SUM(tool_calls) AS tool_calls,
                           SUM(review_rounds) AS review_rounds,
                           MIN(started_at) AS first_started_at,
                           MAX(ended_at) AS last_ended_at
                    FROM stage_metrics
                    WHERE initiative_id IN ({placeholders})
                    GROUP BY initiative_id
                    """,
                    tuple(ids),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("阶段指标聚合失败: %s", exc)
            return {}
        return {
            str(row["initiative_id"]): {
                "stage_count": int(row["stage_count"] or 0),
                "total_duration_ms": int(row["total_duration_ms"] or 0),
                "tool_calls": int(row["tool_calls"] or 0),
                "review_rounds": int(row["review_rounds"] or 0),
                "first_started_at": float(row["first_started_at"] or 0.0),
                "last_ended_at": float(row["last_ended_at"] or 0.0),
            }
            for row in rows
        }

    def list_stages(self, initiative_id: str) -> list[dict[str, Any]]:
        """某任务的全部阶段执行记录（按开始时间升序，含重跑的多次 attempt）。"""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM stage_metrics WHERE initiative_id = ? "
                    "ORDER BY started_at ASC, id ASC",
                    (initiative_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("阶段指标查询失败 initiative=%s: %s", initiative_id, exc)
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["meta"] = json.loads(item.get("meta") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["meta"] = {}
            out.append(item)
        return out

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except sqlite3.Error:
            pass
