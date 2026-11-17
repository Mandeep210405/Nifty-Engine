import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.broker.angel_client import AngelFeed, backoff_delay
from app.broker.history import parse_candle_rows
from app.broker.instrument_master import InstrumentMismatch, verify_instrument
from app.config import Instrument, load_settings, ConfigError, RedactingFilter
from app.market_data.aggregator import CandleAggregator
from app.market_data.normalizer import MalformedTick, normalize_tick, exchange_ts_to_seconds
from app.market_data.tickfilter import TickFilter, DUPLICATE, OUT_OF_ORDER, FUTURE, STALE
from app.models import Tick
from app.storage.repository import Repository, SRC_HIST, SRC_LIVE
from app.timeframes import TIMEFRAMES, bucket_start

# 2026-01-05 (Mon) 10:00:07 IST == 04:30:07 UTC
T0 = int(datetime(2026, 1, 5, 4, 30, 7, tzinfo=timezone.utc).timestamp())


def raw(price_paise=2400050, ts_ms=None, **kw):
    d = {"subscription_mode": 2, "exchange_type": 1, "token": "99926000", "sequence_number": 1,
         "exchange_timestamp": ts_ms if ts_ms is not None else T0 * 1000, "last_traded_price": price_paise}
    d.update(kw)
    return d


def tick(price, ts, vol=None, seq=0):
    return Tick("99926000", 1, price, ts, ts + 0.1, sequence=seq, day_volume=vol)


# ---- price / timestamp conversion --------------------------------------------------
def test_paise_to_rupees():
    t = normalize_tick(raw(2400050), T0 + 0.2)
    assert t.price == 24000.50

def test_currency_divisor():
    t = normalize_tick(raw(840000000, exchange_type=13), T0 + 0.2)
    assert t.price == pytest.approx(84.0)

