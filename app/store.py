"""SQLite storage: usage/cost, caches, participants, sessions, portfolios, notifications, job locks.

Data-isolation rule: every participant-data method takes the user_id resolved from the
server-side session. No method looks up personal data by anything the client sends.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, month TEXT NOT NULL, model TEXT NOT NULL, served_model TEXT,
    instrument TEXT, horizon TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cache_write_tokens INTEGER, cache_read_tokens INTEGER,
    cost_usd REAL NOT NULL, request_id TEXT
);
CREATE INDEX IF NOT EXISTS usage_month ON usage(month);
CREATE TABLE IF NOT EXISTS rec_cache (key TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    notify_opt_in INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolios (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    holdings TEXT NOT NULL,
    unresolved TEXT NOT NULL DEFAULT '[]',
    source_name TEXT,
    uploaded TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_recs (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    payload TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS quotas (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day TEXT NOT NULL, kind TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, kind)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    day TEXT NOT NULL, status TEXT NOT NULL, detail TEXT, ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_day ON notifications(day, user_id);
CREATE TABLE IF NOT EXISTS job_runs (
    kind TEXT NOT NULL, day TEXT NOT NULL, started TEXT NOT NULL, finished TEXT, summary TEXT,
    PRIMARY KEY (kind, day)
);
"""


