"""FlowAnalyzer: turns CLOSED bars into explainable signals.

Pure with respect to I/O: feed it closed bars in time order and it returns signals. Because it only ever sees
bars up to the evaluation bar there is no look-ahead, and the same class drives live analysis, replay and tests.
"""
from __future__ import annotations

import time as _time
from collections import deque
from dataclasses import asdict
from typing import Optional

from app.flow import options as opt
from app.flow.absorption import detect_absorption
from app.flow.bars import Series, SpotBar
from app.flow.config import FlowSettings, score_label
from app.flow.features import Ctx, atr, baseline_volume, clip01
from app.flow.greeks import black76_delta, implied_vol
from app.flow.models import (CALCULATED, ESTIMATED, KIND_CE, KIND_FUT, KIND_PE, OBSERVED, SIGNAL_TYPES, UNAVAILABLE,
                             Contract, Evidence, FlowSignal, InstBar)
from app.flow.positioning import TEXT as POS_TEXT, Positioning, classify_positioning
from app.flow.pressure import Pressure, compute_pressure
from app.flow.quality import bar_flags, rate
from app.flow.regime import classify_regime
from app.flow.traps import TrapDetector
from app.timeframes import TIMEFRAMES

RANK = {"Weak": 0, "Developing": 1, "Moderate": 2, "Strong": 3, "Very Strong": 4}
OBSERVED_GREEK_MAX_AGE_S = 180.0


class _TfState:
    def __init__(self) -> None:
        self.fut = Series(900)
        self.fut_token: Optional[str] = None
        self.spot = Series(900)
        self.opt: dict[str, Series] = {}
        self.traps = TrapDetector()
        self.last_pos_cls: Optional[str] = None
        self.last_pos_emit = 0
        self.last_emit: dict[tuple, int] = {}
        self.pressure_active: dict[str, int] = {}          # type -> band rank currently active
        self.opt_types_at: dict[int, list[str]] = {}
        self.latest: dict = {}


