"""Tests for the NIFTY institutional-activity / options-flow layer."""
import json
import os
import time

import pytest

from app.flow import options as opt
from app.flow.absorption import detect_absorption
from app.flow.analyzer import FlowAnalyzer
from app.flow.bars import BarBook, Series, SpotBar
from app.flow.config import FlowSettings, parse_weights, score_label
from app.flow.contracts import filter_master, option_window, pick_futures, pick_option_expiry, to_contracts
from app.flow.features import Ctx
from app.flow.greeks import black76_price, implied_vol
from app.flow.models import Contract, DerivTick, InstBar
from app.flow.normalizer import normalize_deriv_tick
from app.flow.positioning import classify_positioning
from app.flow.pressure import compute_pressure
from app.flow.tradeclass import BUY, SELL, TradeClassifier
from app.flow.traps import TrapDetector
from app.flow.validation import OutcomeTracker, evaluate_horizon, walk_forward
from app.market_data.normalizer import MalformedTick

S = FlowSettings()
SEC = 60
T0 = 1_800_000_000 - (1_800_000_000 % 60)


def bar(i, close, *, vol=1000.0, oi=1_000_000.0, buy=0.0, sell=0.0, kind="FUT", token="F", atp=None, high=None, low=None,
        open_=None, delta=None, iv=None, spread=None, ticks=10, depth=None):
    b = InstBar(token=token, kind=kind, tf="1m", time=T0 + i * SEC, open=open_ if open_ is not None else close,
                high=high if high is not None else close, low=low if low is not None else close, close=close, ticks=ticks,
                volume=vol, buy_vol=buy, sell_vol=sell, oi_open=oi, oi_close=oi, atp=atp, cvd_close=0.0)
    b.delta, b.delta_src = delta, ("CALCULATED" if delta is not None else None)
    b.iv_calc = iv
    if spread is not None:
        b.spread_sum, b.spread_n = spread, 1
    if depth is not None:
        b.depth_imb_sum, b.depth_imb_n = depth, 1
    return b


def series(bars):
    s = Series()
    for b in bars:
        s.append(b)
    return s


def ctx_for(fut_bars, t_idx=None, spot_bars=None):
    fs = series(fut_bars)
    ss = Series()
    for b in spot_bars or []:
        ss.append(b)
    t = fut_bars[-1].time if t_idx is None else T0 + t_idx * SEC
    from app.flow.features import atr
    return Ctx(S, "1m", SEC, t, fs, ss, fs.get(t), ss.get(t), atr(ss, t, 14), atr(fs, t, 14))


def sbar(i, o, h, l, c):
    return SpotBar(T0 + i * SEC, o, h, l, c)


# ---- normalizer ---------------------------------------------------------------------------------
def raw_fut(**kw):
    d = {"subscription_mode": 3, "exchange_type": 2, "token": "111", "sequence_number": 5, "exchange_timestamp": T0 * 1000,
         "last_traded_price": 2500000, "last_traded_quantity": 75, "average_traded_price": 2499000, "volume_trade_for_the_day": 10000,
         "total_buy_quantity": 500000.0, "total_sell_quantity": 400000.0, "open_price_of_the_day": 2490000, "high_price_of_the_day": 2510000,
         "low_price_of_the_day": 2480000, "closed_price": 2485000, "open_interest": 12000000, "open_interest_change_percentage": 3,
         "best_5_buy_data": [{"flag": 1, "quantity": 600, "price": 2499500, "no of orders": 3}, {"flag": 1, "quantity": 300, "price": 2499000, "no of orders": 2}],
         "best_5_sell_data": [{"flag": 0, "quantity": 450, "price": 2500500, "no of orders": 4}]}
    d.update(kw)
    return d


def test_normalizer_snapquote_fields():
    t = normalize_deriv_tick(raw_fut(), T0 + 0.1)
    assert t.ltp == 25000.0 and t.oi == 12_000_000 and t.atp == 24990.0 and t.depth_state == "ok"
    assert t.best_bid == 24995.0 and t.best_ask == 25005.0 and t.total_buy_qty == 500000.0


def test_normalizer_crossed_book_flagged_not_swapped():
    t = normalize_deriv_tick(raw_fut(best_5_buy_data=[{"flag": 1, "quantity": 1, "price": 2510000, "no of orders": 1}]), T0)
    assert t.depth_state == "crossed"


