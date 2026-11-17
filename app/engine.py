"""MarketEngine: ties feed -> normaliser -> filter -> aggregator -> storage -> browser broadcast."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Optional

from app.config import Settings
from app.flow.engine import FlowEngine
from app.flow.mock import MockDerivGenerator
from app.indicators import build_indicators
from app.market_data.aggregator import CandleAggregator
from app.market_data.mock_feed import MockTickGenerator
from app.market_data.normalizer import MalformedTick, normalize_tick
from app.market_data.tickfilter import STALE, TickFilter
from app.models import Candle, Tick
from app.sessions import in_cash_session
from app.storage.repository import SRC_HIST, SRC_LIVE, SRC_MOCK, Repository
from app.timeframes import TIMEFRAME_KEYS, TIMEFRAMES

log = logging.getLogger(__name__)
SERIES_CAP = 6000


class MarketEngine:
    def __init__(self, settings: Settings, repo: Repository):
        self.s = settings
        self.repo = repo
        self.inst = settings.instrument
        self.agg = CandleAggregator()
        self.filter = TickFilter(stale_max_age_s=settings.stale_tick_max_age_s)
        self.indicators = build_indicators(settings.enabled_indicators)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._tick_q: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._clients: set[asyncio.Queue] = set()
        self._series: dict[str, list[Candle]] = {}
        self._dirty: dict[str, Candle] = {}
        self._tick_rows: list[tuple] = []
        self._tasks: list[asyncio.Task] = []
        self.feed: Any = None
        # status
        self.feed_state = "mock" if settings.is_mock else "connecting"
        self.feed_detail = "Synthetic DEMO data" if settings.is_mock else ""
        self.last_error = ""
        self.instrument_verified: Optional[bool] = None if not settings.is_mock else False
        self.instrument_note = "Mock mode: no broker verification" if settings.is_mock else "pending"
        self.history_status: dict[str, dict] = {}
        self.tick_count = 0
        self.dropped_malformed = 0
        self.last_tick: Optional[Tick] = None
        self.last_accepted_recv = 0.0
        self.ws_up_since = time.time()
        self._latencies: deque = deque(maxlen=50)
        self.prev_close: Optional[float] = None
        self._last_state_sent = ""
        self.started_at = time.time()
        self.mock_gen: Optional[MockTickGenerator] = None
        self.mock_deriv: Optional[MockDerivGenerator] = None
        self.flow: Optional[FlowEngine] = (FlowEngine(settings, settings.flow, repo, self._broadcast, self._flow_subscribe)
                                           if settings.flow.enabled else None)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._restore_from_db()
        self._tasks = [asyncio.create_task(self._consume()), asyncio.create_task(self._flush_loop()),
                       asyncio.create_task(self._watchdog())]
        if self.s.is_mock:
            self._tasks.append(asyncio.create_task(self._mock_loop()))
        else:
            from app.broker.angel_client import AngelFeed
            self.feed = AngelFeed(self.s, self._thread_tick, self._thread_state,
                                  self._thread_history, self._thread_instrument,
                                  on_master=self._thread_master, on_greeks=self._thread_greeks,
                                  get_option_expiry=lambda: self.flow.option_expiry if self.flow else None)
            self.feed.start()

    async def stop(self) -> None:
        if self.feed:
            await asyncio.to_thread(self.feed.stop)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._flush()
        self.repo.close()

    def _restore_from_db(self) -> None:
        src = SRC_MOCK if self.s.is_mock else SRC_LIVE
        for tf in TIMEFRAME_KEYS:
            last = self.repo.last_candle(self.inst.token, tf)
            if last is not None and not self.s.is_mock:
                self.agg.seed(tf, last)     # resume forming candle after a restart

    # ------------------------------------------------------------------ thread-safe entry points
    def _thread_tick(self, raw: dict, recv_ts: float) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._enqueue, raw, recv_ts)

    def _enqueue(self, raw: dict, recv_ts: float) -> None:
        try:
            self._tick_q.put_nowait((raw, recv_ts))
        except asyncio.QueueFull:
            self.dropped_malformed += 1

    def _thread_state(self, state: str, detail: str) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._set_feed_state, state, detail)

    def _thread_history(self, tf: str, candles: list, status: dict) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._apply_history, tf, candles, status)

    def _thread_master(self, rows: list) -> None:
        if self.loop and self.flow:
            self.loop.call_soon_threadsafe(self.flow.set_master, rows)

    def _thread_greeks(self, expiry: str, rows: list) -> None:
        if self.loop and self.flow:
            self.loop.call_soon_threadsafe(self.flow.apply_greeks, expiry, rows)

    def _flow_subscribe(self, token_list: list) -> None:
        """FlowEngine asks for NFO tokens: live -> extra WebSocket subscription; mock -> the generator."""
        if self.s.is_mock:
            if self.mock_deriv:
                for tl in token_list:
                    self.mock_deriv.subscribe(tl["tokens"])
        elif self.feed:
            self.feed.add_subscriptions(token_list)

    def _thread_instrument(self, ok: bool, note: str) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._set_instrument, ok, note)

    def _set_instrument(self, ok: bool, note: str) -> None:
        self.instrument_verified, self.instrument_note = ok, note

    def _set_feed_state(self, state: str, detail: str) -> None:
        self.feed_state, self.feed_detail = state, detail
        if self.flow and state in ("connecting", "reconnecting", "disconnected", "error"):
            self.flow.on_feed_interrupted()
        if state == "subscribed":
            self.ws_up_since = time.time()
        if state in ("auth_failed", "error", "reconnecting"):
            self.last_error = detail
        self._push_status(force=True)

    # ------------------------------------------------------------------ history
    def _apply_history(self, tf: str, candles: list[Candle], status: dict) -> None:
        self.history_status[tf] = status
        if candles:
            self.repo.upsert_candles(self.inst.token, tf, candles, SRC_HIST)
            self._series.pop(tf, None)           # force reload from DB
            self._broadcast({"type": "resync", "timeframe": tf})
        else:
            self._broadcast({"type": "history_status", "timeframe": tf, "status": status})

    # ------------------------------------------------------------------ tick pipeline
    async def _consume(self) -> None:
        while True:
            raw, recv_ts = await self._tick_q.get()
            if self.flow and str(raw.get("token", "")).strip() != self.inst.token:
                try:
                    self.flow.on_raw(raw, recv_ts, in_session=self.s.is_mock or not self.s.candle_session_only
                                     or in_cash_session(float(raw.get("exchange_timestamp", 0)) / 1000.0 or recv_ts))
                except Exception:
                    log.exception("flow tick failed")
                continue
            self.process_raw(raw, recv_ts)

    def process_raw(self, raw: dict, recv_ts: float, *, broadcast: bool = True) -> Optional[Tick]:
        try:
            tick = normalize_tick(raw, recv_ts, expected_token=self.inst.token,
                                  expected_exchange_type=self.inst.exchange_type)
        except MalformedTick as e:
            self.dropped_malformed += 1
            log.debug("malformed tick dropped: %s", e)
            return None
        verdict = self.filter.check(tick)
        if verdict not in (None, STALE):
            return None
        self.tick_count += 1
        self.last_tick = tick
        self.last_accepted_recv = recv_ts
        if tick.prev_close:
            self.prev_close = tick.prev_close
        if tick.latency_ms is not None and not self.s.is_mock:
            self._latencies.append(tick.latency_ms)
        if self.s.store_ticks and verdict is None and broadcast:
            self._tick_rows.append((tick.token, int(tick.exchange_ts * 1000), int(recv_ts * 1000),
                                    tick.price, tick.day_volume, tick.sequence))
        aggregate = verdict is None and (self.s.is_mock or not self.s.candle_session_only
                                         or in_cash_session(tick.exchange_ts))
        if aggregate:
            for upd in self.agg.add_tick(tick):
                self._on_candle_update(upd, broadcast)
            if self.flow:
                self.flow.on_spot(tick)
        if broadcast:
            self._broadcast(self._quote_msg(tick, stale=verdict == STALE))
        return tick

    def _on_candle_update(self, upd, broadcast: bool) -> None:
        tf, c = upd.timeframe, upd.candle
        series = self._series.get(tf)
        if series is None and broadcast:
            series = self._load_series(tf)
        if series is not None:
            if series and series[-1].time == c.time:
                series[-1] = Candle(**c.__dict__)
            elif not series or series[-1].time < c.time:
                series.append(Candle(**c.__dict__))
                if len(series) > SERIES_CAP:
                    del series[: len(series) - SERIES_CAP]
        self._dirty[(tf, c.time)] = Candle(**c.__dict__)      # every bucket persisted, incl. closed ones
        if broadcast:
            self._broadcast({"type": "candle", "timeframe": tf, "is_new": upd.is_new, "candle": c.to_wire()})
            for ind in self.indicators:
                tail = (series or [])[-max(ind.lookback, 1):]
                pts = ind.compute(tail)
                if pts:
                    self._broadcast({"type": "indicator", "timeframe": tf, "name": ind.key, "point": pts[-1]})

    def _quote_msg(self, t: Tick, stale: bool) -> dict:
        chg = pct = None
        if self.prev_close:
            chg = round(t.price - self.prev_close, 2)
            pct = round(chg / self.prev_close * 100, 3)
        return {"type": "quote", "price": t.price, "change": chg, "change_pct": pct,
                "prev_close": self.prev_close, "exchange_ts": t.exchange_ts, "recv_ts": t.recv_ts,
                "latency_ms": None if t.latency_ms is None or self.s.is_mock else round(t.latency_ms, 1),
                "ts_source": t.ts_source, "stale_snapshot": stale, "tick_count": self.tick_count}

    # ------------------------------------------------------------------ persistence
    def _flush(self) -> None:
        src = SRC_MOCK if self.s.is_mock else SRC_LIVE
        dirty, self._dirty = self._dirty, {}
        by_tf: dict[str, list[Candle]] = {}
        for (tf, _t), c in dirty.items():
            by_tf.setdefault(tf, []).append(c)
        for tf, cs in by_tf.items():
            self.repo.upsert_candles(self.inst.token, tf, cs, src)
        if self._tick_rows:
            rows, self._tick_rows = self._tick_rows, []
            self.repo.insert_ticks(rows)
        if self.flow:
            self.flow.flush_store()

    async def _flush_loop(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(1.0)
            try:
                await asyncio.to_thread(self._flush)
                n += 1
                if n % 3600 == 0 and self.s.store_ticks:
                    await asyncio.to_thread(self.repo.prune_ticks, self.s.tick_retention_days)
            except Exception:
                log.exception("flush failed")

    # ------------------------------------------------------------------ mock
    async def _mock_loop(self) -> None:
        self.mock_gen = gen = MockTickGenerator(self.inst.token, self.inst.exchange_type,
                                                seed=self.s.mock_seed, with_volume=self.s.mock_with_volume)
        now = time.time()
        if self.flow:
            self.mock_deriv = MockDerivGenerator(gen, seed=self.s.mock_seed)
            self.flow.set_master(self.mock_deriv.master_rows())
        n = 0
        for raw in gen.backfill(now, self.s.mock_backfill_minutes):
            ts = raw["exchange_timestamp"] / 1000.0
            self.process_raw(raw, ts + 0.05, broadcast=False)
            n += 1
            if self.flow and self.mock_deriv and n % 2 == 0:
                for d in self.mock_deriv.step(ts, gen.price, 2.0):
                    self.flow.on_raw(d, ts + 0.05)
        self._flush()
        self.filter.reset()
        self.ws_up_since = time.time()
        for tf in TIMEFRAME_KEYS:
            self.history_status[tf] = {"state": "unsupported", "count": 0,
                                       "message": "MOCK mode: synthetic data only, no broker history."}
        while True:
            t = time.time()
            self.process_raw(gen.next_raw(t), t + 0.02)
            if self.flow and self.mock_deriv:
                for d in self.mock_deriv.step(t, gen.price, self.s.mock_tick_interval_s):
                    self.flow.on_raw(d, t + 0.02)
            await asyncio.sleep(self.s.mock_tick_interval_s)

    # ------------------------------------------------------------------ status
    def derived_state(self, now: Optional[float] = None) -> tuple[str, str]:
        now = now or time.time()
        if self.s.is_mock:
            return "mock", "Synthetic DEMO data"
        st = self.feed_state
        if st in ("auth_failed", "error", "connecting", "reconnecting", "disconnected"):
            return st, self.feed_detail
        if st == "subscribed":
            ref = self.last_accepted_recv if self.last_accepted_recv >= self.ws_up_since else None
            age = now - (ref if ref else self.ws_up_since)
            if ref and age <= self.s.stale_after_s:
                return "connected", "Receiving ticks"
            if in_cash_session(now):
                return "stale", f"No tick for {age:.0f}s during market hours"
            return "market_closed", "Market closed / no ticks"
        return st, self.feed_detail

    def status(self) -> dict:
        now = time.time()
        state, detail = self.derived_state(now)
        lt = self.last_tick
        lat = list(self._latencies)
        return {
            "state": state, "detail": detail, "mode": self.s.mode, "mock": self.s.is_mock,
            "instrument": self.inst.public(), "instrument_verified": self.instrument_verified,
            "instrument_note": self.instrument_note,
            "market_session_open": in_cash_session(now),
            "last_tick_age_s": round(now - self.last_accepted_recv, 2) if self.last_accepted_recv else None,
            "last_exchange_ts": lt.exchange_ts if lt else None, "last_recv_ts": lt.recv_ts if lt else None,
            "latency_ms_last": round(lat[-1], 1) if lat else None,
            "latency_ms_avg": round(sum(lat) / len(lat), 1) if lat else None,
            "tick_count": self.tick_count,
            "dropped": {**self.filter.counts, "malformed": self.dropped_malformed,
                        "late_for_candle": self.agg.late_ticks_ignored},
            "last_error": self.last_error, "reconnect_attempt": getattr(self.feed, "reconnect_attempt", 0),
            "volume_available": self.agg.volume_seen,
            "history": self.history_status, "clients": len(self._clients),
            "stale_after_s": self.s.stale_after_s, "server_time": now,
        }

    def _push_status(self, force: bool = False) -> None:
        msg = {"type": "status", **self.status()}
        key = f"{msg['state']}|{msg['detail']}"
        if force or key != self._last_state_sent:
            self._last_state_sent = key
        self._broadcast(msg)

    async def _watchdog(self) -> None:
        stale_since: Optional[float] = None
        while True:
            await asyncio.sleep(1.0)
            state, _ = self.derived_state()
            self._push_status()
            if self.flow:
                try:
                    self.flow.flush(time.time() if not self.s.is_mock else max(self.flow.clock, time.time()))
                except Exception:
                    log.exception("flow flush failed")
            if state == "stale" and self.feed:
                stale_since = stale_since or time.time()
                if time.time() - stale_since > self.s.reconnect_after_stale_s:
                    log.warning("Feed stale for %.0fs - forcing reconnect", time.time() - stale_since)
                    stale_since = None
                    self.feed.request_reconnect()
            else:
                stale_since = None

    # ------------------------------------------------------------------ clients
    def _load_series(self, tf: str) -> list[Candle]:
        if tf not in self._series:
            self._series[tf] = self.repo.fetch_candles(self.inst.token, tf, self.s.snapshot_limit)
            cur = self.agg.current(tf)
            ser = self._series[tf]
            if cur is not None:
                if ser and ser[-1].time == cur.time:
                    ser[-1] = Candle(**cur.__dict__)
                elif not ser or ser[-1].time < cur.time:
                    ser.append(Candle(**cur.__dict__))
        return self._series[tf]

    def snapshot(self, tf: str, limit: Optional[int] = None) -> dict:
        series = self._load_series(tf)
        limit = limit or self.s.snapshot_limit
        candles = series[-limit:]
        has_vol = any(c.volume is not None for c in candles)
        return {
            "type": "snapshot", "timeframe": tf, "instrument": self.inst.public(),
            "candles": [c.to_wire() for c in candles],
            "volume_available": has_vol,
            "volume_note": ("Real traded volume from the feed's cumulative day volume." + (" (MOCK volume)" if self.s.is_mock else ""))
            if has_vol else "Volume unavailable: the index feed provides no traded volume. Tick count is not volume.",
            "history_status": self.history_status.get(tf, {"state": "pending", "count": 0, "message": ""}),
            "indicators": {i.key: i.compute(series[-(limit):]) for i in self.indicators},
            "mock": self.s.is_mock,
            "flow_signals": self.flow.snapshot_signals(candles[0].time if candles else None) if self.flow else [],
            "flow_enabled": self.flow is not None,
            "flow_state": {tf: self.flow._brief_state(tf) for tf in self.flow.fs.timeframes if tf in self.flow.latest} if self.flow else {},
        }

    def add_client(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._clients.add(q)
        return q

    def remove_client(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    def _broadcast(self, msg: dict) -> None:
        for q in list(self._clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "resync", "timeframe": "*"})