class FlowAnalyzer:
    def __init__(self, settings: FlowSettings, mode: str = "live"):
        self.s = settings
        self.mode = mode
        self.st: dict[str, _TfState] = {k: _TfState() for k in settings.timeframes}
        self.seen: set[str] = set()
        self.suppressed_counts: dict[str, int] = {}

    # ---------------------------------------------------------------------------------------------
    def ingest_closed(self, tf: str, t: int, inst_bars: dict[str, InstBar], spot_bar: Optional[SpotBar],
                      contracts: dict[str, Contract], active_option_tokens: set[str], prev_day: Optional[dict] = None,
                      observed_greeks: Optional[dict] = None, wall_now: Optional[float] = None) -> list[FlowSignal]:
        s, sec = self.s, TIMEFRAMES[tf].seconds
        st = self.st[tf]
        wall_now = wall_now if wall_now is not None else _time.time()
        fut = next((b for b in inst_bars.values() if b.kind == KIND_FUT), None)
        if fut is not None:
            if st.fut_token not in (None, fut.token):            # contract roll: never mix OI across contracts
                st.fut = Series(900)
            st.fut_token = fut.token
        if spot_bar is not None:
            st.spot.append(spot_bar)
        if fut is not None:
            fut.spot = spot_bar.close if spot_bar else None
            st.fut.append(fut)
        for tok, b in inst_bars.items():
            if b.kind in (KIND_CE, KIND_PE):
                con = contracts.get(tok)
                if con is not None:
                    self._greeks(b, con, fut, t, sec, (observed_greeks or {}).get(tok), wall_now)
                st.opt.setdefault(tok, Series(900)).append(b)
        ctx = Ctx(s, tf, sec, t, st.fut, st.spot, st.fut.get(t), st.spot.get(t),
                  atr(st.spot, t, s.atr_bars), atr(st.fut, t, s.atr_bars), self.mode)
        if ctx.fbar is None and ctx.sbar is None:
            return []
        pos = classify_positioning(ctx)
        results = []
        for tok in sorted(active_option_tokens):
            con = contracts.get(tok)
            if con is not None and tok in st.opt:
                results.append(opt.classify_option(ctx, con, st.opt[tok]))
        bull, bear = opt.summarize(results)
        pr = compute_pressure(ctx, pos, bull, bear)
        regime = classify_regime(st.spot, t)
        sigs: list[FlowSignal] = []
        sigs += self._futures_signals(ctx, st, pos, pr, regime)
        sigs += self._pressure_signals(ctx, st, pos, pr, regime)
        sigs += self._aggressor_signals(ctx, st, regime)
        opt_sigs = self._option_signals(ctx, st, results, regime, contracts)
        sigs += opt_sigs
        st.opt_types_at[t] = [x.type for x in opt_sigs]
        for k in [k for k in st.opt_types_at if k < t - 60 * sec]:
            del st.opt_types_at[k]
        sigs += self._absorption_signals(ctx, st, regime)
        sigs += self._trap_signals(ctx, st, pos, pr, prev_day, regime)
        out = []
        for g in sigs:
            if g.id in self.seen:
                continue                                         # duplicate-event prevention
            self.seen.add(g.id)
            out.append(g)
        st.latest = {"time": t, "tf": tf, "regime": regime, "underlying": ctx.underlying,
                     "pressure": pr.to_dict(), "positioning": asdict(pos),
                     "options": [r.to_public() for r in results],
                     "basis": (None if fut is None or spot_bar is None else round(fut.close - spot_bar.close, 2))}
        return out

    # -- greeks -----------------------------------------------------------------------------------
    def _greeks(self, b: InstBar, con: Contract, fut: Optional[InstBar], t: int, sec: int, obs: Optional[tuple],
                wall_now: float) -> None:
        F = fut.close if fut is not None else None
        b.underlying, b.spot = F, None
        T = (con.expiry_ts - (t + sec)) / (365.0 * 86400.0)
        is_call = con.kind == KIND_CE
        if F and con.strike:
            b.iv_calc = implied_vol(b.close, F, con.strike, T, self.s.risk_free_rate, is_call)
        if obs is not None and wall_now - obs[2] <= OBSERVED_GREEK_MAX_AGE_S:
            b.iv_obs = obs[0]
            if obs[1] is not None:
                b.delta, b.delta_src = obs[1], "OBSERVED"
        if b.delta is None and F and con.strike and b.iv is not None:
            d = black76_delta(F, con.strike, T, b.iv / 100.0, self.s.risk_free_rate, is_call)
            if d is not None:
                b.delta, b.delta_src = d, "CALCULATED"

    # -- helpers ----------------------------------------------------------------------------------
    def _cool(self, st: _TfState, key: tuple, ctx: Ctx, bars: Optional[int] = None) -> bool:
        """True if allowed to emit (and records the emission)."""
        last = st.last_emit.get(key)
        n = (bars if bars is not None else self.s.cooldown_bars) * ctx.sec
        if last is not None and ctx.t - last < n:
            return False
        st.last_emit[key] = ctx.t
        return True

    def _mk(self, ctx: Ctx, typ: str, label: str, strength: float, quality: tuple, headline: str, evidence: list,
            interpretation: str, regime: str, *, instrument: str = "NIFTY", token: Optional[str] = None,
            strike: Optional[float] = None, expiry: Optional[str] = None, premium: Optional[float] = None,
            vol_change: Optional[float] = None, oi_change: Optional[float] = None, components: Optional[dict] = None,
            details: Optional[dict] = None) -> FlowSignal:
        tag, group, direction = SIGNAL_TYPES[typ]
        rating, reasons = quality
        d = dict(details or {})
        d["quality_reasons"] = reasons
        return FlowSignal(
            id=f"{ctx.tf}:{ctx.t}:{typ}:{token or ''}", time=ctx.t, tf=ctx.tf, tf_seconds=ctx.sec, type=typ,
            direction=direction, label=label, strength=round(strength, 1), strength_label=score_label(strength),
            data_quality=rating, underlying_price=ctx.underlying, instrument=instrument, token=token, strike=strike,
            expiry=expiry, premium=premium, vol_change=vol_change, oi_change=oi_change, headline=headline,
            evidence=[e.to_dict() for e in evidence], components=components or {}, interpretation=interpretation,
            details=d, regime=regime, mode=self.mode, created_at=_time.time())

    # -- futures positioning ---------------------------------------------------------------------------
    def _futures_signals(self, ctx: Ctx, st: _TfState, pos: Positioning, pr: Pressure, regime: str) -> list[FlowSignal]:
        s = self.s
        cls = pos.cls if pos.state == "ok" else None
        prev, st.last_pos_cls = st.last_pos_cls, cls
        if cls is None or pos.strength < s.positioning_emit_min:
            return []
        if cls == prev and not self._cool(st, ("POS", cls), ctx, s.window_bars):
            return []
        st.last_emit[("POS", cls)] = ctx.t
        flags = bar_flags(ctx.fbar, ctx.sec)
        q = rate(100.0 if pos.vol_rel is not None else 85.0, flags)
        ev = [
            Evidence("Futures price", f"{pos.price_chg:+.1f} pts ({pos.price_pct:+.3f}%) over {pos.window} bar(s) "
                     f"vs {pos.base_time and ctx.fut.get(pos.base_time).close:.2f}", "supports", OBSERVED,
                     f"noise filter {pos.price_thr:.1f} pts"),
            Evidence("Futures open interest", f"{pos.oi_base:,.0f} -> {pos.oi_now:,.0f} ({pos.oi_chg:+,.0f}, {pos.oi_pct:+.2f}%)",
                     "supports", OBSERVED, f"noise filter {self.s.min_oi_change_pct}%"),
        ]
        ev.append(Evidence("Futures volume", "n/a" if pos.vol_rel is None else f"{pos.vol_rel:.2f}x average",
                           "supports" if (pos.vol_rel or 0) >= 1.2 else "neutral",
                           CALCULATED if pos.vol_rel is not None else UNAVAILABLE))
        if cls in ("SHORT_COVERING", "LONG_UNWINDING"):
            ev.append(Evidence("Nature of move", "closing of existing positions, not fresh participation", "conflicts",
                               CALCULATED, "the move can fade once the covering/unwinding is done"))
        return [self._mk(ctx, cls, cls.replace("_", " "), pos.strength, q, cls.replace("_", " ").title(), ev, POS_TEXT[cls],
                         regime, instrument="FUT", token=ctx.fbar.token if ctx.fbar else None,
                         oi_change=pos.oi_chg, vol_change=None,
                         details={"window_bars": pos.window, "comparison_bar": pos.base_time, "price_threshold": pos.price_thr,
                                  "basis": st.latest.get("basis")})]

    # -- pressure ----------------------------------------------------------------------------------
    def _pressure_signals(self, ctx: Ctx, st: _TfState, pos: Positioning, pr: Pressure, regime: str) -> list[FlowSignal]:
        s, out = self.s, []
        if pr.suppressed:
            self.suppressed_counts["pressure_insufficient_data"] = self.suppressed_counts.get("pressure_insufficient_data", 0) + 1
            st.pressure_active.clear()
            return out
        flags = bar_flags(ctx.fbar, ctx.sec)
        q = rate(pr.available_weight, flags)
        for typ, mine, other in (("BUY_PRESSURE", pr.buy_score, pr.sell_score), ("SELL_PRESSURE", pr.sell_score, pr.buy_score)):
            buy = typ == "BUY_PRESSURE"
            ok = mine >= s.pressure_emit_min and mine - other >= s.pressure_margin and q[0] != "Poor"
            rank = RANK[score_label(mine)]
            if not ok:
                st.pressure_active.pop(typ, None)
                continue
            if typ in st.pressure_active and rank <= st.pressure_active[typ]:
                continue                                               # same episode, no escalation
            st.pressure_active[typ] = rank
            ev = []
            for c in pr.components:
                d = c.to_dict()
                m, o = (c.buy01, c.sell01) if buy else (c.sell01, c.buy01)
                state = "neutral" if not c.available else "supports" if (m or 0) >= 0.15 else "conflicts" if (o or 0) >= 0.15 else "neutral"
                ev.append(Evidence(c.name.replace("_", " "), c.raw if c.available else "unavailable", state,
                                   c.provenance if c.available else UNAVAILABLE, c.note))
            comp = {c.name: {"weight": round(c.weight, 1), "buy_points": c.to_dict()["buy_points"],
                             "sell_points": c.to_dict()["sell_points"], "available": c.available} for c in pr.components}
            what = "buying" if buy else "selling"
            n_sup = sum(1 for e in ev if e.state == "supports")
            interp = (f"{n_sup} of {len(ev)} evidence components point to {what} pressure "
                      f"(score {mine:.0f}, opposing side {other:.0f}). This describes recent activity; it is not a prediction "
                      f"and does not identify who is trading.")
            out.append(self._mk(ctx, typ, f"{what.upper()} PRESSURE", mine, q, f"{what.title()} pressure detected", ev, interp,
                                regime, instrument="FUT", token=ctx.fbar.token if ctx.fbar else None, components=comp,
                                details={"buy_score": round(pr.buy_score, 1), "sell_score": round(pr.sell_score, 1),
                                         "window_bars": pr.window, "available_weight": round(pr.available_weight, 1)}))
        return out

    # -- estimated aggressive buying / selling -----------------------------------------------------------
    def _aggressor_signals(self, ctx: Ctx, st: _TfState, regime: str) -> list[FlowSignal]:
        s, fb = self.s, ctx.fbar
        if fb is None or fb.volume is None:
            return []
        cf, imb = fb.classified_fraction, fb.imbalance
        bv = baseline_volume(ctx.fut, ctx.t, s.baseline_bars, s.min_baseline_bars)
        if imb is None or cf is None or cf < s.min_classified_fraction or not bv:
            return []
        rel = fb.volume / bv
        if rel < s.aggr_emit_rel_volume or abs(imb) < s.aggr_emit_imbalance:
            return []
        buy = imb > 0
        if (buy and fb.close <= fb.open) or (not buy and fb.close >= fb.open):
            return []                                                   # flow not confirmed by the candle
        typ = "AGG_BUY" if buy else "AGG_SELL"
        if not self._cool(st, (typ,), ctx):
            return []
        strength = 100.0 * (0.6 * clip01(abs(imb) / 0.6) + 0.4 * clip01((rel - 1) / 2))
        q = rate(100.0 if fb.atp else 90.0, bar_flags(fb, ctx.sec))
        ev = [Evidence("Estimated trade-side imbalance", f"{imb:+.2f} ({cf * 100:.0f}% of volume classified)", "supports", ESTIMATED,
                       "quote/tick rule on snapshot ticks; execution side is not confirmed"),
              Evidence("Futures volume", f"{rel:.2f}x average", "supports", CALCULATED),
              Evidence("Candle", f"{fb.open:.2f} -> {fb.close:.2f}", "supports", OBSERVED)]
        if fb.tick_rule_only >= 0.6 * max(1, fb.ticks):
            ev.append(Evidence("Classification method", "mostly tick rule (no usable quotes)", "conflicts", ESTIMATED))
        return [self._mk(ctx, typ, "ESTIMATED " + ("AGGRESSIVE BUYING" if buy else "AGGRESSIVE SELLING"), strength, q,
                         "Estimated aggressive " + ("buying" if buy else "selling"), ev,
                         "Heavy futures volume whose estimated trade side leans " + ("buy" if buy else "sell") +
                         " and the candle agrees. Trade side is inferred, not confirmed.", regime, instrument="FUT", token=fb.token,
                         details={"buy_volume_est": fb.buy_vol, "sell_volume_est": fb.sell_vol, "unclassified": fb.unclassified_vol})]

    # -- options -------------------------------------------------------------------------------------
    def _option_signals(self, ctx: Ctx, st: _TfState, results: list, regime: str, contracts: dict) -> list[FlowSignal]:
        out: list[FlowSignal] = []
        by_type: dict[str, list] = {}
        for r in results:
            if r.sig_type:
                by_type.setdefault(r.sig_type, []).append(r)
        for typ, rs in by_type.items():
            rs.sort(key=lambda r: -r.strength)
            for r in rs[: self.s.opt_max_per_type_per_bar]:
                if not self._cool(st, (typ, r.token), ctx):
                    continue
                q = rate(r.available_weight, r.flags)
                if q[0] == "Poor":
                    self.suppressed_counts["option_poor_quality"] = self.suppressed_counts.get("option_poor_quality", 0) + 1
                    continue
                side = "CALL" if r.kind == KIND_CE else "PUT"
                verb = "BUYING" if typ.endswith("BUY") else "SELLING"
                con = contracts.get(r.token)
                f = r.features
                impl = {"CALL_BUY": "consistent with bullish positioning", "CALL_SELL": "consistent with a view that the market will not rise (call writing)",
                        "PUT_BUY": "consistent with bearish positioning/hedging", "PUT_SELL": "consistent with a view that the market will not fall (put writing)"}[typ]
                interp = (f"{side} {r.strike:,.0f} shows PROBABLE {verb} activity ({impl}). Execution side is ESTIMATED from premium, "
                          f"volume, OI, IV and quote behaviour; it is not confirmed and the participant is unknown.")
                out.append(self._mk(ctx, typ, f"{side} {verb} PRESSURE", r.strength, q, f"{side} {verb.lower()} pressure ({r.strike:,.0f} {r.kind})",
                                    r.evidence, interp, regime, instrument=r.kind, token=r.token, strike=r.strike, expiry=r.expiry,
                                    premium=r.premium, vol_change=r.vol_window, oi_change=r.oi_chg,
                                    components={"buy_score": round(r.buy_score, 1), "sell_score": round(r.sell_score, 1)},
                                    details={"execution_side": "ESTIMATED", "features": f, "evidence_weight_available": round(r.available_weight, 1),
                                             "lot_size": con.lot_size if con else None}))
        return out

    # -- absorption ------------------------------------------------------------------------------------
    def _absorption_signals(self, ctx: Ctx, st: _TfState, regime: str) -> list[FlowSignal]:
        hit = detect_absorption(ctx)
        if hit is None:
            return []
        typ = "BUYER_ABSORPTION" if hit.side == "BUYER" else "SELLER_ABSORPTION"
        if not self._cool(st, (typ,), ctx, self.s.abs_lookback_bars):
            return []
        q = rate(90.0, bar_flags(ctx.fbar, ctx.sec) + hit.flags)
        word = "buyers absorbing selling" if hit.side == "BUYER" else "sellers absorbing buying"
        return [self._mk(ctx, typ, f"POSSIBLE {hit.side} ABSORPTION", hit.strength, q, f"Possible {hit.side.lower()} absorption at {hit.level:,.2f}",
                         hit.evidence, f"Heavy volume with little price progress and repeated trading near {hit.level:,.2f}: consistent with {word}. "
                         "This is an inference from estimated trade sides, not proof of who is absorbing.", regime,
                         instrument="FUT", token=ctx.fbar.token, details={"level": hit.level, "touches": hit.touches, **hit.metrics})]

    # -- traps ----------------------------------------------------------------------------------------
    def _trap_signals(self, ctx: Ctx, st: _TfState, pos: Positioning, pr: Pressure, prev_day, regime: str) -> list[FlowSignal]:
        out = []
        for h in st.traps.update(ctx, pos, pr, prev_day, st.opt_types_at):
            typ = "BULL_TRAP" if h.kind == "BULL" else "BEAR_TRAP"
            q = rate(pr.available_weight, bar_flags(ctx.fbar, ctx.sec))
            ev = list(h.evidence)
            opt_break = h.details["breakout"]["options"]
            opt_rev = sorted(set(h.details["failure"]["options"]) | set(h.details["confirmation"]["options"]))
            ev.append(Evidence("Option activity at breakout", ", ".join(opt_break) or "none detected", "neutral", ESTIMATED))
            ev.append(Evidence("Option activity at reversal", ", ".join(opt_rev) or "none detected", "neutral", ESTIMATED))
            word = "bull" if h.kind == "BULL" else "bear"
            out.append(self._mk(ctx, typ, f"POSSIBLE {word.upper()} TRAP", h.strength, q,
                                f"Possible {word} trap around {h.level:,.2f} ({h.level_name})", ev,
                                f"Price broke {'above' if h.kind == 'BULL' else 'below'} {h.level_name} {h.level:,.2f} with "
                                f"{'buying' if h.kind == 'BULL' else 'selling'} activity, failed to hold, and opposing pressure appeared on the "
                                "confirmation candle. Marked only after confirmation.", regime, instrument="NIFTY",
                                details={"level": h.level, "level_name": h.level_name, "breakout_time": h.breakout_time,
                                         "failure_time": h.failure_time, "confirmation_time": h.confirm_time, **h.details}))
        return out
