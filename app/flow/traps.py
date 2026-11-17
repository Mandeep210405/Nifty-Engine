"""Bull / bear trap state machine on spot bars (levels) with futures flow as activity evidence.

Sequence:  breakout candle (close beyond level + buffer, with buying/selling activity)
        -> failure candle (a LATER candle closes back through the level)
        -> confirmation candle (a LATER candle still on the wrong side AND opposing pressure present).
The signal is created on the confirmation candle only - nothing is marked earlier and nothing is repainted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.flow.features import Ctx, baseline_volume, clip01
from app.flow.models import CALCULATED, ESTIMATED, OBSERVED, Evidence
from app.flow.positioning import Positioning
from app.flow.pressure import Pressure


def _day(t: int) -> int:
    return (t + 19800) // 86400


@dataclass
class TrapHit:
    kind: str                       # BULL | BEAR
    level: float
    level_name: str
    breakout_time: int
    failure_time: int
    confirm_time: int
    strength: float
    evidence: list = field(default_factory=list)
    details: dict = field(default_factory=dict)


class TrapDetector:
    def __init__(self) -> None:
        self.state: dict[str, dict] = {}
        self.cooldown: dict[str, int] = {}

    def reset(self) -> None:
        self.state.clear()
        self.cooldown.clear()

    # -- levels known BEFORE bar t ------------------------------------------------------------------
    def _levels(self, ctx: Ctx, prev_day: Optional[dict]) -> dict[str, list[tuple[str, float]]]:
        s, t = ctx.s, ctx.t
        prior = ctx.spot.upto(t - ctx.sec, s.trap_swing_bars)
        up: list[tuple[str, float]] = []
        dn: list[tuple[str, float]] = []
        if prior:
            hi = max(prior, key=lambda b: (b.high, -b.time))
            lo = min(prior, key=lambda b: (b.low, b.time))
            if (t - hi.time) / ctx.sec >= s.trap_min_level_age:
                up.append(("SWING_HIGH", hi.high))
            if (t - lo.time) / ctx.sec >= s.trap_min_level_age:
                dn.append(("SWING_LOW", lo.low))
            today = [b for b in ctx.spot.upto(t - ctx.sec, 2000) if _day(b.time) == _day(t)]
            if today:
                sh = max(today, key=lambda b: (b.high, -b.time))
                sl = min(today, key=lambda b: (b.low, b.time))
                if (t - sh.time) / ctx.sec >= s.trap_min_level_age:
                    up.append(("SESSION_HIGH", sh.high))
                if (t - sl.time) / ctx.sec >= s.trap_min_level_age:
                    dn.append(("SESSION_LOW", sl.low))
        if prev_day:
            if prev_day.get("high"):
                up.append(("PDH", float(prev_day["high"])))
            if prev_day.get("low"):
                dn.append(("PDL", float(prev_day["low"])))
        return {"up": up, "dn": dn}

    def update(self, ctx: Ctx, pos: Positioning, pr: Pressure, prev_day: Optional[dict],
               option_types_at: dict[int, list[str]]) -> list[TrapHit]:
        sb = ctx.sbar
        atr = ctx.atr_ref
        hits: list[TrapHit] = []
        if sb is None or not atr:
            return hits
        s, t, sec = ctx.s, ctx.t, ctx.sec
        buf = s.trap_buffer_atr * atr
        prev = ctx.spot.get(t - sec)
        rel = None
        bv = baseline_volume(ctx.fut, t, s.baseline_bars, s.min_baseline_bars)
        if bv and ctx.fbar is not None and ctx.fbar.volume is not None:
            rel = ctx.fbar.volume / bv

        def stage_info() -> dict:
            return {"time": t, "close": sb.close, "fut_volume_rel": None if rel is None else round(rel, 2),
                    "oi_change_pct": None if pos.oi_pct is None else round(pos.oi_pct, 3),
                    "positioning": pos.cls, "buy_score": round(pr.buy_score, 1), "sell_score": round(pr.sell_score, 1),
                    "options": option_types_at.get(t, [])}

        for kind in ("BULL", "BEAR"):
            bull = kind == "BULL"
            st = self.state.get(kind)
            # ---- advance an existing state --------------------------------------------------------
            if st is not None and t > st["break"]["time"]:
                lvl = st["level"]
                through = sb.close < lvl if bull else sb.close > lvl      # back on the original side
                if st["stage"] == "broken":
                    if through:
                        st["stage"], st["fail"] = "failed", stage_info()
                        st["fail"]["depth"] = abs(lvl - sb.close)
                    elif (t - st["break"]["time"]) // sec >= s.trap_fail_bars:
                        del self.state[kind]                              # breakout held: not a trap
                elif st["stage"] == "failed" and t > st["fail"]["time"]:
                    opp = (pr.sell_score >= s.trap_activity_score or pos.cls in ("SHORT_BUILDUP", "LONG_UNWINDING")) if bull \
                        else (pr.buy_score >= s.trap_activity_score or pos.cls in ("LONG_BUILDUP", "SHORT_COVERING"))
                    if through and opp and not pr.suppressed:
                        hits.append(self._hit(kind, st, stage_info(), atr, pr, option_types_at))
                        self.cooldown[kind] = t
                        del self.state[kind]
                    elif not through:
                        st["stage"] = "broken"                            # reclaimed the level -> re-armed
                        st.pop("fail", None)
                    elif (t - st["fail"]["time"]) // sec >= s.trap_confirm_bars:
                        del self.state[kind]                              # never confirmed: nothing marked
            # ---- look for a new breakout ------------------------------------------------------------
            if kind not in self.state and prev is not None:
                if t - self.cooldown.get(kind, 0) < s.cooldown_bars * sec:
                    continue
                cands = self._levels(ctx, prev_day)["up" if bull else "dn"]
                broke = [(n, L) for n, L in cands if
                         ((prev.close <= L and sb.close > L + buf) if bull else (prev.close >= L and sb.close < L - buf))]
                if not broke:
                    continue
                name, L = max(broke, key=lambda x: x[1]) if bull else min(broke, key=lambda x: x[1])
                act_score = pr.buy_score if bull else pr.sell_score
                active = (rel is not None and rel >= s.trap_activity_rel_volume) or \
                         (not pr.suppressed and act_score >= s.trap_activity_score)
                if not active:
                    continue
                self.state[kind] = {"stage": "broken", "level": L, "level_name": name, "break": stage_info()}
        return hits

    def _hit(self, kind: str, st: dict, conf: dict, atr: float, pr: Pressure, opt_at: dict) -> TrapHit:
        bull = kind == "BULL"
        br, fl = st["break"], st["fail"]
        s1 = clip01((br["fut_volume_rel"] - 1.0) / 2.0) if br["fut_volume_rel"] else 0.0
        s2 = clip01(fl["depth"] / atr)
        s3 = (pr.sell_score if bull else pr.buy_score) / 100.0
        strength = round(100.0 * (0.3 * s1 + 0.3 * s2 + 0.4 * s3), 1)
        word = "above" if bull else "below"
        ev = [
            Evidence("Breakout candle", f"closed {br['close']:.2f}, {word} {st['level_name']} {st['level']:.2f}", "supports",
                     CALCULATED, f"futures volume {br['fut_volume_rel']}x avg, pressure score {br['buy_score' if bull else 'sell_score']}"),
            Evidence("Failed breakout candle", f"closed {fl['close']:.2f}, back {'below' if bull else 'above'} the level", "supports",
                     CALCULATED, f"{fl['depth']:.1f} pts through the level"),
            Evidence("Confirmation candle", f"closed {conf['close']:.2f}, still {'below' if bull else 'above'}", "supports",
                     CALCULATED, f"{'selling' if bull else 'buying'} pressure {conf['sell_score' if bull else 'buy_score']}, futures {conf['positioning'] or 'no OI class'}"),
        ]
        return TrapHit(kind, st["level"], st["level_name"], br["time"], fl["time"], conf["time"], strength, ev,
                       {"breakout": br, "failure": fl, "confirmation": conf})