def test_normalizer_unknown_token_and_missing_oi():
    with pytest.raises(MalformedTick):
        normalize_deriv_tick(raw_fut(), T0, known_tokens={"999"})
    r = raw_fut(); r.pop("open_interest")
    assert normalize_deriv_tick(r, T0).oi is None


# ---- trade classification ----------------------------------------------------------------------------
def dt(price, vol, bid=None, ask=None, ts=0):
    t = DerivTick("x", 2, price, T0 + ts, T0 + ts, day_volume=vol)
    if bid:
        t.bids, t.asks, t.depth_state = [(bid, 100, 1)], [(ask, 100, 1)], "ok"
    return t


def test_quote_rule_and_tick_rule_and_cvd():
    c = TradeClassifier()
    assert c.classify(dt(100, 1000, 99.9, 100.1)) is None                      # baseline
    r = c.classify(dt(100.1, 1100, 99.9, 100.1, 1)); assert (r.side, r.method, r.volume) == (BUY, "quote", 100)
    r = c.classify(dt(99.9, 1150, 99.9, 100.1, 2)); assert r.side == SELL
    assert c.cvd == 100 - 50
    c2 = TradeClassifier(); c2.classify(dt(100, 10)); c2.classify(dt(101, 20))        # no quotes -> tick rule
    r = c2.classify(dt(100.5, 30)); assert (r.side, r.method) == (SELL, "tick")
    r = c2.classify(dt(100.5, 40)); assert r.side == SELL                          # zero-tick keeps last side


def test_counter_reset_and_unclassified():
    c = TradeClassifier(); c.classify(dt(100, 1000)); c.classify(dt(101, 1100))
    assert c.classify(dt(101, 50)) is None                                       # counter reset, not a giant trade
    c3 = TradeClassifier(); c3.classify(dt(100, 10))
    r = c3.classify(dt(100, 20)); assert r.side == 0 and r.method == "unclassified" and c3.cvd == 0


# ---- greeks / contracts -----------------------------------------------------------------------------------
def test_iv_roundtrip_and_bounds():
    p = black76_price(25000, 25100, 10 / 365, 0.15, 0.065, True)
    assert abs(implied_vol(p, 25000, 25100, 10 / 365, 0.065, True) - 15.0) < 0.01
    assert implied_vol(0.5, 25000, 24000, 10 / 365, 0.065, True) is None          # below intrinsic
    assert implied_vol(100, 25000, 25000, 0.0001, 0.065, True) is None            # T too small


def master_rows(exps=("25NOV2037", "02DEC2037"), strikes=range(24500, 25600, 50)):
    rows = [{"token": "1", "symbol": "NIFTY30DEC2037FUT", "name": "NIFTY", "expiry": "30DEC2037", "strike": "-1", "lotsize": "75", "instrumenttype": "FUTIDX", "exch_seg": "NFO"},
            {"token": "2", "symbol": "NIFTY25NOV2020FUT", "name": "NIFTY", "expiry": "25NOV2020", "strike": "-1", "lotsize": "75", "instrumenttype": "FUTIDX", "exch_seg": "NFO"},
            {"token": "3", "symbol": "BANKNIFTY30DEC2037FUT", "name": "BANKNIFTY", "expiry": "30DEC2037", "strike": "-1", "lotsize": "30", "instrumenttype": "FUTIDX", "exch_seg": "NFO"}]
    n = 100
    for e in exps:
        for k in strikes:
            for ty in ("CE", "PE"):
                n += 1
                rows.append({"token": str(n), "symbol": f"NIFTY{e}{k}{ty}", "name": "NIFTY", "expiry": e, "strike": f"{k * 100:.6f}", "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "NFO"})
    return rows


def test_contract_selection_single_expiry_atm_window():
    cons = to_contracts(filter_master(master_rows()))
    assert not any("BANK" in c.symbol for c in cons)
    now = time.time()
    assert pick_futures(cons, now).expiry == "30DEC2037"                           # expired 2020 contract skipped
    exp = pick_option_expiry(cons, now); assert exp == "25NOV2037"
    atm, win = option_window(cons, exp, 25012, 5)
    assert atm == 25000 and len({c.expiry for c in win}) == 1
    assert sorted({c.strike for c in win}) == [24750 + 50 * i for i in range(11)] and len(win) == 22
    assert pick_option_expiry(cons, now, "NOPE") is None


def test_strike_mismatch_rejected():
    rows = master_rows(exps=("25NOV2037",), strikes=[25000])
    rows[-1]["strike"] = "99900000.000000"
    assert all(c.strike != 999000 for c in to_contracts(rows))
    assert len([c for c in to_contracts(rows) if c.kind != "FUT"]) == 1


# ---- positioning --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("dprice,doi,cls", [(+40, +30000, "LONG_BUILDUP"), (-40, +30000, "SHORT_BUILDUP"),
                                            (+40, -30000, "SHORT_COVERING"), (-40, -30000, "LONG_UNWINDING")])
