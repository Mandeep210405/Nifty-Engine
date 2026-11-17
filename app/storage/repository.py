"""SQLite persistence for candles and (optionally) raw ticks. Thread-safe via a lock."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from app.models import Candle

SRC_LIVE = "live"
SRC_HIST = "hist"
SRC_MOCK = "mock"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    token TEXT NOT NULL, timeframe TEXT NOT NULL, ts INTEGER NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL, tick_count INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL,
    PRIMARY KEY (token, timeframe, ts)
);
CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL,
    exchange_ts_ms INTEGER NOT NULL, recv_ts_ms INTEGER NOT NULL,
    price REAL NOT NULL, day_volume INTEGER, seq INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks (recv_ts_ms);
"""


class Repository:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- generic helpers (used by the flow layer's own tables) ---------------------------
    def exec_script(self, script: str) -> None:
        with self._lock:
            self._db.executescript(script)
            self._db.commit()

    def exec_many(self, sql: str, rows: list) -> None:
        if not rows:
            return
        with self._lock:
            self._db.executemany(sql, rows)
            self._db.commit()

    def query(self, sql: str, args: tuple | list = ()) -> list:
        with self._lock:
            return self._db.execute(sql, list(args)).fetchall()

    # -- candles -----------------------------------------------------------------------
    def upsert_candles(self, token: str, timeframe: str, candles: Iterable[Candle], source: str) -> int:
        """Live/mock writes never overwrite broker ('hist') rows; hist writes overwrite anything."""
        rows = [(token, timeframe, c.time, c.open, c.high, c.low, c.close, c.volume, c.tick_count, source)
                for c in candles]
        if not rows:
            return 0
        guard = "" if source == SRC_HIST else " WHERE candles.source != 'hist'"
        sql = ("INSERT INTO candles (token,timeframe,ts,open,high,low,close,volume,tick_count,source) "
               "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(token,timeframe,ts) DO UPDATE SET "
               "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
               "volume=excluded.volume, tick_count=excluded.tick_count, source=excluded.source" + guard)
        with self._lock:
            self._db.executemany(sql, rows)
            self._db.commit()
        return len(rows)

    @staticmethod
    def _row_to_candle(r: sqlite3.Row) -> Candle:
        return Candle(time=r["ts"], open=r["open"], high=r["high"], low=r["low"], close=r["close"],
                      volume=r["volume"], tick_count=r["tick_count"])

    def fetch_candles(self, token: str, timeframe: str, limit: int = 1500,
                      before: Optional[int] = None) -> list[Candle]:
        """Most recent ``limit`` candles, returned ascending and unique by time."""
        q = "SELECT * FROM candles WHERE token=? AND timeframe=?"
        args: list = [token, timeframe]
        if before is not None:
            q += " AND ts < ?"
            args.append(before)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [self._row_to_candle(r) for r in reversed(rows)]

    def last_candle(self, token: str, timeframe: str) -> Optional[Candle]:
        c = self.fetch_candles(token, timeframe, 1)
        return c[0] if c else None

    def count_candles(self, token: str, timeframe: str) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM candles WHERE token=? AND timeframe=?",
                                    (token, timeframe)).fetchone()[0]

    # -- ticks -------------------------------------------------------------------------
    def insert_ticks(self, rows: list[tuple]) -> None:
        """rows: (token, exchange_ts_ms, recv_ts_ms, price, day_volume, seq)"""
        if not rows:
            return
        with self._lock:
            self._db.executemany(
                "INSERT INTO ticks (token,exchange_ts_ms,recv_ts_ms,price,day_volume,seq) VALUES (?,?,?,?,?,?)", rows)
            self._db.commit()

    def prune_ticks(self, retention_days: int) -> int:
        cutoff = int((time.time() - retention_days * 86400) * 1000)
        with self._lock:
            cur = self._db.execute("DELETE FROM ticks WHERE recv_ts_ms < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount

    def count_ticks(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
