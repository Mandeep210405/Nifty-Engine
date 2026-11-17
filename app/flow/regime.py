"""Causal market-regime tag (uses only bars up to and including the signal bar)."""
from __future__ import annotations

from app.flow.bars import Series
from app.flow.features import atr


def classify_regime(spot: Series, t: int, n: int = 20) -> str:
    bars = spot.upto(t, n + 1)
    if len(bars) < n + 1:
        return "unknown"
    path = sum(abs(b.close - a.close) for a, b in zip(bars, bars[1:]))
    net = bars[-1].close - bars[0].close
    er = abs(net) / path if path > 0 else 0.0
    trend = ("trend_up" if net > 0 else "trend_down") if er >= 0.35 else ("range" if er < 0.2 else "mixed")
    cur_atr = atr(spot, t, 14)
    vol = "normal_vol"
    allb = spot.upto(t, 100)
    if cur_atr and len(allb) >= 60:
        trs = sorted(max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close)) for a, b in zip(allb, allb[1:]))
        med = trs[len(trs) // 2]
        if med > 0:
            r = cur_atr / med
            vol = "high_vol" if r >= 1.3 else "low_vol" if r <= 0.7 else "normal_vol"
    else:
        vol = "vol_unknown"
    return f"{trend}|{vol}"
