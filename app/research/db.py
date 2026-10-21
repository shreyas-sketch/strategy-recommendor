"""SQLite storage for the research module (data/research/research.db).

Uploaded files are kept byte-for-byte under data/research/raw/<sha256>.<ext>; everything in the
tables is a processed copy that can be rebuilt from them. Natural keys make re-imports idempotent.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,                 -- market | strategy_pnl
    target TEXT NOT NULL,               -- symbol or strategy id
    filename TEXT NOT NULL,
    stored_as TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    uploaded_by TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL,
    rows_in_file INTEGER NOT NULL DEFAULT 0,
    rows_added INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    rows_conflicting INTEGER NOT NULL DEFAULT 0,
    first_date TEXT, last_date TEXT,
    details TEXT NOT NULL DEFAULT '{}'  -- column mapping, interval, warnings
);
CREATE TABLE IF NOT EXISTS market_bars (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,             -- 1m | 1d
    ts INTEGER NOT NULL,                -- bar open, epoch seconds (UTC)
    day TEXT NOT NULL,                  -- IST trading date YYYY-MM-DD
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL,
    vwap REAL, st_line REAL, st_dir INTEGER, ema REAL, rsi REAL, rsi_ma REAL,
    import_id INTEGER,
    PRIMARY KEY (symbol, interval, ts)
);
CREATE INDEX IF NOT EXISTS market_bars_day ON market_bars(symbol, interval, day);
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    underlying TEXT NOT NULL,           -- index key whose regime describes it (NIFTY50, SENSEX, BANKNIFTY)
    margin REAL NOT NULL,               -- capital the P&L is earned on (Rs)
    cost_per_active_day REAL NOT NULL DEFAULT 0,   -- estimated charges on a day it trades (Rs)
    tradetron_id TEXT NOT NULL DEFAULT '',
    config_strategy_id TEXT NOT NULL DEFAULT '',   -- link to the existing catalog, if any
    notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS strategy_days (
    strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    gross_pnl REAL NOT NULL,
    charges REAL,                       -- from the file when it has a charges column, else NULL
    trades INTEGER,
    import_id INTEGER,
    PRIMARY KEY (strategy_id, day)
);
CREATE TABLE IF NOT EXISTS recommendations (
    target_day TEXT NOT NULL,
    engine TEXT NOT NULL,
    created_at TEXT NOT NULL,
    cutoff_day TEXT NOT NULL,           -- last session whose data was used
    model_version TEXT NOT NULL,
    top_strategy TEXT,                  -- NULL = no strategy met the criteria
    payload TEXT NOT NULL,
    actual TEXT,                        -- filled in once that day's P&L is uploaded
    PRIMARY KEY (target_day, engine)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

BAR_COLS = ("open", "high", "low", "close", "volume", "vwap", "st_line", "st_dir", "ema", "rsi", "rsi_ma")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ResearchDB:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.raw_dir = self.root / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "research.db"
        self._lock = threading.RLock()
        self._open()

    def _open(self) -> None:
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- helpers ----------------------------------------------------------
    def all(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        with self._lock:
            row = self.conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def write(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def get_kv(self, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_kv(self, key: str, value) -> None:
        self.write("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, json.dumps(value)))

    # ---- imports ----------------------------------------------------------
    def import_by_sha(self, sha: str) -> dict | None:
        return self.one("SELECT * FROM imports WHERE sha256 = ?", (sha,))

    def save_raw(self, sha: str, ext: str, raw: bytes) -> str:
        name = f"{sha}{ext}"
        path = self.raw_dir / name
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(raw)
            tmp.replace(path)
        return name

    def record_import(self, **row) -> int:
        row.setdefault("uploaded_at", now_iso())
        row["details"] = json.dumps(row.get("details") or {})
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        return self.write(f"INSERT INTO imports({cols}) VALUES({marks})", tuple(row.values())).lastrowid

    def list_imports(self) -> list[dict]:
        rows = self.all("SELECT * FROM imports ORDER BY id DESC")
        for r in rows:
            r["details"] = json.loads(r["details"])
        return rows

    # ---- market bars ------------------------------------------------------
    def upsert_bars(self, symbol: str, interval: str, rows: list[tuple], import_id: int | None,
                    replace: bool = False) -> dict:
        """rows: (ts, day, open, high, low, close, volume, vwap, st_line, st_dir, ema, rsi, rsi_ma)."""
        added = skipped = conflicting = 0
        with self._lock:
            cur = self.conn.cursor()
            existing = {r[0]: r[1:] for r in cur.execute(
                "SELECT ts, open, high, low, close FROM market_bars WHERE symbol = ? AND interval = ? "
                "AND ts BETWEEN ? AND ?", (symbol, interval, rows[0][0], rows[-1][0]))} if rows else {}
            new = []
            for r in rows:
                old = existing.get(r[0])
                if old is None:
                    new.append(r)
                    added += 1
                elif all(abs((a or 0) - (b or 0)) < 1e-6 for a, b in zip(old, r[2:6])):
                    skipped += 1
                else:
                    conflicting += 1
                    if replace:
                        new.append(r)
            verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
            cur.executemany(
                f"{verb} INTO market_bars(symbol, interval, ts, day, {', '.join(BAR_COLS)}, import_id) "
                f"VALUES (?, ?, ?, ?, {', '.join('?' for _ in BAR_COLS)}, ?)",
                [(symbol, interval, *r, import_id) for r in new])
            self.conn.commit()
        return {"added": added, "skipped": skipped, "conflicting": conflicting}

    def market_summary(self) -> list[dict]:
        return self.all("SELECT symbol, interval, COUNT(*) AS bars, COUNT(DISTINCT day) AS days, MIN(day) AS first_day, "
                        "MAX(day) AS last_day FROM market_bars GROUP BY symbol, interval ORDER BY symbol, interval")

    # ---- strategies ---------------------------------------------------------
    def list_strategies(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM strategies" + (" WHERE active = 1" if active_only else "") + " ORDER BY name"
        return self.all(sql)

    def get_strategy(self, sid: str) -> dict | None:
        return self.one("SELECT * FROM strategies WHERE id = ?", (sid,))

    def upsert_strategy(self, s: dict) -> None:
        cols = ("id", "name", "underlying", "margin", "cost_per_active_day", "tradetron_id", "config_strategy_id",
                "notes", "active")
        vals = tuple(s.get(c, d) for c, d in zip(cols, (None, None, None, None, 0, "", "", "", 1)))
        self.write(f"INSERT INTO strategies({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)}) "
                   f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols[1:])}", vals)

    def upsert_strategy_days(self, sid: str, rows: list[tuple], import_id: int | None, replace: bool = False) -> dict:
        """rows: (day, gross_pnl, charges|None, trades|None)."""
        added = skipped = conflicting = 0
        with self._lock:
            cur = self.conn.cursor()
            existing = {r[0]: r[1] for r in cur.execute(
                "SELECT day, gross_pnl FROM strategy_days WHERE strategy_id = ?", (sid,))}
            new = []
            for r in rows:
                old = existing.get(r[0])
                if old is None:
                    new.append(r)
                    added += 1
                elif abs(old - r[1]) < 0.005:
                    skipped += 1
                else:
                    conflicting += 1
                    if replace:
                        new.append(r)
            verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
            cur.executemany(f"{verb} INTO strategy_days(strategy_id, day, gross_pnl, charges, trades, import_id) "
                            "VALUES (?, ?, ?, ?, ?, ?)", [(sid, *r, import_id) for r in new])
            self.conn.commit()
        return {"added": added, "skipped": skipped, "conflicting": conflicting}

    def strategy_summary(self) -> list[dict]:
        return self.all("SELECT s.id, s.name, COUNT(d.day) AS days, MIN(d.day) AS first_day, MAX(d.day) AS last_day "
                        "FROM strategies s LEFT JOIN strategy_days d ON d.strategy_id = s.id GROUP BY s.id ORDER BY s.name")

    # ---- recommendations -----------------------------------------------------
    def save_recommendation(self, rec: dict) -> None:
        self.write("INSERT INTO recommendations(target_day, engine, created_at, cutoff_day, model_version, top_strategy, "
                   "payload, actual) VALUES(?, ?, ?, ?, ?, ?, ?, NULL) ON CONFLICT(target_day, engine) DO UPDATE SET "
                   "created_at = excluded.created_at, cutoff_day = excluded.cutoff_day, model_version = "
                   "excluded.model_version, top_strategy = excluded.top_strategy, payload = excluded.payload",
                   (rec["target_day"], rec["engine"], now_iso(), rec["cutoff_day"], rec["model_version"],
                    rec.get("top_strategy"), json.dumps(rec)))

    def set_actual(self, target_day: str, engine: str, actual: dict) -> None:
        self.write("UPDATE recommendations SET actual = ? WHERE target_day = ? AND engine = ?",
                   (json.dumps(actual), target_day, engine))

    def list_recommendations(self, limit: int = 250) -> list[dict]:
        rows = self.all("SELECT * FROM recommendations ORDER BY target_day DESC, engine LIMIT ?", (limit,))
        for r in rows:
            r["payload"] = json.loads(r["payload"])
            r["actual"] = json.loads(r["actual"]) if r["actual"] else None
        return rows

    # ---- backup / restore -----------------------------------------------------
    def backup_zip(self) -> bytes:
        """A consistent snapshot of the database plus every original file, as a zip."""
        buf = io.BytesIO()
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "research.db"
            dest = sqlite3.connect(snap)
            with self._lock:
                self.conn.backup(dest)
            dest.close()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(snap, "research.db")
                for f in sorted(self.raw_dir.iterdir()):
                    if f.is_file() and not f.name.endswith(".tmp"):
                        zf.write(f, f"raw/{f.name}")
                zf.writestr("BACKUP.json", json.dumps({"created": now_iso(), "format": "strategy-research/v1"}))
        return buf.getvalue()

    def restore_zip(self, data: bytes) -> dict:
        """Replace the database and raw files with a backup made by backup_zip()."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ValueError("Not a zip file")
        names = zf.namelist()
        if "research.db" not in names or "BACKUP.json" not in names:
            raise ValueError("This zip is not a research backup (research.db / BACKUP.json missing)")
        for n in names:
            if n not in ("research.db", "BACKUP.json") and not (n.startswith("raw/") and "/" not in n[4:]
                                                                 and ".." not in n and n[4:]):
                raise ValueError(f"Unexpected file in backup: {n}")
        with tempfile.TemporaryDirectory() as tmp:
            zf.extractall(tmp)
            check = sqlite3.connect(Path(tmp) / "research.db")
            try:
                ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            except sqlite3.DatabaseError:
                ok, tables = False, set()
            finally:
                check.close()
            if not ok or not {"imports", "market_bars", "strategy_days"} <= tables:
                raise ValueError("The database in this backup is damaged or from a different app")
            with self._lock:
                self.conn.close()
                for suffix in ("", "-wal", "-shm"):
                    Path(str(self.path) + suffix).unlink(missing_ok=True)
                shutil.copy2(Path(tmp) / "research.db", self.path)
                shutil.rmtree(self.raw_dir, ignore_errors=True)
                self.raw_dir.mkdir(parents=True)
                src = Path(tmp) / "raw"
                if src.exists():
                    for f in src.iterdir():
                        shutil.copy2(f, self.raw_dir / f.name)
                self._open()
        return {"restored": True, "raw_files": len(list(self.raw_dir.iterdir()))}
