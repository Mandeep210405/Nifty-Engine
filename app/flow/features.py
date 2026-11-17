"""Small shared numeric helpers (pure, causal: they only look at bars with time <= the evaluation bar)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.flow.bars import Series
from app.flow.config import FlowSettings


def clip01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def atr(series: Series, t: int, n: int, min_bars: int = 5) -> Optional[float]:
    bars = series.upto(t, n + 1)
    if len(bars) < min_bars + 1:
        return None
    trs = []
    for prev, cur in zip(bars, bars[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs) / len(trs) if trs else None


def baseline_volume(series: Series, t: int, n: int, min_n: int) -> Optional[float]:
    """Mean per-bar volume of up to n bars strictly BEFORE t; None if fewer than min_n bars have volume."""
    vols = [b.volume for b in series.upto(t - 1, n) if b.volume is not None]
    return sum(vols) / len(vols) if len(vols) >= min_n else None


def window_bars(series: Series, t: int, w: int, sec: int) -> list:
    """The w bars ending at t that exist (a missing bucket is NOT filled)."""
    return [b for k in range(w - 1, -1, -1) if (b := series.get(t - k * sec)) is not None]


@dataclass
class Ctx:
    """Everything an engine may look at for one closed bar. Nothing later than ``t`` is reachable."""
    s: FlowSettings
    tf: str
    sec: int
    t: int
    fut: Series
    spot: Series
    fbar: Optional[object]            # InstBar of the futures at t
    sbar: Optional[object]            # SpotBar at t
    spot_atr: Optional[float]
    fut_atr: Optional[float]
    mode: str = "live"

    @property
    def underlying(self) -> Optional[float]:
        return self.sbar.close if self.sbar is not None else (self.fbar.close if self.fbar is not None else None)

    @property
    def atr_ref(self) -> Optional[float]:
        return self.spot_atr if self.spot_atr is not None else self.fut_atr
