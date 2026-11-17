"""Indicator engine hook.

Add your own indicator by subclassing :class:`Indicator`, registering it in ``REGISTRY`` and
listing its key in ``ENABLED_INDICATORS`` (comma separated) in ``.env``. Indicators are pure
functions of the candle series for a timeframe; they must only describe price data. This
project makes no claim about institutional activity or order flow.

The frontend draws every enabled indicator as a line series from ``snapshot.indicators``
and live ``indicator`` messages.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from app.models import Candle


class Indicator(ABC):
    key: str = ""
    label: str = ""
    lookback: int = 1          # candles needed to compute the newest point

    @abstractmethod
    def compute(self, candles: Sequence[Candle]) -> list[dict]:
        """Return [{'time': int, 'value': float}, ...] ascending by time."""


class SMA(Indicator):
    """Example indicator: simple moving average of closes (period 20)."""
    key = "sma20"
    label = "SMA 20"
    period = 20

    @property
    def lookback(self) -> int:  # type: ignore[override]
        return self.period

    def compute(self, candles: Sequence[Candle]) -> list[dict]:
        out: list[dict] = []
        closes = [c.close for c in candles]
        for i in range(self.period - 1, len(closes)):
            window = closes[i - self.period + 1: i + 1]
            out.append({"time": candles[i].time, "value": round(sum(window) / self.period, 4)})
        return out


REGISTRY: dict[str, type[Indicator]] = {SMA.key: SMA}


def available_indicators() -> list[str]:
    return sorted(REGISTRY)


def build_indicators(keys: Sequence[str]) -> list[Indicator]:
    unknown = [k for k in keys if k not in REGISTRY]
    if unknown:
        raise ValueError(f"Unknown indicator(s): {', '.join(unknown)}. Available: {', '.join(available_indicators())}")
    return [REGISTRY[k]() for k in keys]
