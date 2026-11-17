"""SQLite persistence for flow signals, outcomes and compact per-bar snapshots (own tables, same database)."""
from __future__ import annotations

import json
import threading
from typing import Iterable, Optional

from app.flow.models import Contract, FlowSignal, InstBar
from app.storage.repository import Repository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_signals (
    id TEXT PRIMARY KEY, mode TEXT NOT NULL, ts INTEGER NOT NULL, tf TEXT NOT NULL, tf_seconds INTEGER NOT NULL,
    type TEXT NOT NULL, instrument TEXT, token TEXT, strike REAL, expiry TEXT, direction INTEGER NOT NULL,
    strength REAL NOT NULL, quality TEXT, entry_ref REAL, regime TEXT, created_at REAL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flow_sig_ts ON flow_signals (ts);
CREATE TABLE IF NOT EXISTS flow_outcomes (
    signal_id TEXT NOT NULL, horizon INTEGER NOT NULL, ref_price REAL NOT NULL, exit_price REAL NOT NULL,
    gross_bps REAL NOT NULL, net_bps REAL NOT NULL, mfe_bps REAL NOT NULL, mae_bps REAL NOT NULL,
    PRIMARY KEY (signal_id, horizon)
);
CREATE TABLE IF NOT EXISTS flow_bars (
    tf TEXT NOT NULL, token TEXT NOT NULL, ts INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (tf, token, ts)
);
CREATE TABLE IF NOT EXISTS flow_spot_bars (
    tf TEXT NOT NULL, ts INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, PRIMARY KEY (tf, ts)
);
CREATE TABLE IF NOT EXISTS flow_contracts (token TEXT PRIMARY KEY, payload TEXT NOT NULL);
"""


class FlowStore:
    def __init__(self, repo: Repository):
        self.repo = repo
        repo.exec_script(_SCHEMA)
        self._sig_rows: list[tuple] = []
        self._out_rows: list[tuple] = []
        self._bar_rows: list[tuple] = []
        self._spot_rows: list[tuple] = []
        self._con_rows: list[tuple] = []
        self._qlock = threading.Lock()

    # -- queueing (cheap, called from the event loop) ---------------------------------------------------
    def queue_signal(self, g: FlowSignal) -> None:
        with self._qlock:
            self._sig_rows.append((g.id, g.mode, g.time, g.tf, g.tf_seconds, g.type, g.instrument, g.token, g.strike, g.expiry,
                               g.direction, g.strength, g.data_quality, g.underlying_price, g.regime, g.created_at,
                               json.dumps(g.to_dict(), separators=(",", ":"))))

    def queue_outcome(self, sid: str, o: dict) -> None:
        with self._qlock:
            self._out_rows.append((sid, o["horizon"], o["ref_price"], o["exit_price"], o["gross_bps"], o["net_bps"],
                               o["mfe_bps"], o["mae_bps"]))

    def queue_bars(self, tf: str, bars: Iterable[InstBar]) -> None:
        rows = [(tf, b.token, b.time, json.dumps(b.to_dict(), separators=(",", ":"))) for b in bars]
        with self._qlock:
            self._bar_rows.extend(rows)

    def queue_spot(self, tf: str, sb) -> None:
        with self._qlock:
            self._spot_rows.append((tf, sb.time, sb.open, sb.high, sb.low, sb.close))

    def queue_contract(self, c: Contract) -> None:
        with self._qlock:
            self._con_rows.append((c.token, json.dumps(c.__dict__)))

    # -- flush (called from a worker thread) -------------------------------------------------------------
    def flush(self) -> None:
        with self._qlock:
            sig, self._sig_rows = self._sig_rows, []
            out, self._out_rows = self._out_rows, []
            bar, self._bar_rows = self._bar_rows, []
            spot, self._spot_rows = self._spot_rows, []
            con, self._con_rows = self._con_rows, []
        r = self.repo
        r.exec_many("INSERT OR IGNORE INTO flow_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", sig)
        r.exec_many("INSERT OR REPLACE INTO flow_outcomes VALUES (?,?,?,?,?,?,?,?)", out)
        r.exec_many("INSERT OR REPLACE INTO flow_bars VALUES (?,?,?,?)", bar)
        r.exec_many("INSERT OR REPLACE INTO flow_spot_bars VALUES (?,?,?,?,?,?)", spot)
        r.exec_many("INSERT OR REPLACE INTO flow_contracts VALUES (?,?)", con)

    # -- reads ----------------------------------------------------------------------------------------
    def signals(self, since: Optional[int] = None, tf: Optional[str] = None, limit: int = 1500) -> list[dict]:
        q, a = "SELECT payload FROM flow_signals WHERE 1=1", []
        if since is not None:
            q += " AND ts >= ?"; a.append(since)
        if tf:
            q += " AND tf = ?"; a.append(tf)
        q += " ORDER BY ts DESC, id LIMIT ?"; a.append(limit)
        rows = self.repo.query(q, a)
        return [json.loads(r["payload"]) for r in reversed(rows)]

    def signal_outcome_rows(self, mode: Optional[str] = None) -> list[dict]:
        """Signals joined with outcomes (one row per signal x horizon)."""
        q = ("SELECT s.id, s.mode, s.ts, s.tf, s.tf_seconds, s.type, s.instrument, s.strike, s.direction, s.strength, s.quality, "
             "s.regime, o.horizon, o.gross_bps, o.net_bps, o.mfe_bps, o.mae_bps FROM flow_signals s "
             "JOIN flow_outcomes o ON o.signal_id = s.id" + (" WHERE s.mode = ?" if mode else "") + " ORDER BY s.ts")
        return [dict(r) for r in self.repo.query(q, [mode] if mode else [])]

    def signal_counts(self, mode: Optional[str] = None) -> list[dict]:
        q = "SELECT type, tf, COUNT(*) AS n, COUNT(DISTINCT (ts + 19800) / 86400) AS days FROM flow_signals" + \
            (" WHERE mode = ?" if mode else "") + " GROUP BY type, tf"
        return [dict(r) for r in self.repo.query(q, [mode] if mode else [])]

    def load_recent_bars(self, tf: str, limit_per_token: int = 40) -> list[InstBar]:
        rows = self.repo.query("SELECT payload FROM flow_bars WHERE tf=? ORDER BY ts DESC LIMIT ?", (tf, limit_per_token * 40))
        bars = [InstBar.from_dict(json.loads(r["payload"])) for r in reversed(rows)]
        return bars

    def load_contracts(self) -> dict[str, Contract]:
        out = {}
        for r in self.repo.query("SELECT payload FROM flow_contracts"):
            d = json.loads(r["payload"])
            out[d["token"]] = Contract(**d)
        return out

    def load_spot(self, tf: str) -> list[tuple]:
        return [tuple(r) for r in self.repo.query("SELECT ts,o,h,l,c FROM flow_spot_bars WHERE tf=? ORDER BY ts", (tf,))]

    def all_bars(self, tf: str) -> list[InstBar]:
        return [InstBar.from_dict(json.loads(r["payload"])) for r in
                self.repo.query("SELECT payload FROM flow_bars WHERE tf=? ORDER BY ts, token", (tf,))]
