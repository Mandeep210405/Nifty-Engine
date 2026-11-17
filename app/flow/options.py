"""CALL / PUT activity classification per contract.

Interpretation rules (hard-coded, not tunable away):
* Open interest alone never produces a BUY or SELL label - every contract has both a buyer and a seller.
* BUY label requires: premium up AND outperforming what the underlying move explains (delta x dFutures),
  AND volume or estimated-aggressor support.
* SELL label requires: OI rising AND premium underperforming the delta-implied change.
* Execution side is ESTIMATED from snapshot quotes. "CONFIRMED EXECUTION SIDE" is only possible if a feed
  supplies real aggressor flags; SmartAPI does not, so this module never emits it.
* Anything else is MIXED ACTIVITY or INSUFFICIENT DATA, never a forced label.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.flow.features import Ctx, baseline_volume, clip01, window_bars
from app.flow.bars import Series
from app.flow.models import CALCULATED, ESTIMATED, KIND_CE, OBSERVED, UNAVAILABLE, Contract, Evidence

BUYING = "PROBABLE BUYING ACTIVITY"
SELLING = "PROBABLE SELLING ACTIVITY"
MIXED = "MIXED ACTIVITY"
INSUFFICIENT = "INSUFFICIENT DATA"
NEUTRAL = "NO NOTABLE ACTIVITY"

BUY_W = {"premium": 30.0, "volume": 20.0, "oi": 15.0, "aggressor": 20.0, "iv": 10.0, "underlying": 5.0}
SELL_W = {"premium": 30.0, "volume": 15.0, "oi": 25.0, "aggressor": 15.0, "iv": 10.0, "underlying": 5.0}


@dataclass
class OptionResult:
    token: str
    kind: str
    strike: float
    expiry: str
    label: str
    buy_score: float = 0.0
    sell_score: float = 0.0
    strength: float = 0.0
    available_weight: float = 0.0
    features: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    why_not: str = ""
    premium: Optional[float] = None
    vol_window: Optional[float] = None
    oi_chg: Optional[float] = None
    flags: list = field(default_factory=list)

    @property
    def sig_type(self) -> Optional[str]:
        if self.label == BUYING:
            return f"{'CALL' if self.kind == KIND_CE else 'PUT'}_BUY"
        if self.label == SELLING:
            return f"{'CALL' if self.kind == KIND_CE else 'PUT'}_SELL"
        return None

    def to_public(self) -> dict:
        return {"token": self.token, "kind": self.kind, "strike": self.strike, "expiry": self.expiry,
                "label": self.label, "buy_score": round(self.buy_score, 1), "sell_score": round(self.sell_score, 1),
                "strength": round(self.strength, 1), "premium": self.premium, "features": self.features,
                "why_not": self.why_not}


def _fmt(v, d=2):
    return "n/a" if v is None else f"{v:,.{d}f}"


def classify_option(ctx: Ctx, con: Contract, series: Series) -> OptionResult:
    s, w, sec = ctx.s, ctx.s.window_bars, ctx.sec
    res = OptionResult(con.token, con.kind, con.strike or 0.0, con.expiry, INSUFFICIENT)
    cur = series.get(ctx.t)
    if cur is None:
        res.why_not = "no ticks for this contract in the bar"
        return res
    res.premium = cur.close
    base = series.get(ctx.t - w * sec)
    if base is None:                                  # accept the nearest earlier bar inside the window span only
        earlier = [b for b in series.upto(ctx.t - sec, w) if b.time >= ctx.t - w * sec]
        base = earlier[0] if earlier else None
    fb, fbase = ctx.fbar, ctx.fut.get(ctx.t - w * sec)
    if base is None or fb is None or fbase is None:
        res.why_not = "no comparison bar / futures reference in the window"
        return res
    if cur.close <= 0:
        res.why_not = "non-positive premium"
        return res
    lot = con.lot_size or 1
    wins = window_bars(series, ctx.t, w, sec)
    vol_w = sum((b.volume or 0.0) for b in wins) if any(b.volume is not None for b in wins) else None
    res.vol_window = vol_w
    is_call = con.kind == KIND_CE
    ev: list[Evidence] = []
    avail_b: dict[str, Optional[float]] = {}
    avail_s: dict[str, Optional[float]] = {}

    # --- underlying move & premium vs delta ---------------------------------------------------
    dF = fb.close - fbase.close
    atr = ctx.fut_atr or ctx.atr_ref
    dP = cur.close - base.close
    delta = cur.delta
    resid = thr = None
    if delta is not None:
        resid = dP - delta * dF
        spread = cur.spread or 0.0
        thr = max(1.5 * spread, 0.01 * max(base.close, 0.0), 0.10)
        res.features.update(delta=round(delta, 3), delta_src=cur.delta_src, premium_residual=round(resid, 2),
                            residual_threshold=round(thr, 2))
    res.features.update(premium_base=round(base.close, 2), premium_change=round(dP, 2),
                        premium_change_pct=round(dP / base.close * 100, 2) if base.close else None,
                        underlying_change=round(dF, 2))
    if resid is not None:
        up = clip01(resid / (4 * thr)) if resid > thr else 0.0
        dn = clip01(-resid / (4 * thr)) if resid < -thr else 0.0
        avail_b["premium"], avail_s["premium"] = (up if dP > 0 else 0.0), dn
        ev.append(Evidence("Premium vs underlying", f"{_fmt(base.close)} -> {_fmt(cur.close)} ({dP:+.2f}); "
                           f"delta-explained {delta * dF:+.2f}, residual {resid:+.2f}",
                           "supports" if resid > thr and dP > 0 else "neutral", CALCULATED,
                           f"delta {cur.delta_src or 'n/a'}"))
    else:
        avail_b["premium"] = avail_s["premium"] = None
        ev.append(Evidence("Premium vs underlying", f"{_fmt(base.close)} -> {_fmt(cur.close)} ({dP:+.2f})", "neutral",
                           OBSERVED, "delta unavailable: cannot separate underlying-driven from demand-driven change"))

    # --- volume -----------------------------------------------------------------------------
    bvol = baseline_volume(series, ctx.t - w * sec, s.baseline_bars, 3)
    abs_ok = vol_w is not None and vol_w >= s.opt_min_window_lots * lot
    if vol_w is None:
        avail_b["volume"] = avail_s["volume"] = None
        ev.append(Evidence("Traded volume", "n/a", "neutral", UNAVAILABLE))
    else:
        rel = (vol_w / (bvol * w)) if bvol else None
        if rel is not None:
            v = clip01((rel - 1.0) / 2.0) if abs_ok else 0.0
        else:
            v = 0.5 * clip01(vol_w / (4 * s.opt_min_window_lots * lot)) if abs_ok else 0.0
        avail_b["volume"] = avail_s["volume"] = v
        res.features.update(volume_window=vol_w, volume_lots=round(vol_w / lot, 1),
                            volume_rel=round(rel, 2) if rel is not None else None)
        ev.append(Evidence("Traded volume", f"{vol_w / lot:,.0f} lots in {w} bar(s)" +
                           (f", {rel:.2f}x the contract's average" if rel is not None else ", no baseline yet"),
                           "supports" if v > 0 else "neutral", CALCULATED,
                           "" if abs_ok else f"below minimum of {s.opt_min_window_lots:.0f} lots"))

    # --- OI ------------------------------------------------------------------------------------
    oi_rising = oi_falling = False
    if cur.oi_close is not None and base.oi_close is not None and base.oi_close > 0 and cur.oi_close > 0:
        dOI = cur.oi_close - base.oi_close
        pct = dOI / base.oi_close * 100.0
        res.oi_chg = dOI
        oi_rising = pct >= s.opt_min_oi_change_pct and dOI >= s.opt_min_oi_lots * lot
        oi_falling = pct <= -s.opt_min_oi_change_pct and -dOI >= s.opt_min_oi_lots * lot
        o = clip01(abs(pct) / (5 * s.opt_min_oi_change_pct)) if oi_rising else 0.0
        avail_b["oi"] = o if oi_rising else (0.0 if oi_falling else 0.2)
        avail_s["oi"] = o
        res.features.update(oi_change=round(dOI, 0), oi_change_pct=round(pct, 2), oi_change_lots=round(dOI / lot, 1))
        ev.append(Evidence("Open interest", f"{base.oi_close:,.0f} -> {cur.oi_close:,.0f} ({dOI:+,.0f}, {pct:+.2f}%)",
                           "neutral", OBSERVED,
                           "rising OI = new contracts opened (buyer and seller both exist); falling OI = positions closing"
                           if oi_rising or oi_falling else "no material OI change"))
    else:
        avail_b["oi"] = avail_s["oi"] = None
        ev.append(Evidence("Open interest", "n/a", "neutral", UNAVAILABLE))

    # --- estimated aggressor -----------------------------------------------------------------------
    B, S = sum(b.buy_vol for b in wins), sum(b.sell_vol for b in wins)
    tot = vol_w or 0.0
    if tot > 0 and (B + S) / tot >= s.min_classified_fraction:
        imb = (B - S) / (B + S)
        avail_b["aggressor"], avail_s["aggressor"] = clip01(imb / s.aggr_full_imbalance), clip01(-imb / s.aggr_full_imbalance)
        res.features["est_imbalance"] = round(imb, 2)
        ev.append(Evidence("Estimated trade side", f"{imb:+.2f} imbalance ({(B + S) / tot * 100:.0f}% classified)",
                           "supports" if abs(imb) > 0.2 else "neutral", ESTIMATED,
                           "inferred from snapshot quotes; execution side is not confirmed"))
    else:
        avail_b["aggressor"] = avail_s["aggressor"] = None
        ev.append(Evidence("Estimated trade side", "n/a", "neutral", UNAVAILABLE, "not enough classifiable volume"))

    # --- IV ---------------------------------------------------------------------------------------
    if cur.iv is not None and base.iv is not None:
        dIV = cur.iv - base.iv
        avail_b["iv"] = clip01(dIV / (3 * s.opt_iv_step)) if dIV > 0 else 0.0
        avail_s["iv"] = clip01(-dIV / (3 * s.opt_iv_step)) if dIV < 0 else 0.0
        res.features.update(iv=round(cur.iv, 2), iv_change=round(dIV, 2), iv_src="OBSERVED" if cur.iv_obs is not None else "CALCULATED")
        ev.append(Evidence("Implied volatility", f"{base.iv:.2f} -> {cur.iv:.2f} ({dIV:+.2f} pts)",
                           "supports" if abs(dIV) >= s.opt_iv_step else "neutral",
                           OBSERVED if cur.iv_obs is not None else CALCULATED))
    else:
        avail_b["iv"] = avail_s["iv"] = None
        ev.append(Evidence("Implied volatility", "n/a", "neutral", UNAVAILABLE))

    # --- underlying consistency --------------------------------------------------------------------
    if atr:
        z = dF / atr
        flat = abs(z) < 0.25
        if is_call:
            avail_b["underlying"] = 1.0 if z >= 0.25 else 0.25 if flat else 0.0
            avail_s["underlying"] = 1.0 if z <= -0.25 else 0.5 if flat else 0.0
        else:
            avail_b["underlying"] = 1.0 if z <= -0.25 else 0.25 if flat else 0.0
            avail_s["underlying"] = 1.0 if z >= 0.25 else 0.5 if flat else 0.0
        ev.append(Evidence("Underlying (futures)", f"{dF:+.1f} pts = {z:+.2f} ATR", "neutral", CALCULATED))
    else:
        avail_b["underlying"] = avail_s["underlying"] = None

    def score(weights: dict, vals: dict) -> tuple[float, float]:
        pts = sum(weights[k] * (vals.get(k) or 0.0) for k in weights)
        avail = sum(weights[k] for k in weights if vals.get(k) is not None)
        return pts, avail

    res.buy_score, aw = score(BUY_W, avail_b)
    res.sell_score, _ = score(SELL_W, avail_s)
    res.available_weight = aw
    res.evidence = ev

    # --- quality gates ---------------------------------------------------------------------------
    res.flags.extend(f for f in ([f"{cur.oi_invalid} invalid OI tick(s) ignored"] if cur.oi_invalid else []))
    if cur.ticks < 2:
        res.flags.append("single tick in bar (thin quote)")
    if aw < 55.0 or avail_b.get("premium") is None:
        res.label, res.why_not = INSUFFICIENT, ("delta/premium comparison unavailable" if avail_b.get("premium") is None
                                                else f"only {aw:.0f}% of evidence weight available")
        return res
    prem_up = dP > 0 and (resid is not None and resid > thr)
    prem_under = resid is not None and resid < -thr
    vol_or_aggr = (avail_b.get("volume") or 0) > 0 or (avail_b.get("aggressor") or 0) > 0
    buy_ok = (res.buy_score >= s.opt_emit_min and res.buy_score - res.sell_score >= s.opt_dominance_margin
              and prem_up and vol_or_aggr)
    sell_ok = (res.sell_score >= s.opt_emit_min and res.sell_score - res.buy_score >= s.opt_dominance_margin
               and oi_rising and prem_under)
    if buy_ok:
        res.label, res.strength = BUYING, res.buy_score
    elif sell_ok:
        res.label, res.strength = SELLING, res.sell_score
    elif max(res.buy_score, res.sell_score) >= 40:
        res.label = MIXED
        res.why_not = "evidence is split or a required condition is missing (e.g. OI up but premium not underperforming)"
    else:
        res.label = NEUTRAL
    return res


def summarize(results: list[OptionResult]) -> tuple[Optional[float], Optional[float]]:
    """(bullish, bearish) evidence strengths 0-100 for the pressure engine; None if no usable contract."""
    usable = [r for r in results if r.label not in (INSUFFICIENT,)]
    if not usable:
        return None, None
    bull = max([r.strength for r in usable if r.sig_type in ("CALL_BUY", "PUT_SELL")] or [0.0])
    bear = max([r.strength for r in usable if r.sig_type in ("PUT_BUY", "CALL_SELL")] or [0.0])
    return bull, bear