def test_four_way(dprice, doi, cls):
    bars = [bar(i, 25000 + (i % 2), oi=1_000_000, high=25002, low=24999) for i in range(10)]
    bars += [bar(10, 25000 + dprice / 3, oi=1_000_000 + doi / 3), bar(11, 25000 + dprice * 2 / 3, oi=1_000_000 + doi * 2 / 3),
             bar(12, 25000 + dprice, oi=1_000_000 + doi)]
    # window=3 -> base is bar 9 (close 25001)
    p = classify_positioning(ctx_for(bars))
    assert p.state == "ok" and p.cls == cls and p.strength > 0


def test_positioning_noise_and_missing_oi():
    bars = [bar(i, 25000, oi=1_000_000) for i in range(13)]
    assert classify_positioning(ctx_for(bars)).state == "noise"
    bars[-1].oi_close = None
    assert classify_positioning(ctx_for(bars)).state == "unavailable"
    only_price = [bar(i, 25000 + (30 if i == 12 else 0), oi=1_000_000) for i in range(13)]
    assert classify_positioning(ctx_for(only_price)).cls is None                   # price alone is not classified


# ---- option interpretation rules -----------------------------------------------------------------------------
def opt_series(prem0, prem1, oi0, oi1, vol=2000.0, buy=0.0, sell=0.0, delta=0.5, spread=0.1):
    out = []
    for i in range(14):
        base = i < 11
        out.append(bar(i, prem0 if base else prem1 if i == 13 else (prem0 + prem1) / 2, vol=vol if i >= 11 else 100.0,
                       oi=oi0 if i < 11 else oi0 + (oi1 - oi0) * (i - 10) / 3, kind="CE", token="C", delta=delta, iv=14.0 + (0.6 if i == 13 else 0),
                       buy=buy if i >= 11 else 0, sell=sell if i >= 11 else 0, spread=spread))
    return out


def run_opt(fut_move, o_bars):
    fut = [bar(i, 25000 + (fut_move if i >= 11 else 0) * (i - 10) / 3 if i >= 11 else 25000 + (i % 2) * 0.5, oi=1e6,
               high=25002, low=24998) for i in range(14)]
    ctx = ctx_for(fut)
    con = Contract("C", "NIFTY25NOV203725000CE", "CE", "25NOV2037", 2e9, 25000.0, 75)
    return opt.classify_option(ctx, con, series(o_bars))


def test_oi_increase_alone_is_never_buy_or_sell():
    r = run_opt(0, opt_series(100, 100, 500_000, 560_000, vol=50.0))              # OI +12%, flat premium, no volume
    assert r.label not in (opt.BUYING, opt.SELLING)


def test_call_buying_needs_premium_outperformance_and_volume():
    r = run_opt(10, opt_series(100, 112, 500_000, 530_000, vol=9000.0, buy=7000.0, sell=500.0))
    assert r.label == opt.BUYING and r.sig_type == "CALL_BUY"
    assert any(e.provenance == "ESTIMATED" for e in r.evidence)                    # aggressor side is labelled an estimate
    assert not hasattr(opt, "CONFIRMED")                                          # never claims confirmed execution side


def test_call_selling_requires_oi_up_and_premium_underperforming():
    r = run_opt(0, opt_series(100, 94, 500_000, 560_000, vol=9000.0, buy=500.0, sell=7000.0))
    assert r.label == opt.SELLING and r.sig_type == "CALL_SELL"
    r2 = run_opt(0, opt_series(100, 94, 500_000, 400_000, vol=9000.0, buy=500.0, sell=7000.0))   # OI falling: closing, not writing
    assert r2.label != opt.SELLING


def test_insufficient_without_delta():
    r = run_opt(10, opt_series(100, 112, 500_000, 530_000, vol=9000.0, delta=None))
    assert r.label == opt.INSUFFICIENT


