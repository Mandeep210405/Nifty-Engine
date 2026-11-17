"""Supported chart timeframes and candle time-bucket maths.

All timestamps are Unix seconds in UTC. Display conversion to Asia/Kolkata is done
in the browser. Bucket alignment:

* 10s / 30s      : aligned to the Unix epoch (equivalently to the wall-clock minute).
* 1m .. 1h       : aligned to the NSE cash-session open, 09:15 IST, so live candles line
                   up exactly with Angel One's historical candles (e.g. 10m -> 09:15, 09:25).
* 1d             : one candle per IST calendar day (bucket start = 00:00 IST).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

IST_OFFSET_S = 5 * 3600 + 30 * 60            # Asia/Kolkata has no DST
SESSION_OPEN_IST_S = 9 * 3600 + 15 * 60      # 09:15 IST seconds-after-midnight
SESSION_OPEN_UTC_ANCHOR = SESSION_OPEN_IST_S - IST_OFFSET_S  # 13500 (03:45 UTC)


@dataclass(frozen=True)
class Timeframe:
    key: str
    label: str
    seconds: int
    angel_interval: Optional[str]   # None => no native broker candles (tick-built only)
    anchor: int                     # bucket alignment offset (seconds, UTC)
    max_hist_days: int = 0          # per-request window for getCandleData (0 = n/a)


_TF_LIST = [
    Timeframe("10s", "10 sec", 10, None, 0),
    Timeframe("30s", "30 sec", 30, None, 0),
    Timeframe("1m", "1 min", 60, "ONE_MINUTE", SESSION_OPEN_UTC_ANCHOR, 30),
    Timeframe("3m", "3 min", 180, "THREE_MINUTE", SESSION_OPEN_UTC_ANCHOR, 60),
    Timeframe("5m", "5 min", 300, "FIVE_MINUTE", SESSION_OPEN_UTC_ANCHOR, 100),
    Timeframe("10m", "10 min", 600, "TEN_MINUTE", SESSION_OPEN_UTC_ANCHOR, 100),
    Timeframe("15m", "15 min", 900, "FIFTEEN_MINUTE", SESSION_OPEN_UTC_ANCHOR, 200),
    Timeframe("30m", "30 min", 1800, "THIRTY_MINUTE", SESSION_OPEN_UTC_ANCHOR, 200),
    Timeframe("1h", "1 hour", 3600, "ONE_HOUR", SESSION_OPEN_UTC_ANCHOR, 400),
    Timeframe("1d", "1 day", 86400, "ONE_DAY", -IST_OFFSET_S, 2000),
]

TIMEFRAMES: dict[str, Timeframe] = {t.key: t for t in _TF_LIST}
TIMEFRAME_KEYS: list[str] = [t.key for t in _TF_LIST]


def get_timeframe(key: str) -> Timeframe:
    try:
        return TIMEFRAMES[key]
    except KeyError:
        raise ValueError(f"Unsupported timeframe {key!r}. Allowed: {', '.join(TIMEFRAME_KEYS)}") from None


def bucket_start(ts: float, tf: Timeframe) -> int:
    """Start (UTC unix seconds) of the candle bucket containing ``ts``."""
    t = int(ts // 1)
    return ((t - tf.anchor) // tf.seconds) * tf.seconds + tf.anchor
