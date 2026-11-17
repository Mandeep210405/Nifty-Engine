from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Tick:
    """A validated, normalised tick. Prices are in rupees, times are UTC unix seconds."""
    token: str
    exchange_type: int
    price: float
    exchange_ts: float            # exchange timestamp (or recv time if ts_source == "local")
    recv_ts: float                # local receive time
    sequence: int = 0
    day_volume: Optional[int] = None      # cumulative traded volume for the day (None if absent)
    last_qty: Optional[int] = None
    prev_close: Optional[float] = None
    day_open: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    mode: int = 1
    ts_source: str = "exchange"           # "exchange" | "local"

    @property
    def latency_ms(self) -> Optional[float]:
        if self.ts_source != "exchange":
            return None
        return (self.recv_ts - self.exchange_ts) * 1000.0


@dataclass
class Candle:
    time: int                     # bucket start, unix seconds UTC
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None  # real traded volume only; None => unavailable
    tick_count: int = 0             # number of ticks aggregated (NOT volume)

    def to_wire(self) -> dict:
        return {
            "time": self.time, "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "volume": self.volume, "tick_count": self.tick_count,
        }


@dataclass
class CandleUpdate:
    timeframe: str
    candle: Candle
    is_new: bool
    closed: Optional[Candle] = None