def token_hash(token: str) -> str:
    """Sessions and invites are stored hashed, so a leaked DB file does not leak live logins."""
    return hashlib.sha256(token.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._conn.executescript(SCHEMA)
            # Lightweight migrations for databases created by earlier versions.
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(users)")}
            if "profile" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN profile TEXT")

    def _all(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args)]

    def _one(self, sql: str, args=()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def _write(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, args)

    # ---- usage ----------------------------------------------------------
    @staticmethod
    def _month(now: datetime | None = None) -> str:
        return (now or datetime.now(timezone.utc)).strftime("%Y-%m")

    def record_usage(self, *, model: str, served_model: str, instrument: str, horizon: str,
                     input_tokens: int, output_tokens: int, cache_write_tokens: int,
                     cache_read_tokens: int, cost_usd: float, request_id: str | None) -> None:
        now = datetime.now(timezone.utc)
        self._write(
            "INSERT INTO usage (ts, month, model, served_model, instrument, horizon, input_tokens,"
            " output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, request_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now.isoformat(), self._month(now), model, served_model, instrument, horizon,
             input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, request_id))

    def month_spend(self, month: str | None = None) -> float:
        row = self._one("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM usage WHERE month = ?",
                        (month or self._month(),))
        return float(row["s"])

    def usage_summary(self, month: str | None = None, recent: int = 25) -> dict:
        month = month or self._month()
        by_model = self._all(
            "SELECT model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens,"
            " ROUND(SUM(cost_usd), 4) AS cost_usd FROM usage WHERE month = ? GROUP BY model", (month,))
        recent_rows = self._all(
            "SELECT ts, model, served_model, instrument, horizon, input_tokens, output_tokens,"
            " ROUND(cost_usd, 5) AS cost_usd FROM usage ORDER BY id DESC LIMIT ?", (recent,))
        return {"month": month, "total_usd": round(self.month_spend(month), 4),
                "by_model": by_model, "recent": recent_rows}

    # ---- market recommendation cache (not personal data) ------------------
    def cache_get(self, key: str, max_age_s: float, now: float) -> dict | None:
        row = self._one("SELECT created, payload FROM rec_cache WHERE key = ?", (key,))
        if row and now - row["created"] <= max_age_s:
            return json.loads(row["payload"])
        return None

    def cache_put(self, key: str, payload: dict, now: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO rec_cache (key, created, payload) VALUES (?,?,?)",
                               (key, now, json.dumps(payload)))
            self._conn.execute("DELETE FROM rec_cache WHERE created < ?", (now - 7 * 86400,))

    def cache_clear(self) -> None:
        self._write("DELETE FROM rec_cache")
        self._write("DELETE FROM daily_recs")

    # ---- participants -----------------------------------------------------
    def create_user(self, name: str, phone: str) -> int:
        try:
            cur = self._write("INSERT INTO users (name, phone, created) VALUES (?,?,?)", (name, phone, _now_iso()))
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"A participant with phone {phone} already exists") from exc
        return int(cur.lastrowid)

    def get_user(self, user_id: int) -> dict | None:
        return self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    def get_user_by_phone(self, phone: str) -> dict | None:
        return self._one("SELECT * FROM users WHERE phone = ?", (phone,))

    def list_users(self) -> list[dict]:
        return self._all(
            "SELECT u.id, u.name, u.phone, u.active, u.notify_opt_in, u.created, u.last_login,"
            " u.password_hash IS NOT NULL AS activated, u.profile IS NOT NULL AS has_profile,"
            " p.uploaded AS portfolio_uploaded, json_array_length(p.holdings) AS holdings_count,"
            " (SELECT status FROM notifications n WHERE n.user_id = u.id ORDER BY n.id DESC LIMIT 1) AS last_notification"
            " FROM users u LEFT JOIN portfolios p ON p.user_id = u.id ORDER BY u.name")

    def update_user(self, user_id: int, **fields) -> None:
        allowed = {"name", "active", "notify_opt_in", "password_hash", "last_login"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return
        sets = ", ".join(f"{k} = ?" for k in cols)
        self._write(f"UPDATE users SET {sets} WHERE id = ?", (*cols.values(), user_id))

    def get_profile(self, user_id: int) -> dict | None:
        row = self._one("SELECT profile FROM users WHERE id = ?", (user_id,))
        return json.loads(row["profile"]) if row and row["profile"] else None

    def set_profile(self, user_id: int, profile: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE users SET profile = ? WHERE id = ?", (json.dumps(profile), user_id))
            self._conn.execute("DELETE FROM daily_recs WHERE user_id = ?", (user_id,))   # re-pick with new profile

    def delete_user(self, user_id: int) -> None:
        # ON DELETE CASCADE removes sessions, invites, portfolio, recommendations, quotas, notifications.
        self._write("DELETE FROM users WHERE id = ?", (user_id,))

    def opted_in_users(self) -> list[dict]:
        return self._all("SELECT * FROM users WHERE active = 1 AND notify_opt_in = 1 ORDER BY id")

    # ---- invites & sessions -----------------------------------------------
    def create_invite(self, user_id: int, token: str, ttl_s: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM invites WHERE user_id = ?", (user_id,))
            self._conn.execute("INSERT INTO invites (token_hash, user_id, expires) VALUES (?,?,?)",
                               (token_hash(token), user_id, time.time() + ttl_s))

    def consume_invite(self, token: str) -> int | None:
        h = token_hash(token)
        with self._lock, self._conn:
            row = self._conn.execute("SELECT user_id, expires FROM invites WHERE token_hash = ?", (h,)).fetchone()
            self._conn.execute("DELETE FROM invites WHERE token_hash = ?", (h,))
        if row and row["expires"] >= time.time():
            return int(row["user_id"])
        return None

    def create_session(self, user_id: int, token: str, ttl_s: float) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            self._conn.execute("INSERT INTO sessions (token_hash, user_id, expires) VALUES (?,?,?)",
                               (token_hash(token), user_id, now + ttl_s))

    def session_user(self, token: str) -> dict | None:
        row = self._one(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = ? AND s.expires >= ? AND u.active = 1", (token_hash(token), time.time()))
        return row

    def delete_session(self, token: str) -> None:
        self._write("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def delete_user_sessions(self, user_id: int) -> None:
        self._write("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # ---- portfolios ---------------------------------------------------------
    def save_portfolio(self, user_id: int, holdings: list[dict], unresolved: list[str], source_name: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO portfolios (user_id, holdings, unresolved, source_name, uploaded)"
                " VALUES (?,?,?,?,?)", (user_id, json.dumps(holdings), json.dumps(unresolved), source_name, _now_iso()))
            self._conn.execute("DELETE FROM daily_recs WHERE user_id = ?", (user_id,))

    def get_portfolio(self, user_id: int) -> dict | None:
        row = self._one("SELECT holdings, unresolved, source_name, uploaded FROM portfolios WHERE user_id = ?",
                        (user_id,))
        if row:
            row["holdings"] = json.loads(row["holdings"])
            row["unresolved"] = json.loads(row["unresolved"])
        return row

    def delete_portfolio(self, user_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM portfolios WHERE user_id = ?", (user_id,))
            self._conn.execute("DELETE FROM daily_recs WHERE user_id = ?", (user_id,))

    # ---- personal daily recommendations ----------------------------------
    def get_daily_rec(self, user_id: int, day: str) -> dict | None:
        row = self._one("SELECT payload FROM daily_recs WHERE user_id = ? AND day = ?", (user_id, day))
        return json.loads(row["payload"]) if row else None

    def put_daily_rec(self, user_id: int, day: str, payload: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO daily_recs (user_id, day, payload, created) VALUES (?,?,?,?)",
                               (user_id, day, json.dumps(payload), time.time()))
            self._conn.execute("DELETE FROM daily_recs WHERE created < ?", (time.time() - 14 * 86400,))

    # ---- per-user quotas ----------------------------------------------------
    def take_quota(self, user_id: int, day: str, kind: str, limit: int) -> bool:
        """Atomically consume one unit; False if the daily limit is already reached."""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT count FROM quotas WHERE user_id=? AND day=? AND kind=?",
                                     (user_id, day, kind)).fetchone()
            if row and row["count"] >= limit:
                return False
            self._conn.execute(
                "INSERT INTO quotas (user_id, day, kind, count) VALUES (?,?,?,1)"
                " ON CONFLICT(user_id, day, kind) DO UPDATE SET count = count + 1", (user_id, day, kind))
            return True

    # ---- notifications & job locks ---------------------------------------
    def log_notification(self, user_id: int | None, day: str, status: str, detail: str = "") -> None:
        self._write("INSERT INTO notifications (user_id, day, status, detail, ts) VALUES (?,?,?,?,?)",
                    (user_id, day, status, detail[:500], _now_iso()))

    def notified_today(self, user_id: int, day: str) -> bool:
        return self._one("SELECT 1 AS x FROM notifications WHERE user_id = ? AND day = ? AND status = 'sent'",
                         (user_id, day)) is not None

    def recent_notifications(self, limit: int = 50) -> list[dict]:
        return self._all(
            "SELECT n.ts, n.day, n.status, n.detail, u.name FROM notifications n"
            " LEFT JOIN users u ON u.id = n.user_id ORDER BY n.id DESC LIMIT ?", (limit,))

    def claim_job(self, kind: str, day: str) -> bool:
        """Insert-or-fail lock so a daily job runs once even across restarts or extra workers."""
        try:
            self._write("INSERT INTO job_runs (kind, day, started) VALUES (?,?,?)", (kind, day, _now_iso()))
            return True
        except sqlite3.IntegrityError:
            return False

    def finish_job(self, kind: str, day: str, summary: dict) -> None:
        self._write("UPDATE job_runs SET finished = ?, summary = ? WHERE kind = ? AND day = ?",
                    (_now_iso(), json.dumps(summary), kind, day))

    def release_job(self, kind: str, day: str) -> None:
        self._write("DELETE FROM job_runs WHERE kind = ? AND day = ?", (kind, day))

    def recent_jobs(self, limit: int = 14) -> list[dict]:
        return self._all("SELECT * FROM job_runs ORDER BY started DESC LIMIT ?", (limit,))
