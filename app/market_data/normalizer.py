"""Raw SmartAPI WebSocket 2.0 packet -> validated :class:`Tick`.

Field names follow the dictionaries produced by ``SmartApi.smartWebSocketV2.SmartWebSocketV2``
(smartapi-python 1.5.5), which decodes the little-endian binary frames:

    subscription_mode, exchange_type, token, sequence_number,
    exchange_timestamp (epoch MILLISECONDS), last_traded_price (integer PAISE),
    and, for QUOTE / SNAP_QUOTE:
    last_traded_quantity, average_traded_price, volume_trade_for_the_day,
    open_price_of_the_day, high_price_of_the_day, low_price_of_the_day, closed_price

Prices are integers in paise (divide by 100) for every segment except currency derivatives
(exchange_type 13, divide by 10,000,000). The parser also tolerates a few documented/observed
alias spellings and second-resolution timestamps.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from app.models import Tick


class MalformedTick(ValueError):
    pass


def price_divisor(exchange_type: int) -> float:
    return 10_000_000.0 if exchange_type == 13 else 100.0


_ALIASES = {
    "ltp": ("last_traded_price", "ltp"),
    "ts": ("exchange_timestamp", "exchange_time", "exchange_ts"),
    "volume": ("volume_trade_for_the_day", "volume", "vol"),
    "qty": ("last_traded_quantity", "ltq"),
    "close": ("closed_price", "close_price"),
    "open": ("open_price_of_the_day", "open_price"),
    "high": ("high_price_of_the_day", "high_price"),
    "low": ("low_price_of_the_day", "low_price"),
    "seq": ("sequence_number", "seq"),
}


def _get(raw: Mapping[str, Any], key: str) -> Any:
    for name in _ALIASES[key]:
        if name in raw and raw[name] is not None:
            return raw[name]
    return None


def _num(v: Any, what: str) -> float:
    if isinstance(v, bool):
        raise MalformedTick(f"{what} is not numeric")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise MalformedTick(f"{what} is not numeric") from None
    if not math.isfinite(f):
        raise MalformedTick(f"{what} is not finite")
    return f


def exchange_ts_to_seconds(v: Any) -> Optional[float]:
    """Exchange timestamp -> UTC epoch seconds. ms (documented) or s accepted; 0/None -> None."""
    if v is None:
        return None
    f = _num(v, "exchange_timestamp")
    if f <= 0:
        return None
    if f > 1e11:          # milliseconds
        return f / 1000.0
    if f > 1e8:           # already seconds
        return f
    raise MalformedTick("exchange_timestamp out of range")


def normalize_tick(raw: Mapping[str, Any], recv_ts: float, *,
                   expected_token: str | None = None,
                   expected_exchange_type: int | None = None) -> Tick:
    if not isinstance(raw, Mapping):
        raise MalformedTick("tick is not a mapping")

    token = raw.get("token")
    if token is None or str(token).strip() == "":
        raise MalformedTick("missing token")
    token = str(token).strip()
    if expected_token is not None and token != str(expected_token):
        raise MalformedTick("unexpected token")

    try:
        ex_type = int(raw.get("exchange_type"))
    except (TypeError, ValueError):
        raise MalformedTick("missing/invalid exchange_type") from None
    if expected_exchange_type is not None and ex_type != expected_exchange_type:
        raise MalformedTick("unexpected exchange_type")

    div = price_divisor(ex_type)
    ltp_raw = _get(raw, "ltp")
    if ltp_raw is None:
        raise MalformedTick("missing last_traded_price")
    price = _num(ltp_raw, "last_traded_price") / div
    if price <= 0:
        raise MalformedTick("non-positive price")

    ex_ts = exchange_ts_to_seconds(_get(raw, "ts"))
    ts_source = "exchange"
    if ex_ts is None:
        ex_ts, ts_source = recv_ts, "local"

    def opt_price(key: str) -> Optional[float]:
        v = _get(raw, key)
        if v is None:
            return None
        try:
            f = _num(v, key) / div
        except MalformedTick:
            return None
        return f if f > 0 else None

    def opt_int(key: str) -> Optional[int]:
        v = _get(raw, key)
        if v is None:
            return None
        try:
            f = _num(v, key)
        except MalformedTick:
            return None
        return int(f) if f >= 0 else None

    seq = opt_int("seq") or 0
    return Tick(
        token=token, exchange_type=ex_type, price=round(price, 4),
        exchange_ts=ex_ts, recv_ts=recv_ts, sequence=seq,
        day_volume=opt_int("volume"), last_qty=opt_int("qty"),
        prev_close=opt_price("close"), day_open=opt_price("open"),
        day_high=opt_price("high"), day_low=opt_price("low"),
        mode=int(raw.get("subscription_mode") or 1), ts_source=ts_source,
    )
