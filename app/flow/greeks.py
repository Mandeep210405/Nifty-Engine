"""Black-76 pricing helpers (futures as the underlying forward).

Used ONLY to compute CALCULATED delta / implied volatility when broker Greeks are not available.
Inputs are observed premium and observed futures price; the risk-free rate is a config assumption.
IV is returned in volatility points (e.g. 14.2). Returns None whenever the inputs do not allow a
reliable value - never a guess.
"""
from __future__ import annotations

import math
from typing import Optional

MIN_T_YEARS = 1.0 / (365.0 * 24.0)      # below ~1 hour to expiry the maths is unstable -> unavailable


def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def black76_price(f: float, k: float, t: float, sigma: float, r: float, is_call: bool) -> float:
    if sigma <= 0 or t <= 0:
        disc = math.exp(-r * max(t, 0))
        return disc * max(f - k, 0.0) if is_call else disc * max(k - f, 0.0)
    sd = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    disc = math.exp(-r * t)
    if is_call:
        return disc * (f * _ncdf(d1) - k * _ncdf(d2))
    return disc * (k * _ncdf(-d2) - f * _ncdf(-d1))


def black76_delta(f: float, k: float, t: float, sigma: float, r: float, is_call: bool) -> Optional[float]:
    if f <= 0 or k <= 0 or t < MIN_T_YEARS or sigma <= 0:
        return None
    sd = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sd * sd) / sd
    disc = math.exp(-r * t)
    return disc * (_ncdf(d1) if is_call else _ncdf(d1) - 1.0)


def implied_vol(price: float, f: float, k: float, t: float, r: float, is_call: bool) -> Optional[float]:
    """Bisection IV in vol points, or None if the premium is outside no-arbitrage bounds."""
    if price <= 0 or f <= 0 or k <= 0 or t < MIN_T_YEARS:
        return None
    disc = math.exp(-r * t)
    intrinsic = disc * (max(f - k, 0.0) if is_call else max(k - f, 0.0))
    upper = disc * (f if is_call else k)
    if price < intrinsic - 1e-9 or price >= upper:
        return None
    lo, hi = 1e-4, 5.0
    if black76_price(f, k, t, hi, r, is_call) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if black76_price(f, k, t, mid, r, is_call) < price:
            lo = mid
        else:
            hi = mid
    iv = 0.5 * (lo + hi)
    return round(iv * 100.0, 4) if 0.01 < iv < 4.9 else None
