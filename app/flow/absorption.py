"""Possible buyer / seller absorption.

Needs ALL of: heavy futures volume, a small price range, estimated aggressive flow against the side that
held, and repeated trading at the same level. Without usable trade-side estimates it stays silent (it cannot
tell buyer absorption from seller absorption). A high-volume candle alone is never labelled absorption.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.flow.features import Ctx, baseline_volume, clip01
from app.flow.models import CALCULATED, ESTIMATED, OBSERVED, Evidence


@dataclass
class AbsorptionHit:
    side: str                      # BUYER | SELLER
    level: float
    strength: float
    touches: int
    evidence: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def detect_absorption(ctx: Ctx) -> Optional[AbsorptionHit]:
    s, fb = ctx.s, ctx.fbar
    atr = ctx.fut_atr or ctx.atr_ref
    if fb is None or fb.volume is None or not atr:
        return None
    bv = baseline_volume(ctx.fut, ctx.t, s.baseline_bars, s.min_baseline_bars)
    if not bv:
        return None
    rel = fb.volume / bv
    rng = fb.high - fb.low
    if rel < s.abs_min_rel_volume or rng > s.abs_max_range_atr * atr:
        return None
    cf, imb = fb.classified_fraction, fb.imbalance
    if imb is None or cf is None or cf < s.min_classified_fraction:
        return None                      # cannot assign a side -> stay silent
    mid = (fb.high + fb.low) / 2.0
    net = fb.close - fb.open
    recent = ctx.fut.upto(ctx.t, s.abs_lookback_bars)
    tol = s.abs_touch_tol_atr * atr
    last3 = ctx.fut.upto(ctx.t, 3)
    if imb <= -s.abs_min_imbalance and fb.close >= mid and net >= -0.25 * atr:
        side, level = "BUYER", min(b.low for b in last3)
        touches = sum(1 for b in recent if abs(b.low - level) <= tol)
        opposing = f"estimated aggressive SELLING {imb:+.2f} but price held"
    elif imb >= s.abs_min_imbalance and fb.close <= mid and net <= 0.25 * atr:
        side, level = "SELLER", max(b.high for b in last3)
        touches = sum(1 for b in recent if abs(b.high - level) <= tol)
        opposing = f"estimated aggressive BUYING {imb:+.2f} but price failed to advance"
    else:
        return None
    if touches < 2:
        return None
    strength = 100.0 * (0.35 * clip01((rel - 2.0) / 2.0) + 0.25 * clip01(abs(imb) / 0.5)
                        + 0.2 * clip01((touches - 1) / 3.0) + 0.2 * (1.0 - rng / (s.abs_max_range_atr * atr)))
    ev = [
        Evidence("Futures volume", f"{rel:.2f}x the {s.baseline_bars}-bar average", "supports", CALCULATED),
        Evidence("Bar range", f"{rng:.1f} pts = {rng / atr:.2f} ATR (limit {s.abs_max_range_atr})", "supports", CALCULATED),
        Evidence("Opposing execution", opposing, "supports", ESTIMATED, f"{cf * 100:.0f}% of volume classified"),
        Evidence("Repeated trading at level", f"{touches} touches within {tol:.1f} pts of {level:.2f} in {s.abs_lookback_bars} bars",
                 "supports", CALCULATED),
    ]
    di = fb.depth_imb
    if di is not None:
        agrees = (di > 0.1) if side == "BUYER" else (di < -0.1)
        ev.append(Evidence("Displayed depth", f"imbalance {di:+.2f}", "supports" if agrees else "conflicts",
                           OBSERVED, "displayed orders can be cancelled"))
    flags = []
    if fb.tick_rule_only >= 0.6 * max(1, fb.ticks):
        flags.append("trade side mostly from tick rule")
    return AbsorptionHit(side, round(level, 2), round(min(100.0, strength), 1), touches, ev, flags,
                         {"rel_volume": round(rel, 2), "range_atr": round(rng / atr, 2), "imbalance": round(imb, 2)})
