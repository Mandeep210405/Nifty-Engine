"""Flow-layer settings. All thresholds are configurable; none are validated claims.

Defaults are *starting points* that must be validated against your own captured history
(see app/flow/validation.py and /api/flow/validation).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

DEFAULT_WEIGHTS = {
    "price": 15.0,       # spot price movement over the observation window (vs ATR)
    "volume": 15.0,      # futures volume relative to its rolling average, on the bar's side
    "futures_oi": 15.0,  # four-way futures price/OI positioning
    "aggressive": 15.0,  # ESTIMATED aggressive buy/sell volume imbalance
    "cvd": 10.0,         # ESTIMATED cumulative volume delta trend
    "depth": 10.0,       # DISPLAYED order-book imbalance (orders can be cancelled - low weight)
    "vwap": 10.0,        # futures price vs exchange-reported average traded price
    "options": 10.0,     # CALL/PUT activity confirmation
}


def score_label(score: float) -> str:
    if score < 30:
        return "Weak"
    if score < 50:
        return "Developing"
    if score < 70:
        return "Moderate"
    if score < 85:
        return "Strong"
    return "Very Strong"


@dataclass(frozen=True)
class FlowSettings:
    enabled: bool = True
    timeframes: tuple[str, ...] = ("1m", "3m", "5m")    # observation timeframes
    strikes_each_side: int = 5
    recenter_strikes: int = 2
    max_subscribed_tokens: int = 60
    option_expiry: str = ""            # e.g. "25NOV2026"; empty = nearest not-yet-expired
    futures_expiry: str = ""           # empty = nearest not-yet-expired
    underlying_name: str = "NIFTY"
    flush_grace_s: float = 3.0         # wait this long after a bucket ends for late ticks
    # observation windows / baselines
    window_bars: int = 3               # positioning / option comparison window
    baseline_bars: int = 20
    min_baseline_bars: int = 8
    atr_bars: int = 14
    cvd_bars: int = 10
    # noise filters
    min_move_atr: float = 0.35
    min_move_bps: float = 1.0
    min_oi_change_pct: float = 0.05
    max_oi_jump_pct: float = 20.0      # a single-tick OI jump above this is treated as invalid
    min_classified_fraction: float = 0.6
    # pressure scoring
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    price_full_atr: float = 1.5
    volume_full_ratio: float = 3.0
    aggr_full_imbalance: float = 0.4
    cvd_full_slope: float = 0.25
    depth_full_imbalance: float = 0.3
    vwap_full_atr: float = 1.0
    min_available_weight: float = 60.0
    pressure_emit_min: float = 50.0
    pressure_margin: float = 15.0
    aggr_emit_imbalance: float = 0.35
    aggr_emit_rel_volume: float = 1.5
    # futures positioning emission
    positioning_emit_min: float = 30.0
    # options
    opt_min_window_lots: float = 10.0
    opt_min_oi_lots: float = 5.0
    opt_min_oi_change_pct: float = 0.3
    opt_emit_min: float = 55.0
    opt_dominance_margin: float = 20.0
    opt_iv_step: float = 0.3           # vol points
    opt_max_quote_age_s: float = 20.0
    opt_max_per_type_per_bar: int = 2
    risk_free_rate: float = 0.065
    # absorption
    abs_min_rel_volume: float = 2.0
    abs_max_range_atr: float = 0.7
    abs_min_imbalance: float = 0.2
    abs_touch_tol_atr: float = 0.25
    abs_lookback_bars: int = 6
    # traps
    trap_swing_bars: int = 20
    trap_min_level_age: int = 3
    trap_buffer_atr: float = 0.1
    trap_fail_bars: int = 3
    trap_confirm_bars: int = 3
    trap_activity_score: float = 40.0
    trap_activity_rel_volume: float = 1.3
    # de-duplication
    cooldown_bars: int = 3
    # validation / costs (ASSUMPTIONS - set from your own broker schedule)
    horizons: tuple[int, ...] = (1, 3, 5, 10, 15)
    cost_bps_round_trip: float = 1.0
    slippage_bps_per_side: float = 1.0
    min_validation_samples: int = 30
    # storage
    store_bars: bool = True
    max_signal_snapshot: int = 1500

    def weight_total(self) -> float:
        return sum(self.weights.values())


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_num(name: str, default, cast):
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return cast(v)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None


def parse_weights(raw: str) -> dict:
    out = dict(DEFAULT_WEIGHTS)
    for part in raw.split(","):
        if not part.strip():
            continue
        k, _, v = part.partition("=")
        k = k.strip().lower()
        if k not in DEFAULT_WEIGHTS:
            raise ValueError(f"Unknown FLOW_WEIGHTS key '{k}'. Allowed: {', '.join(DEFAULT_WEIGHTS)}")
        out[k] = float(v)
    total = sum(out.values())
    if total <= 0:
        raise ValueError("FLOW_WEIGHTS must sum to a positive number")
    return {k: v * 100.0 / total for k, v in out.items()}        # always normalised to 100


def load_flow_settings() -> FlowSettings:
    s = FlowSettings()
    tfs = tuple(x.strip().lower() for x in os.getenv("FLOW_TIMEFRAMES", ",".join(s.timeframes)).split(",") if x.strip())
    from app.timeframes import TIMEFRAMES
    bad = [t for t in tfs if t not in TIMEFRAMES or TIMEFRAMES[t].seconds < 60 or TIMEFRAMES[t].seconds > 3600]
    if bad:
        raise ValueError(f"FLOW_TIMEFRAMES must be among 1m..1h: bad {bad}")
    w = os.getenv("FLOW_WEIGHTS", "").strip()
    return replace(
        s,
        enabled=_env_bool("FLOW_ENABLED", s.enabled),
        timeframes=tfs or s.timeframes,
        strikes_each_side=_env_num("FLOW_STRIKES_EACH_SIDE", s.strikes_each_side, int),
        option_expiry=os.getenv("FLOW_OPTION_EXPIRY", "").strip().upper(),
        futures_expiry=os.getenv("FLOW_FUTURES_EXPIRY", "").strip().upper(),
        window_bars=_env_num("FLOW_WINDOW_BARS", s.window_bars, int),
        pressure_emit_min=_env_num("FLOW_PRESSURE_EMIT_MIN", s.pressure_emit_min, float),
        cost_bps_round_trip=_env_num("FLOW_COST_BPS_ROUND_TRIP", s.cost_bps_round_trip, float),
        slippage_bps_per_side=_env_num("FLOW_SLIPPAGE_BPS_PER_SIDE", s.slippage_bps_per_side, float),
        store_bars=_env_bool("FLOW_STORE_BARS", s.store_bars),
        weights=parse_weights(w) if w else dict(DEFAULT_WEIGHTS),
    )
