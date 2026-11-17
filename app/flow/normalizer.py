"""Raw SmartAPI WebSocket 2.0 SNAP_QUOTE packet (NFO) -> validated :class:`DerivTick`.

Field names follow smartapi-python 1.5.5 ``_parse_binary_data`` (verified by reading the SDK source):
``open_interest`` and ``open_interest_change_percentage`` (raw integer, scale NOT verified -> display only),
``total_buy_quantity`` / ``total_sell_quantity`` (doubles), ``best_5_buy_data`` / ``best_5_sell_data``
(lists of ``{flag, quantity, price, "no of orders"}``), ``average_traded_price``, ``last_traded_timestamp``.

Prices are paise integers (divide by 100) for NFO. The SDK's own buy/sell side assignment is relied on
(flag 1 = buy per Angel's docs); a sanity check flags a crossed book instead of silently swapping sides.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from app.flow.models import DerivTick
from app.market_data.normalizer import MalformedTick, _get, _num, exchange_ts_to_seconds

PRICE_DIV = 100.0


def _levels(raw_levels: Any) -> list[tuple[float, float, int]]:
    out: list[tuple[float, float, int]] = []
    if not isinstance(raw_levels, (list, tuple)):
        return out
    for lv in raw_levels:
        if not isinstance(lv, Mapping):
            continue
        try:
            price = float(lv.get("price")) / PRICE_DIV
            qty = float(lv.get("quantity"))
            orders = int(lv.get("no of orders", lv.get("num_of_orders", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and qty >= 0:
            out.append((price, qty, orders))
    return out


def normalize_deriv_tick(raw: Mapping[str, Any], recv_ts: float, *, known_tokens: Optional[set] = None) -> DerivTick:
    if not isinstance(raw, Mapping):
        raise MalformedTick("tick is not a mapping")
    token = str(raw.get("token", "")).strip()
    if not token:
        raise MalformedTick("missing token")
    if known_tokens is not None and token not in known_tokens:
        raise MalformedTick("unknown token")
    try:
        ex_type = int(raw.get("exchange_type"))
    except (TypeError, ValueError):
        raise MalformedTick("missing exchange_type") from None
    ltp_raw = _get(raw, "ltp")
    if ltp_raw is None:
        raise MalformedTick("missing last_traded_price")
    ltp = _num(ltp_raw, "last_traded_price") / PRICE_DIV
    if ltp < 0:
        raise MalformedTick("negative price")
    ex_ts = exchange_ts_to_seconds(_get(raw, "ts"))
    src = "exchange"
    if ex_ts is None:
        ex_ts, src = recv_ts, "local"

    def opt_int(key_names: tuple[str, ...]) -> Optional[int]:
        for k in key_names:
            v = raw.get(k)
            if v is None:
                continue
            try:
                f = _num(v, k)
            except MalformedTick:
                return None
            return int(f) if f >= 0 else None
        return None

    def opt_price(key_names: tuple[str, ...]) -> Optional[float]:
        v = opt_int(key_names)
        return v / PRICE_DIV if v and v > 0 else None

    bids = sorted(_levels(raw.get("best_5_buy_data")), key=lambda x: -x[0])[:5]
    asks = sorted(_levels(raw.get("best_5_sell_data")), key=lambda x: x[0])[:5]
    depth_state = "none"
    if "best_5_buy_data" in raw or "best_5_sell_data" in raw:
        if not bids or not asks:
            depth_state = "empty_side"
        elif bids[0][0] > asks[0][0]:
            depth_state = "crossed"        # side labels may be swapped, or a transient - never trusted
        else:
            depth_state = "ok"
    tbq = raw.get("total_buy_quantity")
    tsq = raw.get("total_sell_quantity")
    try:
        tbq = float(tbq) if tbq is not None else None
        tsq = float(tsq) if tsq is not None else None
    except (TypeError, ValueError):
        tbq = tsq = None
    oi = opt_int(("open_interest",))
    oipct = raw.get("open_interest_change_percentage")
    try:
        oipct = float(oipct) if oipct is not None else None
    except (TypeError, ValueError):
        oipct = None
    return DerivTick(
        token=token, exchange_type=ex_type, ltp=round(ltp, 4), exchange_ts=ex_ts, recv_ts=recv_ts,
        sequence=opt_int(("sequence_number",)) or 0, day_volume=opt_int(("volume_trade_for_the_day", "volume")),
        last_qty=opt_int(("last_traded_quantity",)), atp=opt_price(("average_traded_price",)),
        total_buy_qty=tbq, total_sell_qty=tsq, oi=oi, oi_change_pct_raw=oipct,
        bids=bids, asks=asks, day_open=opt_price(("open_price_of_the_day",)),
        day_high=opt_price(("high_price_of_the_day",)), day_low=opt_price(("low_price_of_the_day",)),
        prev_close=opt_price(("closed_price",)), depth_state=depth_state, ts_source=src,
    )
