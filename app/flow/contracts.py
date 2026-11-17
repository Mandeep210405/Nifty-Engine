"""Contract universe from the Angel One instrument master (NFO, NIFTY index derivatives).

Master rows (verified against the public OpenAPIScripMaster.json layout):
  futures : instrumenttype FUTIDX, name NIFTY, expiry "25NOV2026", strike -1
  options : instrumenttype OPTIDX, name NIFTY, expiry "25NOV2026", strike = strike*100 (string), symbol ends CE/PE
Different expiries are never mixed: one futures expiry and ONE option expiry are selected and reported.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Iterable, Optional

from app.flow.models import KIND_CE, KIND_FUT, KIND_PE, Contract

IST = timezone(timedelta(hours=5, minutes=30))
_MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
_OPT_RE = re.compile(r"^NIFTY(\d{2}[A-Z]{3}\d{4})(\d+(?:\.\d+)?)(CE|PE)$")
_FUT_RE = re.compile(r"^NIFTY(\d{2}[A-Z]{3}\d{4})FUT$")


def parse_expiry(s: str) -> Optional[date]:
    m = re.fullmatch(r"(\d{2})([A-Z]{3})(\d{4})", (s or "").strip().upper())
    if not m or m.group(2) not in _MON:
        return None
    try:
        return date(int(m.group(3)), _MON[m.group(2)], int(m.group(1)))
    except ValueError:
        return None


def expiry_ts(d: date) -> float:
    return datetime.combine(d, dtime(15, 30), IST).timestamp()


def filter_master(entries: Iterable[dict], underlying: str = "NIFTY") -> list[dict]:
    """Keep only NFO NIFTY index futures/options rows (keeps memory small after the 100k+ row master)."""
    out = []
    for e in entries:
        if e.get("exch_seg") != "NFO" or e.get("name") != underlying:
            continue
        if e.get("instrumenttype") in ("FUTIDX", "OPTIDX"):
            out.append({k: e.get(k) for k in ("token", "symbol", "name", "expiry", "strike", "lotsize", "instrumenttype")})
    return out


def to_contracts(rows: Iterable[dict]) -> list[Contract]:
    out: list[Contract] = []
    for e in rows:
        sym = str(e.get("symbol", "")).strip().upper()
        exp = parse_expiry(str(e.get("expiry", "")))
        if exp is None:
            continue
        try:
            lot = int(float(e.get("lotsize") or 1))
        except (TypeError, ValueError):
            lot = 1
        fm = _FUT_RE.match(sym)
        if fm and e.get("instrumenttype") == "FUTIDX":
            out.append(Contract(str(e["token"]), sym, KIND_FUT, str(e["expiry"]).upper(), expiry_ts(exp), None, lot))
            continue
        om = _OPT_RE.match(sym)
        if om and e.get("instrumenttype") == "OPTIDX":
            try:
                strike = float(e["strike"]) / 100.0
            except (TypeError, ValueError):
                continue
            # symbol strike must agree with the master strike field (instrument token validation)
            if abs(strike - float(om.group(2))) > 1e-6:
                continue
            out.append(Contract(str(e["token"]), sym, KIND_CE if om.group(3) == "CE" else KIND_PE,
                                str(e["expiry"]).upper(), expiry_ts(exp), strike, lot))
    return out


def _live_expiries(contracts: list[Contract], kind_set: set, now: float) -> list[tuple[float, str]]:
    seen = {(c.expiry_ts, c.expiry) for c in contracts if c.kind in kind_set and c.expiry_ts > now}
    return sorted(seen)


def pick_futures(contracts: list[Contract], now: float, override: str = "") -> Optional[Contract]:
    cands = [c for c in contracts if c.kind == KIND_FUT and c.expiry_ts > now]
    if override:
        cands = [c for c in cands if c.expiry == override]
    return min(cands, key=lambda c: c.expiry_ts) if cands else None


def pick_option_expiry(contracts: list[Contract], now: float, override: str = "") -> Optional[str]:
    exps = _live_expiries(contracts, {KIND_CE, KIND_PE}, now)
    if override:
        return override if any(e == override for _, e in exps) else None
    return exps[0][1] if exps else None


def option_window(contracts: list[Contract], expiry: str, spot: float, each_side: int) -> tuple[Optional[float], list[Contract]]:
    """ATM strike (nearest listed) and CE+PE contracts for ATM +/- ``each_side`` listed strikes."""
    rows = [c for c in contracts if c.kind in (KIND_CE, KIND_PE) and c.expiry == expiry and c.strike]
    strikes = sorted({c.strike for c in rows})
    if not strikes:
        return None, []
    atm = min(strikes, key=lambda s: (abs(s - spot), s))
    i = strikes.index(atm)
    chosen = set(strikes[max(0, i - each_side): i + each_side + 1])
    sel = [c for c in rows if c.strike in chosen]
    sel.sort(key=lambda c: (c.strike, c.kind))
    return atm, sel
