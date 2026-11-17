"""Historical candles via SmartConnect.getCandleData (read-only market data)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.config import Instrument
from app.models import Candle
from app.timeframes import IST_OFFSET_S, TIMEFRAMES, Timeframe, bucket_start

log = logging.getLogger(__name__)
IST = timezone(timedelta(seconds=IST_OFFSET_S))
REQUEST_GAP_S = 0.5          # stay well under the historical-API rate limit


def parse_candle_rows(rows: list, tf: Timeframe, now: float) -> list[Candle]:
    """Parse [[iso_ts, o, h, l, c, v], ...]; drops malformed rows and still-forming buckets."""
    out: dict[int, Candle] = {}
    for r in rows or []:
        try:
            ts = datetime.fromisoformat(str(r[0]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            t = int(ts.timestamp())
            o, h, l, c = (float(x) for x in r[1:5])
            if min(o, h, l, c) <= 0 or h < l:
                continue
            vol = float(r[5]) if len(r) > 5 and r[5] is not None else 0.0
        except (ValueError, TypeError, IndexError):
            continue
        t = bucket_start(t, tf) if tf.key != "1d" else t
        if t + tf.seconds > now:        # still forming -> live aggregator owns it
            continue
        out[t] = Candle(time=t, open=o, high=h, low=l, close=c,
                        volume=vol if vol > 0 else None, tick_count=0)
    return [out[k] for k in sorted(out)]


def fetch_history(smart: Any, inst: Instrument, tf: Timeframe, lookback_days: int,
                  now: Optional[float] = None, sleep: Callable[[float], None] = time.sleep) -> tuple[list[Candle], dict]:
    """Returns (candles, status). status = {state: ok|empty|error|unsupported, count, message}."""
    if tf.angel_interval is None:
        return [], {"state": "unsupported", "count": 0,
                    "message": f"Angel One provides no native {tf.label} candles. "
                               f"These are built from live ticks captured while this app runs."}
    now = now or time.time()
    days = max(1, min(lookback_days, tf.max_hist_days))
    end = datetime.fromtimestamp(now, IST)
    start = end - timedelta(days=days)
    params = {"exchange": inst.exchange, "symboltoken": inst.token, "interval": tf.angel_interval,
              "fromdate": start.strftime("%Y-%m-%d %H:%M"), "todate": end.strftime("%Y-%m-%d %H:%M")}
    for attempt in (1, 2):
        try:
            resp = smart.getCandleData(dict(params))
        except Exception as e:                       # network / SDK errors
            return [], {"state": "error", "count": 0, "message": f"Historical request failed: {type(e).__name__}"}
        if isinstance(resp, dict) and resp.get("status") and resp.get("data") is not None:
            candles = parse_candle_rows(resp["data"], tf, now)
            if not candles:
                return [], {"state": "empty", "count": 0,
                            "message": "Angel One returned no historical candles for this range "
                                       "(holiday/weekend, or index history not available for this account)."}
            return candles, {"state": "ok", "count": len(candles), "message": ""}
        code = str((resp or {}).get("errorcode", "")) if isinstance(resp, dict) else ""
        msg = str((resp or {}).get("message", "unknown error"))[:160] if isinstance(resp, dict) else "bad response"
        if attempt == 1 and ("AB1004" in code or "rate" in msg.lower() or "too many" in msg.lower()):
            sleep(2.0)
            continue
        return [], {"state": "error", "count": 0, "message": f"Historical API error {code}: {msg}".strip()}
    return [], {"state": "error", "count": 0, "message": "Historical API rate limited"}
