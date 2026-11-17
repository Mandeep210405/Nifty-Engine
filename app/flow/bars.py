"""Per-timeframe bar construction for derivative instruments and the spot index.

Bars close on a TIMER (bucket end + grace), never on a single tick, and closed bars are immutable:
signals computed from them cannot repaint. Late ticks (older than the finalised watermark) are dropped
and counted, not merged. Missing buckets stay missing - nothing is interpolated.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from app.flow.models import DerivTick, InstBar
from app.flow.tradeclass import TradeSide
from app.timeframes import TIMEFRAMES, bucket_start


@dataclass
class SpotBar:
    time: int
    open: float
    high: float
    low: float
    close: float
    ticks: int = 1


class Series:
    """Time-ordered closed bars with O(1) lookup by bucket start."""

    def __init__(self, maxlen: int = 600):
        self.items: deque = deque(maxlen=maxlen)
        self.by_time: dict[int, object] = {}

    def append(self, bar) -> None:
        if self.items and bar.time <= self.items[-1].time:
            return                                   # never go backwards / duplicate
        if len(self.items) == self.items.maxlen:
            self.by_time.pop(self.items[0].time, None)
        self.items.append(bar)
        self.by_time[bar.time] = bar

    def get(self, t: int):
        return self.by_time.get(t)

    def upto(self, t: int, n: int) -> list:
        """Up to n most recent bars with time <= t (ascending)."""
        out = []
        for b in reversed(self.items):
            if b.time <= t:
                out.append(b)
                if len(out) >= n:
                    break
        return out[::-1]

    def __len__(self) -> int:
        return len(self.items)


def new_bar(tick: DerivTick, kind: str, tf: str, bucket: int, prev_oi: Optional[float]) -> InstBar:
    return InstBar(token=tick.token, kind=kind, tf=tf, time=bucket, open=tick.ltp, high=tick.ltp, low=tick.ltp,
                   close=tick.ltp, oi_open=prev_oi, first_ts=tick.exchange_ts, last_ts=tick.exchange_ts)


def update_bar(bar: InstBar, tick: DerivTick, trade: Optional[TradeSide], cvd: float,
               valid_oi: Optional[float], oi_invalid: bool) -> None:
    p = tick.ltp
    bar.high, bar.low, bar.close = max(bar.high, p), min(bar.low, p), p
    bar.ticks += 1
    bar.last_ts = max(bar.last_ts, tick.exchange_ts)
    bar.last_recv = tick.recv_ts
    if tick.day_volume is not None and bar.volume is None:
        bar.volume = 0.0
    if trade is not None:
        bar.volume = (bar.volume or 0.0) + trade.volume
        if trade.side > 0:
            bar.buy_vol += trade.volume
        elif trade.side < 0:
            bar.sell_vol += trade.volume
        else:
            bar.unclassified_vol += trade.volume
        if trade.method == "tick":
            bar.tick_rule_only += 1
    bar.cvd_close = cvd
    if tick.atp:
        bar.atp = tick.atp
    if oi_invalid:
        bar.oi_invalid += 1
    if valid_oi is not None:
        if bar.oi_close is None or valid_oi != bar.oi_close:
            bar.oi_updates += 1
        if bar.oi_open is None:
            bar.oi_open = valid_oi
        bar.oi_close = valid_oi
    if tick.depth_state == "ok":
        bid, ask = tick.best_bid, tick.best_ask
        bar.spread_sum += ask - bid
        bar.spread_n += 1
        bq = sum(q for _, q, _ in tick.bids)
        aq = sum(q for _, q, _ in tick.asks)
        if bq + aq > 0:
            bar.depth_imb_sum += (bq - aq) / (bq + aq)
            bar.depth_imb_n += 1
    elif tick.depth_state in ("crossed", "empty_side"):
        bar.depth_invalid += 1
    if tick.total_buy_qty is not None and tick.total_sell_qty is not None:
        bar.tbq_close, bar.tsq_close = tick.total_buy_qty, tick.total_sell_qty
        t = tick.total_buy_qty + tick.total_sell_qty
        if t > 0:
            bar.tot_imb_sum += (tick.total_buy_qty - tick.total_sell_qty) / t
            bar.tot_imb_n += 1


class BarBook:
    def __init__(self, tf_keys: tuple[str, ...], grace_s: float = 3.0):
        self.tfs = {k: TIMEFRAMES[k] for k in tf_keys}
        self.grace = grace_s
        self.open: dict[str, dict[int, dict[str, InstBar]]] = {k: {} for k in tf_keys}
        self.spot_open: dict[str, dict[int, SpotBar]] = {k: {} for k in tf_keys}
        self.watermark: dict[str, int] = {k: 0 for k in tf_keys}
        self.late_dropped = 0
        self._last_oi: dict[str, float] = {}

    def bucket(self, tf: str, ts: float) -> int:
        return bucket_start(ts, self.tfs[tf])

    def add_spot(self, ts: float, price: float) -> None:
        for k in self.tfs:
            b = self.bucket(k, ts)
            if b <= self.watermark[k]:
                continue
            sb = self.spot_open[k].get(b)
            if sb is None:
                self.spot_open[k][b] = SpotBar(b, price, price, price, price)
            else:
                sb.high, sb.low, sb.close, sb.ticks = max(sb.high, price), min(sb.low, price), price, sb.ticks + 1

    def add_deriv(self, tick: DerivTick, kind: str, trade: Optional[TradeSide], cvd: float,
                  valid_oi: Optional[float], oi_invalid: bool) -> None:
        if valid_oi is not None:
            self._last_oi[tick.token] = valid_oi
        for k in self.tfs:
            b = self.bucket(k, tick.exchange_ts)
            if b <= self.watermark[k]:
                self.late_dropped += 1
                continue
            bars = self.open[k].setdefault(b, {})
            bar = bars.get(tick.token)
            if bar is None:
                bar = bars[tick.token] = new_bar(tick, kind, k, b, self._last_oi.get(tick.token))
            update_bar(bar, tick, trade, cvd, valid_oi, oi_invalid)

    def due(self, tf: str, now: float) -> list[int]:
        sec = self.tfs[tf].seconds
        buckets = set(self.open[tf]) | set(self.spot_open[tf])
        return sorted(b for b in buckets if b + sec + self.grace <= now)

    def pop(self, tf: str, bucket: int) -> tuple[dict[str, InstBar], Optional[SpotBar]]:
        bars = self.open[tf].pop(bucket, {})
        spot = self.spot_open[tf].pop(bucket, None)
        self.watermark[tf] = max(self.watermark[tf], bucket)
        return bars, spot

    def reset_baselines(self) -> None:
        self._last_oi.clear()