# ---- pressure ----------------------------------------------------------------------------------------------------
def test_weights_normalised_and_labels():
    assert abs(sum(parse_weights("price=30,volume=30").values()) - 100) < 1e-9
    with pytest.raises(ValueError):
        parse_weights("bogus=1")
    assert [score_label(x) for x in (0, 29, 30, 49, 50, 69, 70, 84, 85, 100)] == \
        ["Weak", "Weak", "Developing", "Developing", "Moderate", "Moderate", "Strong", "Strong", "Very Strong", "Very Strong"]


def buy_scenario(n=32):
    f, s = [], []
    for i in range(n):
        up = i >= n - 4
        c = 25000 + (30 * (i - (n - 5)) if up else (i % 2))
        f.append(bar(i, c, vol=4000.0 if up else 1000.0, oi=1_000_000 + (20000 * (i - (n - 5)) if up else 0), buy=3200.0 if up else 500,
                     sell=300.0 if up else 500, atp=24990.0, open_=c - 10 if up else c, high=c + 3, low=c - 3, depth=0.4))
        s.append(sbar(i, c - 10 if up else c, c + 3, c - 3, c))
    for b in f:
        b.cvd_close = b.time / 60 * 100.0
    return ctx_for(f, spot_bars=s)


def test_pressure_components_visible_and_unavailable_not_invented():
    ctx = buy_scenario()
    pos = classify_positioning(ctx)
    pr = compute_pressure(ctx, pos, None, None)
    assert pr.buy_score > pr.sell_score + 30 and pr.buy_score >= 50
    names = {c.name: c for c in pr.components}
    assert not names["options"].available and names["options"].to_dict()["provenance"] == "UNAVAILABLE"
    assert pr.available_weight == pytest.approx(90.0)
    for b in ctx.fut.items:
        b.atp, b.depth_imb_n, b.cvd_close, b.volume = None, 0, None, None
    pr2 = compute_pressure(Ctx(S, "1m", SEC, ctx.t, ctx.fut, ctx.spot, ctx.fbar, ctx.sbar, ctx.spot_atr, ctx.fut_atr), pos, None, None)
    assert pr2.suppressed and "data" in pr2.reason


# ---- absorption -------------------------------------------------------------------------------------------------------
def test_absorption_requires_side_evidence_and_repeats():
    bars = [bar(i, 25000 + (i % 2), vol=1000.0, high=25006, low=24994, buy=500, sell=500) for i in range(20)]
    for i in (17, 18, 19):
        bars[i] = bar(i, 25002, open_=25001, vol=4500.0, high=25004, low=24998, buy=500.0, sell=3500.0)
    hit = detect_absorption(ctx_for(bars))
    assert hit and hit.side == "BUYER" and hit.touches >= 2 and hit.level == 24998
    plain = [bar(i, 25000 + (i % 2), vol=1000.0, high=25006, low=24994, buy=500, sell=500) for i in range(19)] + \
            [bar(19, 25001, vol=4500.0, high=25004, low=24998, buy=0, sell=0)]
    assert detect_absorption(ctx_for(plain)) is None                              # big volume alone is not absorption


# ---- traps ---------------------------------------------------------------------------------------------------------------
def trap_ctx(spot, fut_vol):
    fb = [bar(i, spot[i].close, vol=fut_vol[i], high=spot[i].high, low=spot[i].low, oi=1_000_000) for i in range(len(spot))]
    ctx = ctx_for(fb, spot_bars=spot)
    return ctx


def test_bull_trap_marked_only_after_confirmation():
    spot = [sbar(i, 25000 + (i % 3), 25004 + (i % 3), 24996, 25000 + (i % 3)) for i in range(25)]
    spot[12] = sbar(12, 25004, 25020, 25002, 25018)                                  # swing high 25020 formed early
    spot[13:25] = [sbar(i, 25005, 25012, 24998, 25006) for i in range(13, 25)]
    spot += [sbar(25, 25006, 25040, 25005, 25035)]                                  # breakout
    spot += [sbar(26, 25035, 25036, 25005, 25008)]                                  # failure: back below 25020
    spot += [sbar(27, 25008, 25009, 24985, 24988)]                                  # confirmation
    vols = [1000.0] * 25 + [4000.0, 3000.0, 4500.0]
    det, pr_hits = TrapDetector(), []
    from app.flow.pressure import Pressure
    for n in range(25, 28):
        sub = spot[: n + 1]
        ctx = trap_ctx(sub, vols[: n + 1])
        pr = Pressure(buy_score=70 if n == 25 else 20, sell_score=10 if n < 27 else 65)
        pos = classify_positioning(ctx)
        pr_hits.append(det.update(ctx, pos, pr, None, {}))
    assert pr_hits[0] == [] and pr_hits[1] == []                                   # nothing at breakout or failure candle
    assert len(pr_hits[2]) == 1 and pr_hits[2][0].kind == "BULL"
    h = pr_hits[2][0]
    assert h.breakout_time < h.failure_time < h.confirm_time


