"""Trade-side (aggressor) ESTIMATION from snapshot ticks.

SmartAPI does not provide aggressor-side flags, so execution side can never be CONFIRMED here.
This applies a Lee-Ready style rule to the volume traded since the previous tick:

1. quote rule against the PREVIOUS tick's best bid/ask (lagged quote): price >= ask -> buy, <= bid -> sell,
   inside the spread -> nearer side of the mid-point;
2. at the mid-point or with no valid quote -> tick rule (price up = buy, down = sell, unchanged = previous side);
3. no usable information -> counted as *unclassified* (never forced into buy/sell).

Caveat: the feed is a sampled snapshot stream, so several trades can be merged into one delta and
the side of each is unknowable. Output is therefore an ESTIMATE, and bars carry the classified
fraction so downstream logic can discount or suppress it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.flow.models import DerivTick

BUY, SELL, NONE = 1, -1, 0


@dataclass
class TradeSide:
    side: int
    volume: float
    method: str        # "quote" | "tick" | "unclassified"


class TradeClassifier:
    def __init__(self) -> None:
        self._prev: Optional[DerivTick] = None
        self._last_side = NONE
        self._last_price: Optional[float] = None
        self.cvd = 0.0

    def reset_session(self) -> None:
        self.cvd = 0.0
        self._prev = None
        self._last_side, self._last_price = NONE, None

    def reset_baseline(self) -> None:
        """Forget the previous tick (e.g. after a feed outage) without touching CVD."""
        self._prev = None
        self._last_price = None

    def classify(self, tick: DerivTick) -> Optional[TradeSide]:
        """Return the estimated trade for volume added since the previous tick (None if no new volume)."""
        prev = self._prev
        self._prev = tick
        if prev is None or tick.day_volume is None or prev.day_volume is None:
            self._last_price = tick.ltp
            return None
        dv = tick.day_volume - prev.day_volume
        if dv < 0:                       # cumulative counter reset (new session)
            self.cvd = 0.0
            self._last_side, self._last_price = NONE, tick.ltp
            return None
        if dv == 0:
            return None
        p = tick.ltp
        side, method = NONE, "unclassified"
        if prev.depth_state == "ok" and prev.best_bid and prev.best_ask:
            bid, ask = prev.best_bid, prev.best_ask
            if p >= ask:
                side, method = BUY, "quote"
            elif p <= bid:
                side, method = SELL, "quote"
            else:
                mid = (bid + ask) / 2.0
                if p > mid:
                    side, method = BUY, "quote"
                elif p < mid:
                    side, method = SELL, "quote"
        if side == NONE and self._last_price is not None:
            if p > self._last_price:
                side, method = BUY, "tick"
            elif p < self._last_price:
                side, method = SELL, "tick"
            elif self._last_side != NONE:
                side, method = self._last_side, "tick"
        self._last_price = p
        if side != NONE:
            self._last_side = side
            self.cvd += side * dv
        return TradeSide(side, float(dv), method)
