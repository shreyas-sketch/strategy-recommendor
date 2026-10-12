"""SQLite storage for Claude usage/cost tracking and the recommendation cache."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    month TEXT NOT NULL,
                    model TEXT NOT NULL,
                    served_model TEXT,
                    instrument TEXT,
                    horizon TEXT,
                    input_tokens INTEGER, output_tokens INTEGER,
                    cache_write_tokens INTEGER, cache_read_tokens INTEGER,
                    cost_usd REAL NOT NULL,
                    request_id TEXT
                );
                CREATE INDEX IF NOT EXISTS usage_month ON usage(month);
                CREATE TABLE IF NOT EXISTS rec_cache (
                    key TEXT PRIMARY KEY,
                    created REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )

    # ---- usage ----------------------------------------------------------
    @staticmethod
    def _month(now: datetime | None = None) -> str:
        return (now or datetime.now(timezone.utc)).strftime("%Y-%m")

    def record_usage(self, *, model: str, served_model: str, instrument: str, horizon: str,
                     input_tokens: int, output_tokens: int, cache_write_tokens: int,
                     cache_read_tokens: int, cost_usd: float, request_id: str | None) -> None:
        now = datetime.now(timezone.utc)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO usage (ts, month, model, served_model, instrument, horizon, input_tokens,"
                " output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, request_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (now.isoformat(), self._month(now), model, served_model, instrument, horizon,
                 input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, request_id),
            )

    def month_spend(self, month: str | None = None) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM usage WHERE month = ?", (month or self._month(),)
        ).fetchone()
        return float(row["s"])

    def usage_summary(self, month: str | None = None, recent: int = 25) -> dict:
        month = month or self._month()
        by_model = [dict(r) for r in self._conn.execute(
            "SELECT model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens,"
            " ROUND(SUM(cost_usd), 4) AS cost_usd FROM usage WHERE month = ? GROUP BY model", (month,))]
        recent_rows = [dict(r) for r in self._conn.execute(
            "SELECT ts, model, served_model, instrument, horizon, input_tokens, output_tokens,"
            " ROUND(cost_usd, 5) AS cost_usd FROM usage ORDER BY id DESC LIMIT ?", (recent,))]
        return {"month": month, "total_usd": round(self.month_spend(month), 4),
                "by_model": by_model, "recent": recent_rows}

    # ---- recommendation cache --------------------------------------------
    def cache_get(self, key: str, max_age_s: float, now: float) -> dict | None:
        row = self._conn.execute("SELECT created, payload FROM rec_cache WHERE key = ?", (key,)).fetchone()
        if row and now - row["created"] <= max_age_s:
            return json.loads(row["payload"])
        return None

    def cache_put(self, key: str, payload: dict, now: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO rec_cache (key, created, payload) VALUES (?,?,?)",
                (key, now, json.dumps(payload)),
            )
            # keep the table small
            self._conn.execute("DELETE FROM rec_cache WHERE created < ?", (now - 7 * 86400,))

    def cache_clear(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM rec_cache")