def test_timestamp_ms_to_utc_seconds_and_latency():
    t = normalize_tick(raw(ts_ms=T0 * 1000 + 250), T0 + 1.0)
    assert t.exchange_ts == pytest.approx(T0 + 0.25)
    assert t.latency_ms == pytest.approx(750)
    ist = datetime.fromtimestamp(t.exchange_ts, timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    assert (ist.hour, ist.minute) == (10, 0)

def test_second_resolution_and_missing_timestamp():
    assert exchange_ts_to_seconds(T0) == T0
    t = normalize_tick(raw(ts_ms=0), T0 + 5)
    assert t.ts_source == "local" and t.exchange_ts == T0 + 5 and t.latency_ms is None

@pytest.mark.parametrize("bad", [{"last_traded_price": None}, {"last_traded_price": "abc"}, {"last_traded_price": 0},
                                 {"last_traded_price": float("nan")}, {"token": ""}, {"token": "123"}, {"exchange_type": "x"}])
def test_malformed(bad):
    with pytest.raises(MalformedTick):
        normalize_tick(raw(**bad), T0, expected_token="99926000")

def test_optional_fields_and_missing_volume():
    t = normalize_tick(raw(closed_price=2390000, volume_trade_for_the_day=0), T0)
    assert t.prev_close == 23900.0 and t.day_volume == 0
    assert normalize_tick(raw(), T0).day_volume is None


# ---- buckets -----------------------------------------------------------------------
def test_bucket_boundaries():
    assert bucket_start(T0, TIMEFRAMES["10s"]) == T0 - 7
    assert bucket_start(T0 + 2.9, TIMEFRAMES["10s"]) == T0 - 7
    assert bucket_start(T0 + 3.5, TIMEFRAMES["10s"]) == T0 + 3
    assert bucket_start(T0, TIMEFRAMES["30s"]) == T0 - 7
    assert bucket_start(T0, TIMEFRAMES["1m"]) == T0 - 7
    # 10:00:07 IST -> 5m bucket 10:00, 10m bucket 09:55 (anchored 09:15), 1h bucket 09:15
    ist = lambda ts: datetime.fromtimestamp(ts, timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).strftime("%H:%M:%S")
    assert ist(bucket_start(T0, TIMEFRAMES["5m"])) == "10:00:00"
    assert ist(bucket_start(T0, TIMEFRAMES["10m"])) == "09:55:00"
    assert ist(bucket_start(T0, TIMEFRAMES["15m"])) == "10:00:00"
    assert ist(bucket_start(T0, TIMEFRAMES["30m"])) == "09:45:00"
    assert ist(bucket_start(T0, TIMEFRAMES["1h"])) == "09:15:00"
    assert ist(bucket_start(T0, TIMEFRAMES["1d"])) == "00:00:00"


# ---- aggregation -------------------------------------------------------------------
def test_ohlc_update_vs_new_candle():
    agg = CandleAggregator(["10s", "1m"])
    base = T0 - 7
    ups = agg.add_tick(tick(100, base + 1))
    assert all(u.is_new for u in ups)
    for p, dt in [(105, 2), (98, 3), (102, 4)]:
        u = [x for x in agg.add_tick(tick(p, base + dt)) if x.timeframe == "10s"][0]
        assert not u.is_new
    c = agg.current("10s")
    assert (c.open, c.high, c.low, c.close, c.tick_count) == (100, 105, 98, 102, 4)
    ups = {u.timeframe: u for u in agg.add_tick(tick(110, base + 10.2))}
    assert ups["10s"].is_new and ups["10s"].closed.close == 102 and ups["10s"].candle.time == base + 10
    assert not ups["1m"].is_new and agg.current("1m").tick_count == 5
    assert agg.current("1m").high == 110

def test_candle_times_unique_ascending():
    agg = CandleAggregator(["10s"])
    times = []
    for i in range(50):
        for u in agg.add_tick(tick(100 + i, T0 + i * 1.7)):
            if u.is_new:
                times.append(u.candle.time)
    assert times == sorted(set(times))

def test_missing_volume_stays_none():
    agg = CandleAggregator(["1m"])
    agg.add_tick(tick(100, T0, vol=None)); agg.add_tick(tick(101, T0 + 1, vol=0))
    c = agg.current("1m")
    assert c.volume is None and c.tick_count == 2 and not agg.volume_seen

def test_volume_from_cumulative():
    agg = CandleAggregator(["1m"])
    for i, v in enumerate([1000, 1100, 1250]):
        agg.add_tick(tick(100, T0 + i, vol=v))
    assert agg.current("1m").volume == 250          # first obs is baseline only
    agg.add_tick(tick(100, T0 + 3, vol=40))         # counter reset
    assert agg.current("1m").volume == 290

def test_late_tick_ignored_by_aggregator():
    agg = CandleAggregator(["10s"])
    agg.add_tick(tick(100, T0 + 20))
    assert agg.add_tick(tick(90, T0 - 30)) == [] and agg.late_ticks_ignored == 1
    assert agg.current("10s").low == 100


# ---- tick filter -------------------------------------------------------------------
def test_duplicate_and_out_of_order_and_future_and_stale():
    f = TickFilter(stale_max_age_s=120)
    now = T0 + 0.1
    a = Tick("t", 1, 100, T0, now, sequence=5)
    assert f.check(a) is None
    assert f.check(Tick("t", 1, 100, T0, now + 1, sequence=5)) == DUPLICATE
    assert f.check(Tick("t", 1, 101, T0 - 2, now + 1, sequence=6)) == OUT_OF_ORDER
    assert f.check(Tick("t", 1, 101, T0, now + 1, sequence=7)) is None        # same ts, new data is fine
    assert f.check(Tick("t", 1, 101, T0 + 1000, now)) == FUTURE
    f2 = TickFilter(stale_max_age_s=120)
    assert f2.check(Tick("t", 1, 100, T0 - 3600, T0)) == STALE


# ---- storage / history shape -------------------------------------------------------
def test_repository_precedence_and_ordering():
    from app.models import Candle
    r = Repository(":memory:")
    r.upsert_candles("x", "1m", [Candle(120, 1, 2, 0.5, 1.5, None, 3), Candle(60, 1, 1, 1, 1)], SRC_LIVE)
    r.upsert_candles("x", "1m", [Candle(60, 9, 9, 9, 9, 5.0)], SRC_HIST)
    r.upsert_candles("x", "1m", [Candle(60, 1, 1, 1, 1)], SRC_LIVE)          # must NOT overwrite broker row
    rows = r.fetch_candles("x", "1m")
    assert [c.time for c in rows] == [60, 120] and rows[0].open == 9

def test_history_parse_and_drop_forming():
    tf = TIMEFRAMES["5m"]
    now = datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc).timestamp()
    rows = [["2026-01-05T09:15:00+05:30", 100, 101, 99, 100.5, 0], ["bad"], ["2026-01-05T10:30:00+05:30", 1, 2, 0.5, 1, 0]]
    out = parse_candle_rows(rows, tf, now)
    assert len(out) == 1 and out[0].volume is None and out[0].time == int(datetime(2026,1,5,3,45,tzinfo=timezone.utc).timestamp())

