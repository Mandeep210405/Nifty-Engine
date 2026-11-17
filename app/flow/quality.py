"""Data-quality rating. Missing/invalid inputs lower the rating or suppress the signal; nothing is invented."""
from __future__ import annotations

from app.flow.models import InstBar

LEVELS = ["Good", "Fair", "Poor"]


def rate(available_weight_pct: float, flags: list[str]) -> tuple[str, list[str]]:
    """available_weight_pct: share (0-100) of scoring weight with real data behind it."""
    lvl = 0 if available_weight_pct >= 85 else 1 if available_weight_pct >= 60 else 2
    reasons = []
    if available_weight_pct < 100:
        reasons.append(f"{100 - available_weight_pct:.0f}% of scoring weight has no data")
    major = [f for f in flags if f]
    if major:
        lvl = min(2, lvl + (1 if len(major) < 3 else 2))
        reasons.extend(major)
    return LEVELS[lvl], reasons


def bar_flags(bar: InstBar | None, sec: int, *, is_option: bool = False) -> list[str]:
    """Quality problems visible on one bar."""
    if bar is None:
        return ["no bar for this bucket"]
    f: list[str] = []
    if bar.ticks < 3 and not is_option:
        f.append(f"sparse ticks ({bar.ticks})")
    if bar.oi_invalid:
        f.append(f"{bar.oi_invalid} invalid OI tick(s) ignored")
    if bar.depth_invalid and bar.depth_invalid >= max(1, bar.ticks // 2):
        f.append("order-book frequently crossed/empty (depth ignored)")
    cf = bar.classified_fraction
    if bar.volume and cf is not None and cf < 0.6:
        f.append(f"only {cf * 100:.0f}% of volume classifiable to a side")
    if bar.volume and bar.ticks and bar.tick_rule_only >= 0.6 * max(1, bar.ticks):
        f.append("trade side mostly from tick rule (no usable quotes)")
    return f
