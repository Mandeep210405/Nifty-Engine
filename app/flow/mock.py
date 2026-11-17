"""MOCK derivatives generator (DEMO ONLY - synthetic, not market data).

Emits raw packets shaped like SmartWebSocketV2 SNAP_QUOTE output (paise ints, ms timestamps, best_5 lists) so the real
normaliser / classifier / bars / analyzer path is exercised. A hidden regime machine (neutral / buy / sell / absorption
phases) drives spot drift, futures OI, trade side, volume, option IV and option OI, so the detectors have something to find.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.flow.greeks import black76_delta, black76_price

IST = timezone(timedelta(hours=5, minutes=30))
LOT = 75
FUT_TOKEN = "900001"
PHASES = ["neutral", "buy", "sell", "absorb_buy", "absorb_sell"]


def _p(x: float) -> int:
    return int(round(x * 100))


def _tick(x: float, step: float = 0.05) -> float:
    return round(round(x / step) * step, 2)


class _Inst:
    def __init__(self, token, kind, strike, oi0):
        self.token, self.kind, self.strike = token, kind, strike
        self.vol = 0
        self.oi = float(oi0)
        self.ltp = 0.0
        self.pv = 0.0
        self.hi = self.lo = self.open = None
        self.seq = 0
        self.last_side = 1


class MockDerivGenerator:
    def __init__(self, spot_gen, seed: Optional[int] = None):
        self.spot_gen = spot_gen
        self.rng = random.Random(None if seed is None else seed + 7)
        today = datetime.now(IST).date()
        self.opt_exp = today + timedelta(days=5)
        self.fut_exp = today + timedelta(days=25)
        self.phase, self.phase_until = "neutral", 0.0
        self.sub_mode = "up"
        self.basis = 55.0
        self.base_iv = 14.0
        self.iv_shift = {"CE": 0.0, "PE": 0.0}
        self.tokens: set[str] = set()
        self.inst: dict[str, _Inst] = {}
        self.strikes = [x * 50.0 for x in range(int(22000 / 50), int(26500 / 50) + 1)]
        self._build()

    @staticmethod
    def _fmt(d) -> str:
        return d.strftime("%d%b%Y").upper()

    def _build(self) -> None:
        self.rows = []
        n = 910000
        fe, oe = self._fmt(self.fut_exp), self._fmt(self.opt_exp)
        self.rows.append({"token": FUT_TOKEN, "symbol": f"NIFTY{fe}FUT", "name": "NIFTY", "expiry": fe, "strike": "-1.000000",
                          "lotsize": str(LOT), "instrumenttype": "FUTIDX", "exch_seg": "NFO"})
        self.inst[FUT_TOKEN] = _Inst(FUT_TOKEN, "FUT", None, 12_000_000)
        for k in self.strikes:
            for kind in ("CE", "PE"):
                n += 1
                tok = str(n)
                self.rows.append({"token": tok, "symbol": f"NIFTY{oe}{int(k)}{kind}", "name": "NIFTY", "expiry": oe,
                                  "strike": f"{k * 100:.6f}", "lotsize": str(LOT), "instrumenttype": "OPTIDX", "exch_seg": "NFO"})
                self.inst[tok] = _Inst(tok, kind, k, 300_000 + self.rng.randint(0, 900_000))

    def master_rows(self) -> list[dict]:
        return list(self.rows)

    def subscribe(self, tokens) -> None:
        self.tokens.update(tokens)

    # -- regime ------------------------------------------------------------------------------------
    def _advance_phase(self, ts: float) -> None:
        if ts < self.phase_until:
            return
        self.phase = self.rng.choice(PHASES)
        self.phase_until = ts + self.rng.uniform(240, 600)
        self.sub_mode = self.rng.choice(["up", "down"])           # OI direction inside a directional phase
        self.spot_gen.drift = {"buy": 0.28, "sell": -0.28}.get(self.phase, 0.0)

    def _p_buy(self) -> float:
        return {"buy": 0.72, "sell": 0.28, "absorb_buy": 0.22, "absorb_sell": 0.78}.get(self.phase, 0.5)

    def _vol_mult(self) -> float:
        return {"buy": 3.0, "sell": 3.0, "absorb_buy": 4.0, "absorb_sell": 4.0}.get(self.phase, 1.0)

    # -- ticks ---------------------------------------------------------------------------------------
    def step(self, ts: float, spot: float, dt: float = 1.0) -> list[dict]:
        self._advance_phase(ts)
        self.basis += self.rng.gauss(0, 0.15) + (55.0 - self.basis) * 0.01
        F = spot + self.basis
        T = max((datetime.combine(self.opt_exp, datetime.min.time(), IST) + timedelta(hours=15, minutes=30)).timestamp() - ts, 3600) / (365 * 86400)
        for kind, up in (("CE", 1), ("PE", -1)):
            target = 0.0
            if self.phase == "buy":
                target = 0.9 if kind == "CE" else -0.5
            elif self.phase == "sell":
                target = 0.9 if kind == "PE" else -0.5
            self.iv_shift[kind] += (target - self.iv_shift[kind]) * 0.01
        out = []
        for tok in list(self.tokens):
            ins = self.inst.get(tok)
            if ins is None:
                continue
            out.append(self._fut(ins, ts, F, dt) if ins.kind == "FUT" else self._opt(ins, ts, F, T, dt))
        return out

    def _book(self, mid: float, spread: float, qbias: float, base_q: float):
        half = spread / 2
        bid, ask = _tick(mid - half), _tick(mid + half)
        if ask <= bid:
            ask = _tick(bid + 0.05)
        bids, asks = [], []
        for i in range(5):
            bids.append({"flag": 1, "quantity": int(base_q * qbias * self.rng.uniform(0.5, 1.5)), "price": _p(bid - 0.05 * i), "no of orders": self.rng.randint(1, 30)})
            asks.append({"flag": 0, "quantity": int(base_q / qbias * self.rng.uniform(0.5, 1.5)), "price": _p(ask + 0.05 * i), "no of orders": self.rng.randint(1, 30)})
        return bid, ask, bids, asks

    def _finish(self, ins: _Inst, ts: float, ltp: float, dv: int, bid_asks, oi: float, qty: int) -> dict:
        bids, asks = bid_asks
        ins.seq += 1
        ins.vol += dv
        ins.pv += ltp * dv
        ins.ltp = ltp
        ins.open = ins.open or ltp
        ins.hi = max(ins.hi or ltp, ltp)
        ins.lo = min(ins.lo or ltp, ltp)
        atp = ins.pv / ins.vol if ins.vol else ltp
        return {
            "subscription_mode": 3, "exchange_type": 2, "token": ins.token, "sequence_number": ins.seq,
            "exchange_timestamp": int(ts * 1000), "last_traded_price": _p(ltp), "last_traded_quantity": qty,
            "average_traded_price": _p(atp), "volume_trade_for_the_day": ins.vol,
            "total_buy_quantity": float(sum(b["quantity"] for b in bids) * 8), "total_sell_quantity": float(sum(a["quantity"] for a in asks) * 8),
            "open_price_of_the_day": _p(ins.open), "high_price_of_the_day": _p(ins.hi), "low_price_of_the_day": _p(ins.lo),
            "closed_price": _p(ins.open), "last_traded_timestamp": int(ts), "open_interest": int(oi), "open_interest_change_percentage": 0,
            "best_5_buy_data": bids, "best_5_sell_data": asks,
        }

    def _fut(self, ins: _Inst, ts: float, F: float, dt: float) -> dict:
        rate = 800 * self._vol_mult() * dt
        dv = int(self.rng.expovariate(1.0 / rate)) if self.rng.random() < 0.9 else 0
        buy = self.rng.random() < self._p_buy()
        bias = {"buy": 1.5, "sell": 0.67}.get(self.phase, 1.0)
        bid, ask, bids, asks = self._book(F, 0.10, bias, 3000)
        ltp = ask if (buy and dv) else bid if dv else _tick(F)
        d = 0.0
        if self.phase in ("buy", "sell"):
            d = (150 if self.sub_mode == "up" else -150) * dt
        ins.oi = max(1.0, ins.oi + d + self.rng.gauss(0, 25))
        return self._finish(ins, ts, ltp, dv, (bids, asks), ins.oi, min(dv, 1500) or 75)

    def _opt(self, ins: _Inst, ts: float, F: float, T: float, dt: float) -> dict:
        is_call = ins.kind == "CE"
        iv = (self.base_iv + self.iv_shift[ins.kind] + 0.15 * abs(ins.strike - F) / 500 * 10) / 100.0
        prem = max(0.05, black76_price(F, ins.strike, T, iv, 0.065, is_call) + self.rng.gauss(0, 0.04))
        dist = abs(ins.strike - F)
        lam = 90 * math.exp(-dist / 350) * dt
        mult, p_buy, oi_d = 1.0, 0.5, 0.0
        if self.phase == "buy":
            if is_call:
                mult, p_buy, oi_d = 4.0, 0.78, 6.0
            else:
                mult, p_buy, oi_d = 2.0, 0.25, 4.0
        elif self.phase == "sell":
            if not is_call:
                mult, p_buy, oi_d = 4.0, 0.78, 6.0
            else:
                mult, p_buy, oi_d = 2.0, 0.25, 4.0
        elif self.phase.startswith("absorb"):
            mult = 1.5
        dv = int(self.rng.expovariate(1.0 / max(lam * mult, 1e-6))) if self.rng.random() < min(0.9, 0.15 + lam * mult / 60) else 0
        spread = max(0.05, round(prem * 0.004, 2))
        bid, ask, bids, asks = self._book(prem, spread, 1.0, 1500)
        buy = self.rng.random() < p_buy
        ltp = ask if (buy and dv) else bid if dv else _tick(prem)
        ins.oi = max(1.0, ins.oi + oi_d * dt * (1 + dv / 400) + self.rng.gauss(0, 8))
        return self._finish(ins, ts, max(ltp, 0.05), dv, (bids, asks), ins.oi, min(dv, 900) or 75)
