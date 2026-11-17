"""Flow-layer data structures. Provenance constants tag every displayed figure."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

OBSERVED = "OBSERVED"          # taken directly from the broker feed
CALCULATED = "CALCULATED"      # deterministic arithmetic on observed data
ESTIMATED = "ESTIMATED"        # model-based inference (e.g. trade-side classification)
UNAVAILABLE = "UNAVAILABLE"    # not provided / not reliable - never replaced by an invented value

KIND_FUT, KIND_CE, KIND_PE = "FUT", "CE", "PE"


@dataclass
class DerivTick:
    """Validated NFO tick. Prices in rupees; times UTC unix seconds."""
    token: str
    exchange_type: int
    ltp: float
    exchange_ts: float
    recv_ts: float
    sequence: int = 0
    day_volume: Optional[int] = None
    last_qty: Optional[int] = None
    atp: Optional[float] = None                  # exchange average traded price (day VWAP)
    total_buy_qty: Optional[float] = None
    total_sell_qty: Optional[float] = None
    oi: Optional[int] = None
    oi_change_pct_raw: Optional[float] = None    # as sent by the feed; scale unverified -> display only
    bids: list = field(default_factory=list)     # [(price, qty, orders)] best first
    asks: list = field(default_factory=list)
    day_open: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    prev_close: Optional[float] = None
    depth_state: str = "none"                    # none | ok | crossed | empty_side
    ts_source: str = "exchange"

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None


@dataclass
class Contract:
    token: str
    symbol: str
    kind: str                 # FUT | CE | PE
    expiry: str               # as in the master, e.g. 25NOV2026
    expiry_ts: float          # expiry 15:30 IST as UTC epoch
    strike: Optional[float]   # rupees; None for futures
    lot_size: int = 1
    exchange_type: int = 2

    def public(self) -> dict:
        return {"token": self.token, "symbol": self.symbol, "kind": self.kind, "expiry": self.expiry,
                "strike": self.strike, "lot_size": self.lot_size}


@dataclass
class InstBar:
    """Per-instrument, per-timeframe aggregate of derivative ticks. Closed bars are immutable."""
    token: str
    kind: str
    tf: str
    time: int
    open: float
    high: float
    low: float
    close: float
    ticks: int = 0
    volume: Optional[float] = None
    buy_vol: float = 0.0               # ESTIMATED
    sell_vol: float = 0.0              # ESTIMATED
    unclassified_vol: float = 0.0
    oi_open: Optional[float] = None
    oi_close: Optional[float] = None
    oi_updates: int = 0
    oi_invalid: int = 0
    atp: Optional[float] = None
    spread_sum: float = 0.0
    spread_n: int = 0
    depth_imb_sum: float = 0.0         # best-5 displayed imbalance
    depth_imb_n: int = 0
    tot_imb_sum: float = 0.0           # total buy qty vs total sell qty imbalance
    tot_imb_n: int = 0
    tbq_close: Optional[float] = None
    tsq_close: Optional[float] = None
    depth_invalid: int = 0
    cvd_close: Optional[float] = None  # session cumulative (ESTIMATED)
    iv_calc: Optional[float] = None    # CALCULATED, vol points (e.g. 14.2)
    iv_obs: Optional[float] = None     # OBSERVED from broker Greeks poll, if any
    delta: Optional[float] = None
    delta_src: Optional[str] = None
    underlying: Optional[float] = None  # futures close for options (CALCULATED reference)
    spot: Optional[float] = None
    first_ts: float = 0.0
    last_ts: float = 0.0
    last_recv: float = 0.0
    tick_rule_only: int = 0            # trades classified without a usable quote

    # -- derived (None = unavailable) ----------------------------------------------------
    @property
    def spread(self) -> Optional[float]:
        return self.spread_sum / self.spread_n if self.spread_n else None

    @property
    def depth_imb(self) -> Optional[float]:
        return self.depth_imb_sum / self.depth_imb_n if self.depth_imb_n else None

    @property
    def tot_imb(self) -> Optional[float]:
        return self.tot_imb_sum / self.tot_imb_n if self.tot_imb_n else None

    @property
    def iv(self) -> Optional[float]:
        return self.iv_obs if self.iv_obs is not None else self.iv_calc

    @property
    def classified_fraction(self) -> Optional[float]:
        if not self.volume:
            return None
        return min(1.0, (self.buy_vol + self.sell_vol) / self.volume)

    @property
    def imbalance(self) -> Optional[float]:
        t = self.buy_vol + self.sell_vol
        return (self.buy_vol - self.sell_vol) / t if t > 0 else None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "InstBar":
        return InstBar(**{k: v for k, v in d.items() if k in InstBar.__dataclass_fields__})


@dataclass
class Evidence:
    name: str
    value: str
    state: str = "neutral"        # supports | conflicts | neutral
    provenance: str = OBSERVED
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FlowSignal:
    id: str
    time: int                     # start of the (closed) observation bar the signal is attached to
    tf: str
    tf_seconds: int
    type: str                     # see SIGNAL_TYPES
    direction: int                # +1 bullish, -1 bearish, 0 neutral (used for validation)
    label: str                    # e.g. PROBABLE BUYING ACTIVITY
    strength: float
    strength_label: str
    data_quality: str             # Good | Fair | Poor
    underlying_price: Optional[float]
    instrument: str               # NIFTY | FUT | CE | PE
    token: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[str] = None
    premium: Optional[float] = None
    vol_change: Optional[float] = None
    oi_change: Optional[float] = None
    headline: str = ""
    evidence: list = field(default_factory=list)       # supporting + conflicting, with provenance
    components: dict = field(default_factory=dict)
    interpretation: str = ""
    details: dict = field(default_factory=dict)
    regime: str = "unknown"
    mode: str = "live"
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# signal type -> (display tag, group, direction)
SIGNAL_TYPES = {
    "AGG_BUY": ("▲", "aggressor", 1), "AGG_SELL": ("▼", "aggressor", -1),
    "CALL_BUY": ("CALL", "options", 1), "CALL_SELL": ("CALL", "options", -1),
    "PUT_BUY": ("PUT", "options", -1), "PUT_SELL": ("PUT", "options", 1),
    "LONG_BUILDUP": ("LONG", "futures", 1), "SHORT_BUILDUP": ("SHORT", "futures", -1),
    "SHORT_COVERING": ("SC", "futures", 1), "LONG_UNWINDING": ("LU", "futures", -1),
    "BUY_PRESSURE": ("BUY", "pressure", 1), "SELL_PRESSURE": ("SELL", "pressure", -1),
    "BUYER_ABSORPTION": ("ABS", "absorption", 1), "SELLER_ABSORPTION": ("ABS", "absorption", -1),
    "BULL_TRAP": ("BT", "trap", -1), "BEAR_TRAP": ("BR", "trap", 1),
}
