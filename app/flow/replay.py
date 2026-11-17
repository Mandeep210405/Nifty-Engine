"""Offline replay: re-run the detectors over stored per-bar snapshots (flow_bars / flow_spot_bars).

    python -m app.flow.replay --db data/nifty_live.db --tf 1m [--mode live]

Uses the same FlowAnalyzer as live trading-hours analysis, feeding closed bars strictly in time order, so it is
causal (no look-ahead). Use it to see how a settings change would have behaved on data you captured, and then
judge it on days the settings were NOT tuned on (see walk-forward in validation.py). Prints a summary only; it does
not write to the database.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from app.flow.analyzer import FlowAnalyzer
from app.flow.bars import Series, SpotBar
from app.flow.config import load_flow_settings
from app.flow.models import KIND_CE, KIND_PE
from app.flow.store import FlowStore
from app.flow.validation import OutcomeTracker, summarize, walk_forward
from app.storage.repository import Repository


def replay(repo: Repository, tf: str, mode: str, fs=None) -> dict:
    fs = fs or load_flow_settings()
    store = FlowStore(repo)
    contracts = store.load_contracts()
    bars_by_t: dict[int, dict] = defaultdict(dict)
    for b in store.all_bars(tf):
        bars_by_t[b.time][b.token] = b
    spots = {t: SpotBar(t, o, h, l, c) for t, o, h, l, c in store.load_spot(tf)}
    an, tr = FlowAnalyzer(fs, mode), OutcomeTracker(fs)
    signals, outcomes = [], []
    for t in sorted(set(bars_by_t) | set(spots)):
        bars = bars_by_t.get(t, {})
        opts = [contracts[k] for k in bars if k in contracts and contracts[k].kind in (KIND_CE, KIND_PE)]
        exp = min((c.expiry_ts for c in opts), default=None)
        active = {c.token for c in opts if c.expiry_ts == exp}
        sigs = an.ingest_closed(tf, t, bars, spots.get(t), contracts, active, None, None, wall_now=t + 120)
        for g in sigs:
            tr.add(g)
            signals.append(g.to_dict())
        for sid, o in tr.on_bar_close(tf, an.st[tf].spot):
            outcomes.append((sid, o))
    by_id = {s["id"]: s for s in signals}
    rows = [{"id": sid, "mode": mode, "ts": by_id[sid]["time"], "tf": tf, "tf_seconds": by_id[sid]["tf_seconds"], "type": by_id[sid]["type"],
             "strength": by_id[sid]["strength"], "regime": by_id[sid]["regime"], "horizon": o["horizon"], "gross_bps": o["gross_bps"],
             "net_bps": o["net_bps"], "mfe_bps": o["mfe_bps"], "mae_bps": o["mae_bps"]} for sid, o in outcomes]
    counts: dict[tuple, dict] = {}
    for s in signals:
        c = counts.setdefault((s["type"], tf), {"type": s["type"], "tf": tf, "n": 0, "days": set()})
        c["n"] += 1
        c["days"].add((s["time"] + 19800) // 86400)
    cl = [{**c, "days": len(c["days"])} for c in counts.values()]
    return {"signals": len(signals), "summary": summarize(rows, cl, fs), "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--tf", default="1m")
    ap.add_argument("--mode", default="live")
    a = ap.parse_args()
    out = replay(Repository(a.db), a.tf, a.mode)
    print(json.dumps({"signals": out["signals"], "summary": out["summary"]}, indent=2))


if __name__ == "__main__":
    main()
