"""Historical validation: what happened AFTER each signal, with costs, and honest out-of-sample reporting.

Rules enforced here:
* Entry reference = close of the signal bar (the earliest moment the signal could be known). No look-ahead.
* Outcomes use only bars AFTER the signal bar; they are written only once the horizon bar has closed.
* Costs and slippage are explicit assumptions (config) charged on every hypothetical trade.
* Weights are never fitted here. Walk-forward only selects a score THRESHOLD on training days and reports
  performance on later, unseen days. Results from MOCK data are meaningless and are labelled as such.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Optional

from app.flow.bars import Series
from app.flow.config import FlowSettings
from app.flow.models import FlowSignal


def evaluate_horizon(sig: dict, spot: Series, horizon: int, s: FlowSettings) -> Optional[dict]:
    """Outcome of one signal at one horizon, or None if the horizon bar has not closed / data is missing."""
    sec, t0, d = sig["tf_seconds"], sig["time"], sig["direction"]
    p0 = sig.get("underlying_price")
    if d == 0 or not p0:
        return None
    exit_bar = spot.get(t0 + horizon * sec)
    if exit_bar is None:
        return None
    path = [spot.get(t0 + k * sec) for k in range(1, horizon + 1)]
    path = [b for b in path if b is not None]
    if len(path) < max(1, horizon // 2):
        return None                                   # too many missing bars to trust MFE/MAE
    hi, lo = max(b.high for b in path), min(b.low for b in path)
    mfe = (hi - p0) if d > 0 else (p0 - lo)
    mae = (p0 - lo) if d > 0 else (hi - p0)
    gross = d * (exit_bar.close - p0) / p0 * 1e4
    cost = s.cost_bps_round_trip + 2.0 * s.slippage_bps_per_side
    return {"horizon": horizon, "ref_price": p0, "exit_price": exit_bar.close, "gross_bps": round(gross, 3),
            "net_bps": round(gross - cost, 3), "mfe_bps": round(max(mfe, 0) / p0 * 1e4, 3), "mae_bps": round(max(mae, 0) / p0 * 1e4, 3)}


class OutcomeTracker:
    """Holds signals awaiting outcomes per timeframe; call ``on_bar_close`` after each closed spot bar."""

    def __init__(self, settings: FlowSettings):
        self.s = settings
        self.pending: dict[str, list[dict]] = defaultdict(list)       # tf -> [{sig, todo:set}]

    def add(self, sig: FlowSignal) -> None:
        if sig.direction != 0 and sig.underlying_price:
            self.pending[sig.tf].append({"sig": sig.to_dict(), "todo": set(self.s.horizons)})

    def on_bar_close(self, tf: str, spot: Series) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        keep = []
        for item in self.pending[tf]:
            sig = item["sig"]
            for h in sorted(item["todo"]):
                if spot.get(sig["time"] + h * sig["tf_seconds"]) is None:
                    continue
                o = evaluate_horizon(sig, spot, h, self.s)
                item["todo"].discard(h)                              # evaluated (or permanently unusable)
                if o:
                    out.append((sig["id"], o))
            latest = spot.items[-1].time if len(spot) else 0
            expired = latest > sig["time"] + (max(self.s.horizons) + 3) * sig["tf_seconds"]
            if item["todo"] and not expired:
                keep.append(item)
        self.pending[tf] = keep
        return out


# ----------------------------------------------------------------------------------------------------
def _stats(rows: list[dict], min_n: int) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    g = [r["gross_bps"] for r in rows]
    nt = [r["net_bps"] for r in rows]
    mean_n = sum(nt) / n
    sd = math.sqrt(sum((x - mean_n) ** 2 for x in nt) / (n - 1)) if n > 1 else None
    ci = None
    if sd is not None and n >= min_n:
        half = 1.96 * sd / math.sqrt(n)
        ci = [round(mean_n - half, 3), round(mean_n + half, 3)]
    mfe = sum(r["mfe_bps"] for r in rows) / n
    mae = sum(r["mae_bps"] for r in rows) / n
    srt = sorted(nt)
    return {
        "n": n, "hit_rate_gross": round(sum(1 for x in g if x > 0) / n, 3),
        "hit_rate_net": round(sum(1 for x in nt if x > 0) / n, 3),
        "false_signal_rate": round(sum(1 for x in g if x <= 0) / n, 3),
        "mean_gross_bps": round(sum(g) / n, 3), "mean_net_bps": round(mean_n, 3),
        "median_net_bps": round(srt[n // 2], 3), "mean_mfe_bps": round(mfe, 3), "mean_mae_bps": round(mae, 3),
        "mfe_mae_ratio": round(mfe / mae, 2) if mae > 0 else None, "net_mean_ci95_bps": ci,
        "sufficient_sample": n >= min_n,
    }


def summarize(rows: list[dict], counts: list[dict], s: FlowSettings, group_by: str = "type") -> dict:
    """Aggregate outcome rows. group_by in {type, tf, regime}; always split by horizon."""
    if group_by not in ("type", "tf", "regime"):
        raise ValueError("group_by must be type, tf or regime")
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[(r[group_by], r["horizon"])].append(r)
    groups: dict[str, dict] = {}
    for (k, h), rs in sorted(buckets.items()):
        groups.setdefault(k, {})[str(h)] = _stats(rs, s.min_validation_samples)
    freq = []
    for c in counts:
        freq.append({"type": c["type"], "tf": c["tf"], "signals": c["n"], "sessions": c["days"],
                     "per_session": round(c["n"] / c["days"], 1) if c["days"] else None})
    return {"group_by": group_by, "groups": groups, "frequency": freq,
            "assumptions": {"entry": "close of the signal bar (reference: spot index)",
                            "cost_bps_round_trip": s.cost_bps_round_trip, "slippage_bps_per_side": s.slippage_bps_per_side,
                            "false_signal": "directional gross move <= 0 at the horizon",
                            "min_sample_for_ci": s.min_validation_samples}}


def walk_forward(rows: list[dict], s: FlowSettings, *, sig_type: str, tf: str, horizon: int,
                 thresholds: Iterable[float] = (0, 50, 60, 70, 80), min_train_days: int = 2) -> dict:
    """Expanding-window walk-forward over IST trading days.

    For each test day: pick the score threshold with the best mean NET return on strictly EARLIER days
    (requires min_validation_samples), then apply it to the test day. Only the pooled out-of-sample result
    should be quoted as performance. Signal weights are not touched.
    """
    sel = [r for r in rows if r["type"] == sig_type and r["tf"] == tf and r["horizon"] == horizon]
    day = lambda r: (r["ts"] + 19800) // 86400
    days = sorted({day(r) for r in sel})
    out = {"type": sig_type, "tf": tf, "horizon": horizon, "days": len(days), "folds": [], "pooled_oos": None}
    if len(days) < min_train_days + 1:
        out["status"] = "insufficient_history"
        out["message"] = f"need at least {min_train_days + 1} distinct trading days of signals with outcomes; have {len(days)}"
        return out
    pooled: list[dict] = []
    for i in range(min_train_days, len(days)):
        train = [r for r in sel if day(r) < days[i]]
        test = [r for r in sel if day(r) == days[i]]
        best, best_mean = None, -1e18
        for th in thresholds:
            sub = [r for r in train if r["strength"] >= th]
            if len(sub) < s.min_validation_samples:
                continue
            m = sum(r["net_bps"] for r in sub) / len(sub)
            if m > best_mean:
                best, best_mean = th, m
        if best is None:
            out["folds"].append({"test_day": days[i], "status": "skipped: too few training samples"})
            continue
        oos = [r for r in test if r["strength"] >= best]
        pooled.extend(oos)
        out["folds"].append({"test_day": days[i], "train_days": i, "chosen_min_strength": best,
                             "train_mean_net_bps": round(best_mean, 3), "test": _stats(oos, s.min_validation_samples)})
    out["status"] = "ok" if pooled else "no_out_of_sample_signals"
    out["pooled_oos"] = _stats(pooled, s.min_validation_samples) if pooled else None
    out["note"] = ("Only pooled_oos reflects unseen data. Train-day figures are in-sample and must not be quoted as performance.")
    return out
