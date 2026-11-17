"""FlowEngine: routes derivative ticks, builds bars, runs the analyzer on closed bars, persists and broadcasts.

Runs inside the MarketEngine's asyncio loop. Per-tick work is O(timeframes); analysis happens once per closed bar.
Derivative ticks reuse the existing WebSocket (one extra SNAP_QUOTE subscription of ~24 tokens, far below
Angel's per-session token limit) - no REST polling is needed for the core signals.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Callable, Optional

from app.config import Settings
from app.flow.analyzer import FlowAnalyzer
from app.flow.bars import BarBook, Series
from app.flow.config import FlowSettings
from app.flow.contracts import filter_master, option_window, pick_futures, pick_option_expiry, to_contracts
from app.flow.models import KIND_CE, KIND_FUT, KIND_PE, Contract, DerivTick, FlowSignal
from app.flow.normalizer import normalize_deriv_tick
from app.flow.store import FlowStore
from app.flow.tradeclass import TradeClassifier
from app.flow.validation import OutcomeTracker
from app.market_data.normalizer import MalformedTick
from app.models import Tick
from app.storage.repository import Repository
from app.timeframes import TIMEFRAMES

log = logging.getLogger(__name__)
SIG_BRIEF = ("id", "time", "tf", "tf_seconds", "type", "direction", "label", "strength", "strength_label", "data_quality",
             "headline", "instrument", "strike", "expiry", "premium", "token")


def brief(sig: dict) -> dict:
    return {k: sig.get(k) for k in SIG_BRIEF}


class _InstState:
    def __init__(self, con: Contract):
        self.con = con
        self.classifier = TradeClassifier()
        self.last: Optional[DerivTick] = None
        self.last_key: Optional[tuple] = None
        self.last_oi: Optional[float] = None
        self.oi_pending: list[float] = []
        self.day = 0
        self.ticks = 0
        self.last_recv = 0.0


class FlowEngine:
    def __init__(self, settings: Settings, fs: FlowSettings, repo: Repository,
                 broadcast: Callable[[dict], None], subscribe: Callable[[list[dict]], None]):
        self.s, self.fs = settings, fs
        self.mode = "mock" if settings.is_mock else "live"
        self.store = FlowStore(repo)
        self.repo = repo
        self._broadcast, self._subscribe = broadcast, subscribe
        self.analyzer = FlowAnalyzer(fs, self.mode)
        self.tracker = OutcomeTracker(fs)
        self.book = BarBook(fs.timeframes, fs.flush_grace_s)
        self.contracts_all: list[Contract] = []
        self.by_token: dict[str, Contract] = {}
        self.selected: dict[str, Contract] = {}
        self.active_options: set[str] = set()
        self.fut: Optional[Contract] = None
        self.option_expiry: Optional[str] = None
        self.atm: Optional[float] = None
        self.subscribed: set[str] = set()
        self.cap_note = ""
        self.states: dict[str, _InstState] = {}
        self.spot: Optional[float] = None
        self.spot_ts = 0.0
        self.clock = 0.0                          # data-time of the newest tick (equals wall time live)
        self.signals: deque = deque(maxlen=4000)
        self.observed_greeks: dict[str, tuple] = {}
        self.greeks_status = "not polled"
        self.greeks_ok = 0
        self.master_status = "waiting for instrument master"
        self._last_check_bucket = 0
        self._prev_day_cache: tuple[int, Optional[dict]] = (-1, None)
        self.counters = {"deriv_ticks": 0, "malformed": 0, "unknown_token": 0, "duplicate": 0, "out_of_order": 0,
                         "future_ts": 0, "stale": 0, "crossed_depth": 0, "outside_session": 0, "oi_invalid": 0,
                         "signals": 0, "dedup_blocked": 0}
        self.latest: dict[str, dict] = {}
        self.sampling: dict[str, int] = {}
        self._restore()

    # ------------------------------------------------------------------ restore (live only)
    def _restore(self) -> None:
        if self.s.is_mock:
            return
        try:
            now = time.time()
            for d in self.store.signals(since=int(now - 2 * 86400), limit=self.fs.max_signal_snapshot):
                self.signals.append(d)
                self.analyzer.seen.add(d["id"])
            self.by_token.update(self.store.load_contracts())
        except Exception:
            log.exception("flow restore failed (continuing empty)")

    # ------------------------------------------------------------------ master / selection
    def set_master(self, rows: list[dict]) -> None:
        """rows: instrument-master entries already filtered to NIFTY NFO futures/options."""
        cons = to_contracts(rows)
        self.contracts_all = cons
        self.by_token.update({c.token: c for c in cons})
        self.master_status = f"{len(cons)} NIFTY derivative contracts loaded"
        log.info("Flow: %s", self.master_status)
        if self.spot:
            self._reselect(force=True)

    def _step(self, expiry: str) -> float:
        ks = sorted({c.strike for c in self.contracts_all if c.expiry == expiry and c.strike})
        gaps = [b - a for a, b in zip(ks, ks[1:]) if b > a]
        return min(gaps) if gaps else 50.0

    def _reselect(self, force: bool = False) -> None:
        if not self.contracts_all or not self.spot:
            return
        now = self.clock or time.time()
        fut = pick_futures(self.contracts_all, now, self.fs.futures_expiry)
        exp = pick_option_expiry(self.contracts_all, now, self.fs.option_expiry)
        if fut is None or exp is None:
            self.master_status = "no usable NIFTY futures/option expiry in the master (check FLOW_*_EXPIRY overrides)"
            return
        changed = force or fut != self.fut or exp != self.option_expiry
        if not changed and self.atm is not None:
            if abs(self.spot - self.atm) < self.fs.recenter_strikes * self._step(exp):
                return
        atm, win = option_window(self.contracts_all, exp, self.spot, self.fs.strikes_each_side)
        if atm is None:
            return
        self.fut, self.option_expiry, self.atm = fut, exp, atm
        self.active_options = {c.token for c in win}
        self.selected = {fut.token: fut, **{c.token: c for c in win}}
        for c in self.selected.values():
            self.store.queue_contract(c)
            self.states.setdefault(c.token, _InstState(c))
        new = [t for t in self.selected if t not in self.subscribed]
        room = self.fs.max_subscribed_tokens - len(self.subscribed)
        if len(new) > room:
            self.cap_note = f"subscription cap {self.fs.max_subscribed_tokens} reached; {len(new) - room} contract(s) not added"
            new = new[:max(room, 0)]
        if new:
            self.subscribed.update(new)
            self._subscribe([{"exchangeType": 2, "tokens": list(new)}])
            log.info("Flow: subscribing %d NFO token(s); futures %s, option expiry %s, ATM %.0f", len(new), fut.symbol, exp, atm)

    # ------------------------------------------------------------------ inputs
    def on_spot(self, tick: Tick) -> None:
        self.spot, self.spot_ts = tick.price, tick.exchange_ts
        self.clock = max(self.clock, tick.exchange_ts)
        self.book.add_spot(tick.exchange_ts, tick.price)
        if self.contracts_all and (self.atm is None or abs(tick.price - self.atm) >= self.fs.recenter_strikes * 50):
            self._reselect()
        self._maybe_flush(tick.exchange_ts)

    def on_feed_interrupted(self) -> None:
        """Socket dropped: volume accumulated while blind must not be booked as one giant trade."""
        for st in self.states.values():
            st.classifier.reset_baseline()
            st.last = None

    def on_raw(self, raw: dict, recv_ts: float, *, in_session: bool = True) -> Optional[DerivTick]:
        try:
            tick = normalize_deriv_tick(raw, recv_ts, known_tokens=set(self.states))
        except MalformedTick as e:
            self.counters["unknown_token" if "unknown token" in str(e) else "malformed"] += 1
            return None
        st = self.states[tick.token]
        if tick.ts_source == "exchange" and tick.exchange_ts - recv_ts > 5.0 and not self.s.is_mock:
            self.counters["future_ts"] += 1
            return None
        key = (tick.sequence, round(tick.exchange_ts, 3), tick.ltp, tick.day_volume, tick.oi)
        if key == st.last_key:
            self.counters["duplicate"] += 1
            return None
        if st.last is not None and tick.exchange_ts < st.last.exchange_ts:
            self.counters["out_of_order"] += 1
            return None
        st.last_key = key
        self.counters["deriv_ticks"] += 1
        st.ticks += 1
        st.last_recv = recv_ts
        self.clock = max(self.clock, tick.exchange_ts)
        if tick.depth_state in ("crossed", "empty_side"):
            self.counters["crossed_depth"] += 1
        if not in_session:
            self.counters["outside_session"] += 1
            st.last = tick
            return tick
        if not self.s.is_mock and tick.ts_source == "exchange" and recv_ts - tick.exchange_ts > self.s.stale_tick_max_age_s:
            self.counters["stale"] += 1                          # stale snapshot: never aggregated
            st.last = tick
            return tick
        day = int(tick.exchange_ts + 19800) // 86400
        if st.day and st.day != day:
            st.classifier.reset_session()
            st.last_oi, st.oi_pending = None, []
        st.day = day
        valid_oi, oi_bad = self._validate_oi(st, tick)
        trade = st.classifier.classify(tick)
        st.last = tick
        self.book.add_deriv(tick, st.con.kind, trade, st.classifier.cvd, valid_oi, oi_bad)
        self._maybe_flush(tick.exchange_ts)
        return tick

    def _validate_oi(self, st: _InstState, tick: DerivTick) -> tuple[Optional[float], bool]:
        """OI must be positive and not jump implausibly in one tick (a persistent new level is accepted after 3 agreeing ticks)."""
        if tick.oi is None:
            return None, False
        v = float(tick.oi)
        if v <= 0:
            self.counters["oi_invalid"] += 1
            return None, True
        last = st.last_oi
        if last is None or abs(v - last) / last * 100.0 <= self.fs.max_oi_jump_pct:
            st.last_oi, st.oi_pending = v, []
            return v, False
        st.oi_pending.append(v)
        if len(st.oi_pending) >= 3 and max(st.oi_pending[-3:]) <= min(st.oi_pending[-3:]) * 1.01:
            st.last_oi, st.oi_pending = v, []
            return v, False
        self.counters["oi_invalid"] += 1
        return None, True

    # ------------------------------------------------------------------ bar closing / analysis
    def _maybe_flush(self, ts: float) -> None:
        b = int(ts // 10)
        if b != self._last_check_bucket:
            self._last_check_bucket = b
            self.flush(ts)

    def _prev_day(self, t: int) -> Optional[dict]:
        day = (t + 19800) // 86400
        if self._prev_day_cache[0] == day:
            return self._prev_day_cache[1]
        info = None
        try:
            rows = self.repo.fetch_candles(self.s.instrument.token, "1d", 6)
            day_start = day * 86400 - 19800
            prev = [c for c in rows if c.time < day_start]
            if prev:
                info = {"high": prev[-1].high, "low": prev[-1].low, "time": prev[-1].time}
        except Exception:
            log.exception("prev-day lookup failed")
        self._prev_day_cache = (day, info)
        return info

    def flush(self, now: float) -> list[FlowSignal]:
        new: list[FlowSignal] = []
        for tf in self.fs.timeframes:
            for b in self.book.due(tf, now):
                bars, spot_bar = self.book.pop(tf, b)
                if self.fs.store_bars:
                    self.store.queue_bars(tf, bars.values())
                    if spot_bar is not None:
                        self.store.queue_spot(tf, spot_bar)
                try:
                    sigs = self.analyzer.ingest_closed(tf, b, bars, spot_bar, self.selected_or_known(), set(self.active_options),
                                                       self._prev_day(b), self.observed_greeks, wall_now=now)
                except Exception:
                    log.exception("flow analysis failed for %s bar %s", tf, b)
                    continue
                for o in self.tracker.on_bar_close(tf, self.analyzer.st[tf].spot):
                    self.store.queue_outcome(o[0], o[1])
                for g in sigs:
                    self._publish(g)
                    new.append(g)
                self.latest[tf] = self.analyzer.st[tf].latest
                self.sampling[tf] = b
                self._broadcast({"type": "flow_state", "tf": tf, "state": self._brief_state(tf)})
        return new

    def selected_or_known(self) -> dict[str, Contract]:
        return self.by_token if self.by_token else self.selected

    def _publish(self, g: FlowSignal) -> None:
        d = g.to_dict()
        self.signals.append(d)
        self.store.queue_signal(g)
        self.tracker.add(g)
        self.counters["signals"] += 1
        self._broadcast({"type": "flow_signal", "signal": brief(d)})

    # ------------------------------------------------------------------ outputs for API / UI
    def _brief_state(self, tf: str) -> dict:
        lt = self.latest.get(tf) or {}
        pr = lt.get("pressure") or {}
        return {"time": lt.get("time"), "regime": lt.get("regime"), "basis": lt.get("basis"), "underlying": lt.get("underlying"),
                "buy_score": pr.get("buy_score"), "sell_score": pr.get("sell_score"), "buy_label": pr.get("buy_label"),
                "sell_label": pr.get("sell_label"), "suppressed": pr.get("suppressed"), "reason": pr.get("reason"),
                "available_weight": pr.get("available_weight"), "components": pr.get("components"),
                "positioning": (lt.get("positioning") or {}).get("cls"),
                "options": lt.get("options")}

    def snapshot_signals(self, since: Optional[int] = None) -> list[dict]:
        out = [brief(d) for d in self.signals if since is None or d["time"] >= since]
        return out[-self.fs.max_signal_snapshot:]

    def signal_detail(self, sid: str) -> Optional[dict]:
        for d in reversed(self.signals):
            if d["id"] == sid:
                return d
        rows = self.repo.query("SELECT payload FROM flow_signals WHERE id=?", (sid,))
        if rows:
            import json
            return json.loads(rows[0]["payload"])
        return None

    def apply_greeks(self, expiry: str, rows: list[dict]) -> None:
        """Broker option Greeks (OBSERVED). Rejected wholesale if they disagree wildly with our CALCULATED IV."""
        if expiry != self.option_expiry:
            return
        idx = {(c.strike, c.kind): c.token for c in self.selected.values() if c.kind in (KIND_CE, KIND_PE)}
        now, used, bad, cmp_n = time.time(), 0, 0, 0
        parsed = []
        for r in rows:
            try:
                k = float(r["strikePrice"])
                kind = KIND_CE if str(r["optionType"]).upper().startswith("C") else KIND_PE
                iv, delta = float(r["impliedVolatility"]), r.get("delta")
                delta = float(delta) if delta not in (None, "") else None
            except (KeyError, TypeError, ValueError):
                continue
            tok = idx.get((k, kind))
            if tok:
                parsed.append((tok, iv, delta))
        for tok, iv, _ in parsed:
            ser = self.analyzer.st[self.fs.timeframes[0]].opt.get(tok)
            if ser and len(ser) and ser.items[-1].iv_calc:
                cmp_n += 1
                ratio = iv / ser.items[-1].iv_calc
                if not 0.5 <= ratio <= 2.0:
                    bad += 1
        if cmp_n >= 3 and bad > cmp_n / 2:
            self.greeks_status = "broker IV disagrees with calculated IV (unit/scale?) - observed Greeks ignored"
            return
        for tok, iv, delta in parsed:
            self.observed_greeks[tok] = (iv, delta, now)
            used += 1
        self.greeks_ok += 1
        self.greeks_status = f"broker Greeks OK ({used} contracts, {time.strftime('%H:%M:%S')})"

    def status(self) -> dict:
        now = self.clock or time.time()
        inst = []
        for tok, c in sorted(self.selected.items(), key=lambda kv: (kv[1].kind != KIND_FUT, kv[1].strike or 0, kv[1].kind)):
            st = self.states.get(tok)
            inst.append({**c.public(), "ticks": st.ticks if st else 0,
                         "last_tick_age_s": round(time.time() - st.last_recv, 1) if st and st.last_recv else None,
                         "oi": st.last_oi if st else None})
        caveats = [
            "Trade side (aggressor), CVD and CALL/PUT buy-vs-sell labels are ESTIMATED from snapshot quotes; SmartAPI provides no aggressor flag.",
            "Displayed market depth can be cancelled/modified and is not executed volume.",
            "Signals are evidence summaries, not predictions; weights/thresholds are unvalidated starting points.",
        ]
        if self.s.is_mock:
            caveats.insert(0, "MOCK MODE: every derivative figure is synthetic. Validation statistics from mock data are meaningless.")
        return {
            "enabled": True, "mode": self.mode, "timeframes": list(self.fs.timeframes),
            "futures": self.fut.public() if self.fut else None, "option_expiry": self.option_expiry, "atm_strike": self.atm,
            "strikes_each_side": self.fs.strikes_each_side, "instruments": inst,
            "subscribed_tokens": len(self.subscribed), "subscription_cap": self.fs.max_subscribed_tokens, "cap_note": self.cap_note,
            "master": self.master_status, "greeks": self.greeks_status, "counters": dict(self.counters),
            "late_ticks_dropped": self.book.late_dropped, "suppressed": dict(self.analyzer.suppressed_counts),
            "ts_skew_fut_vs_spot_s": self._skew(), "weights": self.fs.weights, "caveats": caveats,
            "last_bar": {tf: self.sampling.get(tf) for tf in self.fs.timeframes},
        }

    def _skew(self) -> Optional[float]:
        if not self.fut or self.fut.token not in self.states or not self.spot_ts:
            return None
        st = self.states[self.fut.token]
        return round(st.last.exchange_ts - self.spot_ts, 2) if st.last else None

    def flush_store(self) -> None:
        self.store.flush()
