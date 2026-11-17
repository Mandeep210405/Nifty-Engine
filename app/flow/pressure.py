"""Buying / selling pressure engine: transparent weighted score 0-100 with per-component breakdown.

The score is NOT a probability and NOT proof of institutional participation. Components without real data
contribute 0 (they are not renormalised away), are shown as UNAVAILABLE, and lower the data-quality rating.
If less than ``min_available_weight`` of the weight has data the score is suppressed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.flow.config import score_label
from app.flow.features import Ctx, baseline_volume, clip01, window_bars
from app.flow.models import CALCULATED, ESTIMATED, OBSERVED, UNAVAILABLE
from app.flow.positioning import Positioning

DISPLAYED = OBSERVED


@dataclass
class Component:
    name: str
    weight: float
    buy01: Optional[float] = None
    sell01: Optional[float] = None
    raw: str = ""
    provenance: str = OBSERVED
    note: str = ""

    @property
    def available(self) -> bool:
        return self.buy01 is not None

    def to_dict(self) -> dict:
        b = round(self.weight * (self.buy01 or 0), 1)
        s = round(self.weight * (self.sell01 or 0), 1)
        return {"name": self.name, "weight": round(self.weight, 1), "available": self.available,
                "buy_points": b, "sell_points": s, "raw": self.raw if self.available else "no data",
                "provenance": self.provenance if self.available else UNAVAILABLE, "note": self.note}


@dataclass
class Pressure:
    buy_score: float = 0.0
    sell_score: float = 0.0
    components: list = field(default_factory=list)
    available_weight: float = 0.0
    suppressed: bool = False
    reason: str = ""
    window: int = 0

    @property
    def buy_label(self) -> str:
        return score_label(self.buy_score)

    @property
    def sell_label(self) -> str:
        return score_label(self.sell_score)

    def to_dict(self) -> dict:
        return {"buy_score": round(self.buy_score, 1), "sell_score": round(self.sell_score, 1),
                "buy_label": self.buy_label, "sell_label": self.sell_label,
                "available_weight": round(self.available_weight, 1), "suppressed": self.suppressed,
                "reason": self.reason, "components": [c.to_dict() for c in self.components]}


def _signed(v01_signed: float) -> tuple[float, float]:
    return clip01(v01_signed), clip01(-v01_signed)


def compute_pressure(ctx: Ctx, pos: Positioning, opt_bull: Optional[float], opt_bear: Optional[float]) -> Pressure:
    s, w = ctx.s, ctx.s.window_bars
    W = s.weights
    comps: list[Component] = []
    fut = ctx.fbar
    atr_ref = ctx.atr_ref
    wb = window_bars(ctx.fut, ctx.t, w, ctx.sec)

    # 1 price movement (spot index preferred: it is what the chart shows)
    c = Component("price", W["price"])
    base_spot = ctx.spot.get(ctx.t - w * ctx.sec)
    if ctx.sbar is not None and base_spot is not None and atr_ref:
        mv = ctx.sbar.close - base_spot.close
        c.buy01, c.sell01 = _signed(mv / (s.price_full_atr * atr_ref))
        c.raw = f"{mv:+.1f} pts over {w} bar(s) = {mv / atr_ref:+.2f} ATR"
        c.provenance = CALCULATED
    elif fut is not None and ctx.fut.get(ctx.t - w * ctx.sec) is not None and (ctx.fut_atr or atr_ref):
        a = ctx.fut_atr or atr_ref
        mv = fut.close - ctx.fut.get(ctx.t - w * ctx.sec).close
        c.buy01, c.sell01 = _signed(mv / (s.price_full_atr * a))
        c.raw = f"futures {mv:+.1f} pts over {w} bar(s) = {mv / a:+.2f} ATR (spot unavailable)"
        c.provenance = CALCULATED
    comps.append(c)

    # 2 volume relative to average, on the side of the bar's direction
    c = Component("volume", W["volume"])
    bv = baseline_volume(ctx.fut, ctx.t, s.baseline_bars, s.min_baseline_bars)
    if fut is not None and fut.volume is not None and bv:
        rel = fut.volume / bv
        v = clip01((rel - 1.0) / (s.volume_full_ratio - 1.0))
        up = fut.close > fut.open
        dn = fut.close < fut.open
        c.buy01, c.sell01 = (v if up else 0.0), (v if dn else 0.0)
        c.raw = f"futures volume {rel:.2f}x its {s.baseline_bars}-bar average, bar {'up' if up else 'down' if dn else 'flat'}"
        c.provenance = CALCULATED
    else:
        c.note = f"needs >= {s.min_baseline_bars} bars of futures volume history"
    comps.append(c)

    # 3 futures OI positioning
    c = Component("futures_oi", W["futures_oi"])
    if pos.state in ("ok", "noise"):
        f = pos.strength / 100.0 if pos.state == "ok" else 0.0
        b = {"LONG_BUILDUP": 1.0, "SHORT_COVERING": 0.5}.get(pos.cls or "", 0.0) * f
        sl = {"SHORT_BUILDUP": 1.0, "LONG_UNWINDING": 0.5}.get(pos.cls or "", 0.0) * f
        c.buy01, c.sell01 = b, sl
        c.raw = (pos.cls.replace("_", " ").lower() if pos.cls else "no qualifying price/OI move") + \
            (f" (OI {pos.oi_pct:+.2f}%)" if pos.oi_pct is not None else "")
        c.provenance = CALCULATED
    else:
        c.note = pos.reason
    comps.append(c)

    # 4 estimated aggressive buying/selling over the window
    c = Component("aggressive", W["aggressive"])
    if wb:
        B, S = sum(b.buy_vol for b in wb), sum(b.sell_vol for b in wb)
        tot = sum((b.volume or 0.0) for b in wb)
        if tot > 0 and (B + S) / tot >= s.min_classified_fraction:
            imb = (B - S) / (B + S)
            c.buy01, c.sell01 = _signed(imb / s.aggr_full_imbalance)
            c.raw = f"estimated buy/sell volume imbalance {imb:+.2f} ({(B + S) / tot * 100:.0f}% of volume classified)"
            c.provenance = ESTIMATED
        else:
            c.note = "too little volume could be assigned to a side"
    comps.append(c)

    # 5 CVD trend (estimated)
    c = Component("cvd", W["cvd"])
    n = s.cvd_bars
    seq = ctx.fut.upto(ctx.t, n + 1)
    if len(seq) >= n + 1 and all(b.cvd_close is not None for b in (seq[0], seq[-1])):
        vol = sum((b.volume or 0.0) for b in seq[1:])
        if vol > 0:
            slope = (seq[-1].cvd_close - seq[0].cvd_close) / vol
            c.buy01, c.sell01 = _signed(slope / s.cvd_full_slope)
            c.raw = f"estimated CVD slope {slope:+.2f} of volume over {n} bars"
            c.provenance = ESTIMATED
    if not c.available:
        c.note = f"needs {n} bars of classified futures volume"
    comps.append(c)

    # 6 displayed depth imbalance (low weight; orders can be cancelled)
    c = Component("depth", W["depth"])
    imbs = [b.depth_imb for b in wb if b.depth_imb is not None]
    tot_imbs = [b.tot_imb for b in wb if b.tot_imb is not None]
    pool = imbs + tot_imbs
    if pool:
        im = sum(pool) / len(pool)
        c.buy01, c.sell01 = _signed(im / s.depth_full_imbalance)
        c.raw = f"displayed bid/ask quantity imbalance {im:+.2f}"
        c.provenance = DISPLAYED
        c.note = "Displayed depth is not executed volume; orders can be cancelled or modified."
    else:
        c.note = "no valid order-book data"
    comps.append(c)

    # 7 position vs exchange average traded price
    c = Component("vwap", W["vwap"])
    if fut is not None and fut.atp and (ctx.fut_atr or atr_ref):
        a = ctx.fut_atr or atr_ref
        d = (fut.close - fut.atp) / a
        c.buy01, c.sell01 = _signed(d / s.vwap_full_atr)
        c.raw = f"futures {fut.close - fut.atp:+.1f} pts vs day VWAP {fut.atp:.2f}"
        c.provenance = OBSERVED
    comps.append(c)

    # 8 options confirmation
    c = Component("options", W["options"])
    if opt_bull is not None and opt_bear is not None:
        net = (opt_bull - opt_bear) / 100.0
        c.buy01, c.sell01 = _signed(net)
        c.raw = f"bullish option evidence {opt_bull:.0f} vs bearish {opt_bear:.0f}"
        c.provenance = ESTIMATED
    else:
        c.note = "no option contract had enough data"
    comps.append(c)

    p = Pressure(components=comps, window=w)
    p.available_weight = sum(c.weight for c in comps if c.available)
    tw = sum(W.values()) or 1.0
    p.available_weight = p.available_weight * 100.0 / tw
    p.buy_score = sum(c.weight * (c.buy01 or 0.0) for c in comps) * 100.0 / tw
    p.sell_score = sum(c.weight * (c.sell01 or 0.0) for c in comps) * 100.0 / tw
    if p.available_weight < s.min_available_weight:
        p.suppressed = True
        p.reason = f"only {p.available_weight:.0f}% of the scoring weight has data (< {s.min_available_weight:.0f}%)"
    return p