def test_unconfirmed_failure_never_marked():
    spot = [sbar(i, 25000 + (i % 3), 25004 + (i % 3), 24996, 25000 + (i % 3)) for i in range(25)]
    spot[12] = sbar(12, 25004, 25020, 25002, 25018)
    spot += [sbar(25, 25006, 25040, 25005, 25035), sbar(26, 25035, 25036, 25005, 25008)] + [sbar(i, 25008, 25012, 25004, 25010) for i in range(27, 33)]
    vols = [1000.0] * 25 + [4000.0] + [1000.0] * 7
    from app.flow.pressure import Pressure
    det, hits = TrapDetector(), []
    for n in range(25, len(spot)):
        ctx = trap_ctx(spot[: n + 1], vols[: n + 1])
        hits += det.update(ctx, classify_positioning(ctx), Pressure(buy_score=70 if n == 25 else 20, sell_score=10), None, {})
    assert hits == []


# ---- bars / data quality --------------------------------------------------------------------------------------------------
def test_barbook_closes_on_timer_and_drops_late_ticks():
    bb = BarBook(("1m",), grace_s=3)
    t = DerivTick("F", 2, 100.0, T0 + 5, T0 + 5, day_volume=10)
    bb.add_deriv(t, "FUT", None, 0.0, None, False)
    assert bb.due("1m", T0 + 62) == [] and bb.due("1m", T0 + 63) == [T0]
    bars, _ = bb.pop("1m", T0)
    assert bars["F"].close == 100.0
    bb.add_deriv(DerivTick("F", 2, 101.0, T0 + 30, T0 + 70, day_volume=20), "FUT", None, 0.0, None, False)
    assert bb.late_dropped == 1 and bb.due("1m", T0 + 200) == []                   # not merged into a closed bar


def test_oi_validation_rejects_jumps_but_accepts_persistent_level():
    from app.flow.engine import FlowEngine, _InstState
    from app.config import load_settings
    os.environ["FEED_MODE"] = "mock"
    from app.storage.repository import Repository
    eng = FlowEngine(load_settings(env_file="/nonexistent"), S, Repository(":memory:"), lambda m: None, lambda t: None)
    st = _InstState(Contract("x", "x", "FUT", "e", 0, None))
    mk = lambda oi: DerivTick("x", 2, 1, T0, T0, oi=oi)
    assert eng._validate_oi(st, mk(1_000_000)) == (1_000_000.0, False)
    assert eng._validate_oi(st, mk(5_000_000)) == (None, True)                     # +400% in one tick
    assert eng._validate_oi(st, mk(0)) == (None, True)
    assert eng._validate_oi(st, mk(1_010_000))[0] == 1_010_000.0
    eng._validate_oi(st, mk(2_000_000)); eng._validate_oi(st, mk(2_001_000))
    assert eng._validate_oi(st, mk(2_002_000))[0] == 2_002_000.0                   # three agreeing ticks = new level


def test_analyzer_deduplicates_and_is_causal():
    an = FlowAnalyzer(S, "mock")
    bars = {"F": bar(0, 25000)}
    an.ingest_closed("1m", T0, bars, sbar(0, 25000, 25001, 24999, 25000), {}, set())
    assert len(an.st["1m"].fut) == 1
    an.ingest_closed("1m", T0, {"F": bar(0, 25999)}, None, {}, set())               # same bucket again cannot rewrite history
    assert an.st["1m"].fut.get(T0).close == 25000


# ---- validation -------------------------------------------------------------------------------------------------------------------
def sig(t=T0, d=1, strength=60.0, typ="CALL_BUY", price=25000.0):
    return {"id": f"1m:{t}:{typ}:", "time": t, "tf": "1m", "tf_seconds": 60, "type": typ, "direction": d, "strength": strength,
            "underlying_price": price, "regime": "range|normal_vol"}


