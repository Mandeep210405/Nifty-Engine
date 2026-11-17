"""Tick quality gate: duplicates, out-of-order, future-dated and stale ticks."""
from __future__ import annotations

from typing import Optional

from app.models import Tick

DUPLICATE = "duplicate"
OUT_OF_ORDER = "out_of_order"
FUTURE = "future_timestamp"
STALE = "stale"          # accepted for the quote display, NOT aggregated into candles


class TickFilter:
    def __init__(self, max_future_s: float = 5.0, stale_max_age_s: float = 120.0):
        self.max_future_s = max_future_s
        self.stale_max_age_s = stale_max_age_s
        self._last_key: Optional[tuple] = None
        self._last_ts: float = 0.0
        self.counts: dict[str, int] = {DUPLICATE: 0, OUT_OF_ORDER: 0, FUTURE: 0, STALE: 0}

    def reset(self) -> None:
        self._last_key, self._last_ts = None, 0.0

    def check(self, tick: Tick) -> Optional[str]:
        """Return None to accept fully, STALE for quote-only, or a drop reason."""
        if tick.ts_source == "exchange":
            if tick.exchange_ts - tick.recv_ts > self.max_future_s:
                return self._count(FUTURE)
        key = (tick.sequence, round(tick.exchange_ts, 3), tick.price, tick.day_volume)
        if key == self._last_key:
            return self._count(DUPLICATE)
        if tick.exchange_ts < self._last_ts:
            return self._count(OUT_OF_ORDER)
        self._last_key, self._last_ts = key, tick.exchange_ts
        if tick.ts_source == "exchange" and tick.recv_ts - tick.exchange_ts > self.stale_max_age_s:
            self.counts[STALE] += 1
            return STALE
        return None

    def _count(self, reason: str) -> str:
        self.counts[reason] += 1
        return reason
