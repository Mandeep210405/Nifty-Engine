"""Market-session helpers (NSE cash: Mon-Fri 09:15-15:30 IST). Exchange holidays are NOT modelled."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def ist_datetime(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, IST)


def in_cash_session(ts: float) -> bool:
    d = ist_datetime(ts)
    if d.weekday() >= 5:
        return False
    minutes = d.hour * 60 + d.minute
    return 9 * 60 + 15 <= minutes < 15 * 60 + 30