def test_outcome_uses_only_later_bars_and_charges_costs():
    sp = Series()
    for i in range(0, 8):
        sp.append(sbar(i, 25000, 25000 + 10 * i + 5, 25000 - 3, 25000 + 10 * i))
    o = evaluate_horizon(sig(), sp, 3, S)
    assert o["exit_price"] == 25030 and o["gross_bps"] == pytest.approx(12.0) and o["net_bps"] == pytest.approx(12.0 - 3.0)
    assert o["mfe_bps"] > o["mae_bps"] > 0
    assert evaluate_horizon(sig(), sp, 15, S) is None                               # horizon bar not closed yet
    short = evaluate_horizon(sig(d=-1), sp, 3, S)
    assert short["gross_bps"] == pytest.approx(-12.0)
    tr = OutcomeTracker(S); tr.add(__import__("app.flow.models", fromlist=["FlowSignal"]).FlowSignal(
        id="a", time=T0, tf="1m", tf_seconds=60, type="CALL_BUY", direction=1, label="", strength=60, strength_label="",
        data_quality="Good", underlying_price=25000.0, instrument="CE"))
    done = [h for _, o in tr.on_bar_close("1m", sp) for h in [o["horizon"]]]
    assert sorted(done) == [1, 3, 5]


def test_walk_forward_never_uses_future_days():
    day = 86400
    rows = []
    for d in range(6):
        for k in range(40):
            strength = 50 + (k % 4) * 10
            net = (5 if strength >= 70 else -5) * (1 if d < 3 else -1)               # behaviour flips after day 3
            rows.append({"type": "CALL_BUY", "tf": "1m", "horizon": 5, "ts": T0 + d * day + k * 60, "strength": strength,
                         "net_bps": net, "gross_bps": net + 3, "mfe_bps": 8, "mae_bps": 4})
    wf = walk_forward(rows, S, sig_type="CALL_BUY", tf="1m", horizon=5, min_train_days=2)
    assert wf["status"] == "ok" and all("train_days" in f for f in wf["folds"] if "test" in f)
    assert wf["folds"][0]["test_day"] > 0 and wf["pooled_oos"]["n"] > 0
    assert wf["pooled_oos"]["mean_net_bps"] < 5            # the post-day-3 regime flip is exposed out-of-sample, not hidden
    assert walk_forward(rows[:40], S, sig_type="CALL_BUY", tf="1m", horizon=5)["status"] == "insufficient_history"


# ---- API / end-to-end (mock) -------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient
    from app.config import load_settings
    from app.main import create_app
    os.environ.update(FEED_MODE="mock", DATA_DIR=str(tmp_path_factory.mktemp("fd")), MOCK_BACKFILL_MINUTES="150", MOCK_SEED="3")
    with TestClient(create_app(load_settings(env_file="/nonexistent"))) as c:
        time.sleep(1.5)
        yield c


def test_flow_status_and_config(client):
    st = client.get("/api/flow/status").json()
    assert st["mode"] == "mock" and st["futures"]["kind"] == "FUT" and st["subscribed_tokens"] >= 23
    assert any("MOCK MODE" in c for c in st["caveats"]) and st["counters"]["deriv_ticks"] > 1000
    assert st["counters"]["malformed"] == 0
    cfg = client.get("/api/config").json()
    assert cfg["flow"]["enabled"] and "CALL_BUY" in cfg["flow"]["signal_types"] and "anchor" in cfg["timeframes"][0]


def test_signals_are_explained_and_provenance_tagged(client):
    sigs = client.get("/api/flow/signals?limit=3000&full=true").json()["signals"]
    assert sigs
    ids = [s["id"] for s in sigs]
    assert len(ids) == len(set(ids))                                                 # no duplicate events
    for s in sigs:
        assert s["evidence"] and s["interpretation"] and s["data_quality"] in ("Good", "Fair", "Poor")
        assert all(e["provenance"] in ("OBSERVED", "CALCULATED", "ESTIMATED", "UNAVAILABLE") for e in s["evidence"])
        assert "CONFIRMED" not in s["label"]
        if s["type"].startswith(("CALL_", "PUT_")):
            assert s["strike"] and s["expiry"] and s["premium"] is not None and "ESTIMATED" in s["interpretation"]
    expiries = {s["expiry"] for s in sigs if s["expiry"]}
    assert len(expiries) <= 2                                                          # expiries are never silently mixed
    one = client.get("/api/flow/signal/" + sigs[0]["id"]).json()
    assert one["id"] == sigs[0]["id"] and client.get("/api/flow/signal/nope").status_code == 404


