"""Tick -> OHLC candle aggregation for every supported timeframe.

Candles are built ONLY from ticks (or seeded from storage). Sub-minute candles (10s, 30s)
do not exist at the broker; they are time-bucketed groupings of live ticks.
Volume is real traded volume derived from the cumulative day-volume field when the feed
provides a positive value; otherwise it stays ``None`` (unavailable). ``tick_count`` is a
separate field and is never shown as volume.
"""
from __future__ import annotations

from typing import Iterable, Optional

from app.models import Candle, CandleUpdate, Tick
from app.timeframes import TIMEFRAMES, Timeframe, bucket_start


class CandleAggregator:
    def __init__(self, timeframes: Iterable[str] | None = None):
        keys = list(timeframes) if timeframes else list(TIMEFRAMES)
        self._tfs: list[Timeframe] = [TIMEFRAMES[k] for k in keys]
        self._current: dict[str, Candle] = {}
        self._last_day_volume: Optional[int] = None
        self._volume_seen = False
        self.late_ticks_ignored = 0

    # -- state -------------------------------------------------------------------------
    def current(self, tf_key: str) -> Optional[Candle]:
        return self._current.get(tf_key)

    @property
    def volume_seen(self) -> bool:
        return self._volume_seen

    def seed(self, tf_key: str, candle: Candle) -> None:
        """Resume a forming candle restored from storage (only if newer than current)."""
        cur = self._current.get(tf_key)
        if cur is None or candle.time > cur.time:
            self._current[tf_key] = Candle(**candle.__dict__)

    # -- volume ------------------------------------------------------------------------
    def _volume_delta(self, tick: Tick) -> Optional[float]:
        v = tick.day_volume
        if v is None:
            return None
        if v > 0:
            self._volume_seen = True
        last = self._last_day_volume
        self._last_day_volume = v
        if not self._volume_seen:
            return None
        if last is None:
            return 0.0                    # first observation: no baseline, count nothing
        if v >= last:
            return float(v - last)
        return float(v)                   # cumulative counter reset (new session)

    # -- main --------------------------------------------------------------------------
    def add_tick(self, tick: Tick) -> list[CandleUpdate]:
        delta = self._volume_delta(tick)
        p = tick.price
        out: list[CandleUpdate] = []
        for tf in self._tfs:
            b = bucket_start(tick.exchange_ts, tf)
            cur = self._current.get(tf.key)
            vol_part = (delta if delta is not None else 0.0) if self._volume_seen else None
            if cur is None or b > cur.time:
                new = Candle(time=b, open=p, high=p, low=p, close=p,
                             volume=vol_part, tick_count=1)
                self._current[tf.key] = new
                out.append(CandleUpdate(tf.key, new, True, closed=cur))
            elif b == cur.time:
                cur.high = max(cur.high, p)
                cur.low = min(cur.low, p)
                cur.close = p
                cur.tick_count += 1
                if vol_part is not None:
                    cur.volume = (cur.volume or 0.0) + vol_part
                out.append(CandleUpdate(tf.key, cur, False))
            else:
                self.late_ticks_ignored += 1
        return out
