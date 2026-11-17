"""Futures price x OI four-way positioning (long buildup / short buildup / short covering / long unwinding).

Never classifies from one tick: it compares closed bars ``window_bars`` apart and requires both the price
move and the OI change to exceed noise filters (ATR-scaled / bps / OI %).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.flow.features import Ctx, baseline_volume

CLASSES = {
    (1, 1): "LONG_BUILDUP", (-1, 1): "SHORT_BUILDUP", (1, -1): "SHORT_COVERING", (-1, -1): "LONG_UNWINDING",
}
TEXT = {
    "LONG_BUILDUP": "Futures price up with open interest up: new long positions are being added.",
    "SHORT_BUILDUP": "Futures price down with open interest up: new short positions are being added.",
    "SHORT_COVERING": "Futures price up with open interest down: shorts are closing (not fresh buying).",
    "LONG_UNWINDING": "Futures price down with open interest down: longs are closing (not fresh selling).",
}


@dataclass
class Positioning:
    state: str                       # ok | noise | unavailable
    cls: Optional[str] = None
    reason: str = ""
    window: int = 0
    base_time: Optional[int] = None
    price_chg: Optional[float] = None
    price_pct: Optional[float] = None
    oi_chg: Optional[float] = None
    oi_pct: Optional[float] = None
    oi_base: Optional[float] = None
    oi_now: Optional[float] = None
    vol_rel: Optional[float] = None
    price_thr: Optional[float] = None
    strength: float = 0.0


def classify_positioning(ctx: Ctx) -> Positioning:
    s, w = ctx.s, ctx.s.window_bars
    cur, base = ctx.fbar, ctx.fut.get(ctx.t - w * ctx.sec)
    p = Positioning(state="unavailable", window=w)
    if cur is None or base is None:
        p.reason = "futures bar missing for the comparison window"
        return p
    if cur.oi_close is None or base.oi_close is None or cur.oi_close <= 0 or base.oi_close <= 0:
        p.reason = "open interest unavailable/invalid in the window"
        return p
    p.base_time = base.time
    p.price_chg = cur.close - base.close
    p.price_pct = p.price_chg / base.close * 100.0
    p.oi_now, p.oi_base = cur.oi_close, base.oi_close
    p.oi_chg = cur.oi_close - base.oi_close
    p.oi_pct = p.oi_chg / base.oi_close * 100.0
    thr = s.min_move_bps * cur.close / 1e4
    if ctx.fut_atr:
        thr = max(thr, s.min_move_atr * ctx.fut_atr)
    p.price_thr = thr
    bv = baseline_volume(ctx.fut, ctx.t, s.baseline_bars, s.min_baseline_bars)
    if bv and cur.volume is not None:
        p.vol_rel = cur.volume / bv if bv > 0 else None
    if abs(p.price_chg) < thr or abs(p.oi_pct) < s.min_oi_change_pct:
        p.state = "noise"
        p.reason = "price or OI change below the noise filter"
        return p
    p.cls = CLASSES[(1 if p.price_chg > 0 else -1, 1 if p.oi_chg > 0 else -1)]
    p.state = "ok"
    pf = min(1.0, abs(p.price_chg) / (3 * thr))
    of = min(1.0, abs(p.oi_pct) / (5 * s.min_oi_change_pct))
    vf = min(1.0, max(0.0, (p.vol_rel - 1.0) / 2.0)) if p.vol_rel else 0.0
    p.strength = round(100.0 * (0.4 * pf + 0.4 * of + 0.2 * vf), 1)
    return p