def test_instrument_master_verification():
    inst = Instrument()
    master = [{"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "exch_seg": "NSE"}]
    assert verify_instrument(inst, master)["symbol"] == "Nifty 50"
    with pytest.raises(InstrumentMismatch):
        verify_instrument(inst, [{"token": "99926000", "symbol": "Nifty Bank", "exch_seg": "NSE"}])
    with pytest.raises(InstrumentMismatch):
        verify_instrument(inst, [])


# ---- config / secrets --------------------------------------------------------------
def test_live_mode_requires_env(monkeypatch, tmp_path):
    for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FEED_MODE", "live")
    with pytest.raises(ConfigError) as e:
        load_settings(env_file=tmp_path / "none.env")
    assert "ANGEL_API_KEY" in str(e.value)

def test_redaction():
    f = RedactingFilter(["supersecret123"])
    assert "supersecret123" not in f.redact("token=supersecret123 x")


# ---- reconnect ---------------------------------------------------------------------
def test_backoff_bounded_exponential():
    ds = [backoff_delay(i, 2, 60, jitter=False) for i in range(8)]
    assert ds == [2, 4, 8, 16, 32, 60, 60, 60]

def test_ws_disconnect_triggers_reconnect(monkeypatch, tmp_path):
    """Supervisor re-logs in only when needed and reconnects after the socket drops."""
    monkeypatch.setenv("FEED_MODE", "mock")
    s = load_settings(env_file=tmp_path / "none.env")
    states = []
    feed = AngelFeed(s, lambda *a: None, lambda st, d: states.append(st), lambda *a: None, lambda *a: None)
    calls = {"login": 0, "ws": 0}

    def fake_login():
        calls["login"] += 1
        feed._smart = object(); feed._tokens = {"jwt": "x"}; feed._logged_in_at = time.time()
    def fake_ws():
        calls["ws"] += 1
        class W: last_error = "ConnectionResetError: boom"
        feed._ws = W()
        if calls["ws"] >= 3:
            feed._stop.set()
        return calls["ws"] == 1
    feed._login, feed._run_ws = fake_login, fake_ws
    feed._verify_instrument = lambda: None
    feed._backfill = lambda: None
    object.__setattr__(s, "backoff_base_s", 0.01)
    feed._run()
    assert calls == {"login": 1, "ws": 3} and states.count("reconnecting") >= 2 and states[-1] == "disconnected"


# ---- API ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os
    from fastapi.testclient import TestClient
    from app.main import create_app
    os.environ["FEED_MODE"] = "mock"; os.environ["DATA_DIR"] = str(tmp_path_factory.mktemp("d"))
    os.environ["MOCK_BACKFILL_MINUTES"] = "20"
    with TestClient(create_app(load_settings(env_file="/nonexistent"))) as c:
        time.sleep(1.0)
        yield c

def test_root_is_html(client):
    r = client.get("/")
    assert r.headers["content-type"].startswith("text/html") and "<title>NIFTY Live Engine" in r.text

def test_health_shape_no_secrets(client):
    j = client.get("/api/health").json()
    assert {"backend", "feed_state", "latest_tick_age_s", "instrument"} <= j.keys()
    assert "token" in j["instrument"] and "jwt" not in json.dumps(j).lower()

def test_history_shape_and_validation(client):
    j = client.get("/api/history?timeframe=10s").json()
    assert j["timeframe"] == "10s" and j["candles"] and j["mock"] is True
    c = j["candles"][0]
    assert {"time", "open", "high", "low", "close", "volume", "tick_count"} <= c.keys()
    times = [x["time"] for x in j["candles"]]
    assert times == sorted(set(times))
    assert client.get("/api/history?timeframe=7s").status_code == 422

def test_ws_snapshot_then_live(client):
    with client.websocket_connect("/ws/market?timeframe=10s") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        seen = {ws.receive_json()["type"] for _ in range(6)}
        assert "quote" in seen or "candle" in seen
        ws.send_text(json.dumps({"action": "set_timeframe", "timeframe": "1m"}))
        for _ in range(20):
            m = ws.receive_json()
            if m["type"] == "snapshot":
                assert m["timeframe"] == "1m"; break
        else:
            pytest.fail("no snapshot after timeframe switch")

def test_fixture_is_marked_mock():
    d = json.loads((Path(__file__).parent.parent / "fixtures" / "mock_ticks.json").read_text())
    assert "MOCK" in d["_MOCK_DATA"] and len(d["ticks"]) > 100
    agg = CandleAggregator(["10s"])
    for r in d["ticks"]:
        t = normalize_tick(r, r["exchange_timestamp"] / 1000 + 0.05)
        agg.add_tick(t)
    assert agg.current("10s").tick_count > 0