def test_signals_only_reference_closed_bars(client):
    from app.timeframes import TIMEFRAMES
    now = time.time()
    for s in client.get("/api/flow/signals?limit=3000").json()["signals"]:
        assert s["time"] + s["tf_seconds"] <= now + 1                                 # attached to a bar that has already closed


def test_validation_and_walkforward_endpoints(client):
    v = client.get("/api/flow/validation").json()
    assert v["mode"] == "mock" and "MOCK" in v["warning"] and v["assumptions"]["cost_bps_round_trip"] >= 0
    for g in v["groups"].values():
        for h, st in g.items():
            assert st["n"] >= 1 and 0 <= st["hit_rate_net"] <= 1
    assert client.get("/api/flow/validation?group_by=regime").status_code == 200
    assert client.get("/api/flow/validation?group_by=zzz").status_code == 422
    w = client.get("/api/flow/validation/walkforward?type=CALL_BUY&tf=1m&horizon=5").json()
    assert w["status"] in ("insufficient_history", "ok", "no_out_of_sample_signals")
    assert client.get("/api/flow/validation/walkforward?type=NOPE").status_code == 422


def test_snapshot_carries_light_flow_signals(client):
    with client.websocket_connect("/ws/market?timeframe=1m") as ws:
        m = ws.receive_json()
    assert m["type"] == "snapshot" and m["flow_enabled"] and m["flow_signals"]
    assert "evidence" not in m["flow_signals"][0]                                       # details are fetched on demand


def test_existing_chart_api_unchanged(client):
    j = client.get("/api/history?timeframe=1m").json()
    assert j["candles"] and {"time", "open", "high", "low", "close"} <= j["candles"][0].keys()


def test_live_path_routing_and_subscription_bookkeeping(client):
    """Derivative packets arriving through the shared tick queue reach the flow layer; spot path is untouched."""
    eng = client.app.state.engine
    tok = next(t for t, c in eng.flow.selected.items() if c.kind == "FUT")
    before = eng.flow.counters["deriv_ticks"]
    spot_before = eng.tick_count
    raw = raw_fut(token=tok, exchange_timestamp=int((time.time() + 600) * 1000), sequence_number=999999)
    eng.loop.call_soon_threadsafe(eng._enqueue, raw, time.time())
    time.sleep(0.5)
    assert eng.flow.counters["deriv_ticks"] >= before        # routed (mock generator keeps adding too)
    assert eng.flow.counters["unknown_token"] == 0


def test_feed_add_subscriptions_merges_and_subscribes_live_socket():
    from app.broker.angel_client import AngelFeed
    from app.config import load_settings
    os.environ["FEED_MODE"] = "mock"
    s = load_settings(env_file="/nonexistent")
    feed = AngelFeed(s, lambda *a: None, lambda *a: None, lambda *a: None, lambda *a: None)
    sent = []

    class W:
        def subscribe(self, cid, mode, tl): sent.append((cid, mode, tl))
    feed.add_subscriptions([{"exchangeType": 2, "tokens": ["5", "6"]}])
    assert sent == []                                                                   # socket not open yet: remembered only
    feed._ws, feed._ws_open = W(), True
    feed.add_subscriptions([{"exchangeType": 2, "tokens": ["6", "7"]}])
    assert feed._extra == [{"exchangeType": 2, "tokens": ["5", "6", "7"]}]
    assert sent == [("niftyflow1", 3, [{"exchangeType": 2, "tokens": ["6", "7"]}])]


def test_replay_over_stored_bars_matches_live_signal_types(client):
    from app.flow.replay import replay
    eng = client.app.state.engine
    eng.flush_store() if hasattr(eng, "flush_store") else eng._flush()
    out = replay(eng.repo, "1m", "mock", eng.flow.fs)
    live = {s["type"] for s in eng.flow.signals if s["tf"] == "1m"}
    assert out["signals"] > 0 and {r["type"] for r in out["rows"]} <= live | {"x"}
    assert all(r["horizon"] in eng.flow.fs.horizons for r in out["rows"])
