# NIFTY Live Engine

A local, **market-data and charting only** dashboard for NIFTY 50 using your own Angel One SmartAPI account
(FastAPI + SmartAPI WebSocket 2.0 + TradingView Lightweight Charts v5.0.9, vendored locally).
It never places orders or touches portfolio/trading endpoints. The optional **Flow layer** (see below) adds explainable market-activity markers;
it is an analysis aid, not a trade recommendation or prediction.

## Important honesty notes

* **Live mode was NOT exercised against Angel One in the environment this was built in** (outbound access to
  Angel One hosts was blocked there). Everything in live mode is built from the installed `smartapi-python 1.5.5`
  source and public documentation/forum posts. Mock mode, the pipeline, storage, API, UI and 67 automated tests were run.
  Expect to check the live path yourself on first run and read the Diagnostics panel.
* **10-second and 30-second candles are aggregated from live ticks** that this app receives while it is running.
  Angel One has no native sub-minute candles and no sub-minute history. Before first run / during downtime these
  timeframes are empty. Candle buckets are time groupings of ticks, nothing more.
* The feed is not claimed to be real-time. The app measures `receive time - exchange timestamp` per tick and shows it
  (live mode only). Latency can be negative/odd if your PC clock is off - sync it.
* **Volume:** the feed's cumulative day volume is used only if it is > 0. Index feeds usually report none, in which case the
  volume pane is empty and the UI says so. Tick count is stored separately and is never shown as volume.
* Historical per-request windows (1m=30d, 3m=60d, 5m/10m=100d, 15m/30m=200d, 1h=400d, 1d=2000d) are taken from SmartAPI
  forum/moderator posts, not an official table I could open. Default lookback is 5 days (`HISTORY_LOOKBACK_DAYS`).
  Whether history works for the NIFTY index on your account is reported per timeframe in the UI; if not, charting starts from live ticks.
* Prices are integer **paise** (divide by 100) and `exchange_timestamp` is epoch **milliseconds**, per the SDK parser and
  public tutorials. The code verifies nothing beyond that; the tests encode this assumption.
* Exchange holidays are not modelled. "Market closed" = outside Mon-Fri 09:15-15:30 IST, or no ticks.
* With `CANDLE_SESSION_ONLY=true` (default) pre-open ticks (before 09:15 IST) update the quote but are not put into candles,
  matching Angel's historical candles.
* The SDK quirks I found and how this project handles them:
  * `SmartApi` calls `api.ipify.org` at import time and then overwrites the public-IP header with a hardcoded value.
    Set `ANGEL_CLIENT_PUBLIC_IP` to your registered IP if your app registration requires one.
  * The SDK websocket disables TLS verification; this project overrides it to verify certificates (`WS_VERIFY_TLS=true`).
  * The SDK writes logs into `./logs/` (git-ignored). It does not log credentials.
* Static IP: Angel One's current rules for apps/APIs (including whether market-data-only apps need a registered static IP)
  can change and are account-specific. Check the official SmartAPI portal/docs. If login is rejected for IP reasons, fix the
  registration in the portal; this app does not and will not work around it.

## Quick start (macOS) - run each command separately

1. Create and activate a virtual environment
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. Install dependencies
   ```
   pip install -r requirements.txt
   ```
3. Copy the env template
   ```
   cp .env.example .env
   ```
4. Edit `.env` locally (leave `FEED_MODE=mock` first to try the demo; for live set `FEED_MODE=live` and fill the four `ANGEL_*` values).
   Never paste these values into chat or commit them.
5. Run the tests
   ```
   python -m pytest -q
   ```
6. Start the server (do not use `--reload`; it would open a second broker session. A lock file blocks duplicates anyway)
   ```
   python -m app.main
   ```
7. Open http://127.0.0.1:8000

**Windows (PowerShell):** `py -3.11 -m venv .venv`, `.venv\Scripts\Activate.ps1`, `copy .env.example .env`, then the same pip / pytest / `python -m app.main` commands.
Requires Python 3.11+.

## Angel One setup (live mode)

1. Create a SmartAPI app at the official SmartAPI portal (smartapi.angelone.in). Note the **API key**.
2. Enable TOTP on your Angel One account and keep the **TOTP secret** (the text key shown when you set up the authenticator).
3. Put `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET` in `.env`.
4. *Redirect URL / Postback URL* in the app registration are portal settings for OAuth/order callbacks. They are **not** this dashboard's URL,
   and this app doesn't use them. Follow the portal's guidance; don't put `http://127.0.0.1:8000` there.
5. Start the server. Flow: login with TOTP -> JWT + feed token held in memory only -> verify the instrument against the official instrument master
   -> subscribe WebSocket 2.0 (mode 2 QUOTE, exchangeType 1, token 99926000) -> background history backfill.
   Logout is called on clean shutdown.

## Architecture

```
Angel login -> WS 2.0 (thread) -> normalizer (paise, ms -> rupees, UTC) -> tick filter (dup / out-of-order / future / stale)
 -> aggregator (10 timeframes, tick buckets) -> SQLite (candles, ticks) + in-memory series -> FastAPI WS /ws/market -> Lightweight Charts
```
* `app/config.py` - all settings + the single instrument definition. `app/timeframes.py` - bucket maths (UTC storage, IST display).
* `app/broker/` - login, WS supervisor with bounded exponential backoff (2s..60s, jitter), instrument master check, history.
* `app/market_data/` - normalizer, tick filter, aggregator, mock generator. `app/storage/repository.py` - SQLite (`data/`).
* `app/engine.py` - orchestration, status state machine, broadcast. `app/api/routes.py` - HTTP + WS. `frontend/` - UI.
* `app/indicators/` - hook for your own indicators (subclass `Indicator`, register, set `ENABLED_INDICATORS=sma20` to see the example).
  Simple indicators describe price data only. Activity detection lives in the separate Flow layer (`app/flow/`).
* Candle alignment: 10s/30s on the clock; 1m-1h anchored to 09:15 IST (like Angel's candles); 1d on IST dates.
* Broker candles overwrite locally aggregated ones for closed buckets; live writes never overwrite broker rows.
* Mock mode uses `data/nifty_mock.db`, which is deleted at each start. Live data uses `data/nifty_live.db` and survives restarts.
* `fixtures/mock_ticks.json` is a labelled MOCK tick sample in the SDK's packet shape (used by tests).

### API
`GET /` dashboard - `GET /api/health` - `GET /api/status` - `GET /api/config` - `GET /api/history?timeframe=10s|30s|1m|3m|5m|10m|15m|30m|1h|1d&limit=` -
`WS /ws/market?timeframe=` (messages: `snapshot`, `candle`, `quote`, `status`, `indicator`, `history_status`, `error`; client sends `{"action":"set_timeframe","timeframe":"5m"}`).

### UI states
Connecting, Connected, Stale feed (no tick for `STALE_AFTER_SECONDS` in market hours; forced reconnect after `RECONNECT_AFTER_STALE_SECONDS`),
Reconnecting, Disconnected, Authentication failed (not retried automatically, to avoid account lock-outs), Market closed / no ticks,
Backend offline, and a **DEMO / MOCK DATA** badge in mock mode.

## Troubleshooting

* **Authentication failed**: check API key / client code / PIN; TOTP needs a correct PC clock (sync time) and the *secret*, not a 6-digit code.
  Fix `.env` and restart. The message shows only Angel's error code/text, never credentials.
* **Static IP / app registration errors** (e.g. IP not whitelisted): resolve in the SmartAPI portal; set `ANGEL_CLIENT_PUBLIC_IP` if instructed.
* **WebSocket won't connect / TLS errors**: corporate proxies can break verification; try another network. `WS_VERIFY_TLS=false` is available but not recommended.
  On macOS python.org builds run "Install Certificates.command". 401/403 triggers a fresh login on retry.
* **"Another instance already holds the broker session"**: stop the other process (live mode allows one per `DATA_DIR`).
* **No ticks outside market hours**: expected. The header shows the last known price tagged "(last known)"; stale snapshot ticks are not added to candles.
* **Chart blank**: open the browser console; check `/api/history?timeframe=1m`. Empty-state text explains missing history.
* **Historical data empty**: Angel may not serve index history to your account/range, or it's a holiday. Live ticks still build candles. No history is fabricated.
* **Instrument verification warning**: the master (margincalculator.angelbroking.com) couldn't be fetched, so the configured token is used unverified.
  A *mismatch* stops the feed with an error.

## Security
Credentials, JWT and feed token exist only server-side in memory, never sent to the browser and never written to disk by this app (the SDK writes its own
logs; a log filter also masks known secret values). `.env`, DBs and logs are git-ignored; `.env.example` contains placeholders only.

Chart library: TradingView Lightweight Charts (Apache-2.0), vendored in `frontend/vendor/` with its license; it shows a TradingView attribution logo as the license requires.


---

# Flow layer: NIFTY institutional-activity & options-flow indicator

A *market-activity detector* built on top of the existing chart. It does **not** predict, does **not** identify who is trading, and never
places orders. Every figure in the UI carries a provenance tag: **OBSERVED** (feed), **CALCULATED** (arithmetic on feed data),
**ESTIMATED** (model inference) or **UNAVAILABLE** (never replaced by an invented value).

Turn on with the default `FLOW_ENABLED=true`; try it first with `FEED_MODE=mock` (synthetic futures/options with hidden buy/sell/absorption phases).

## What it detects (chart markers, click for the full explanation)
| Marker | Meaning | Core evidence |
|---|---|---|
| ▲/▼ AGG | estimated aggressive buying/selling | futures volume spike + estimated trade-side imbalance + confirming candle |
| CALL / PUT (green=buying, red=selling) | probable buying/selling activity in a strike | premium vs delta-implied change, volume, OI, estimated side, IV, underlying |
| LONG / SHORT / SC / LU | futures long buildup, short buildup, short covering, long unwinding | futures price x OI over a window, noise-filtered |
| BUY / SELL (circle) | pressure score 0-100 with component breakdown | 8 weighted components, configurable |
| ABS (circle) | possible buyer/seller absorption | heavy volume + small range + opposing estimated flow + repeated level |
| BT / BR | possible bull / bear trap | breakout -> failure -> confirmation candle, only marked after confirmation |

Several events on one candle become one marker ("CALL +2"); click the candle for the event list. Labels hide when zoomed out.
Panel **Flow**: toggle each type, minimum strength, observation timeframe (1m/3m/5m), strike, expiry, minimum data quality.
Panel **Validation**: per-signal hit/false rate, net-of-cost returns, MFE/MAE by horizon, walk-forward out-of-sample results.
Marker positions: above the bar = bearish implication, below = bullish (a green PUT above a bar is put *buying*).

## Interpretation rules that are enforced in code
* **OI alone never makes a BUY or SELL.** BUY needs premium rising *and* outperforming `delta x futures move`, plus volume or estimated-aggressor support. SELL needs OI rising *and* premium underperforming. Everything else is `MIXED ACTIVITY` or `INSUFFICIENT DATA`.
* **Execution side is always ESTIMATED** (Lee-Ready style quote/tick rule on snapshot ticks). `CONFIRMED EXECUTION SIDE` is impossible with SmartAPI (no aggressor flag), so it is never emitted.
* Displayed depth is shown but low-weight and tagged: it can be cancelled.
* Missing components score 0 (not renormalised away) and are listed as UNAVAILABLE; below 60% available weight the pressure score is suppressed.
* Signals are created only from **closed** bars (timer close, 3 s grace), so markers never repaint and there is no look-ahead. Duplicate events are blocked by deterministic ids + cooldowns.
* Data-quality checks: stale/late/duplicate/out-of-order ticks, invalid or jumping OI, crossed/empty book, instrument-token and strike validation against the master, single option expiry, session gating, feed-outage baseline reset, contract roll reset.

## How it gets data (SmartAPI)
* Existing: spot via WebSocket 2.0 (mode 2). New: one extra **SNAP_QUOTE (mode 3)** subscription on exchangeType 2 (NFO) for the nearest NIFTY future + ATM +/- 5 strikes (CE and PE, one expiry), re-centred as spot moves (cap `max_subscribed_tokens`=60). Gives LTP, volume, OI, best-5 depth, total buy/sell qty, average traded price.
* Instrument master (already downloaded for verification) is filtered to NIFTY FUTIDX/OPTIDX for contract selection.
* Optional: `optionGreek` REST (1 call/min) for broker IV/delta (OBSERVED). If it fails or disagrees with our CALCULATED Black-76 IV by >2x it is ignored. Calculated Greeks are used otherwise.
* No other REST calls per tick; nothing blocks the chart (analysis runs once per closed bar).

## Known limits (read these)
* **Live NFO packets were not exercised against Angel One here.** The parser follows the installed SDK source (field names verified); open-interest-change % is sent as an integer of unverified scale and is not used. If the SDK's best-5 side labels were swapped the book would show as crossed and be ignored (visible in Flow > Data sources), never silently trusted.
* Snapshot ticks can merge several trades, so side/CVD estimates are coarse; quality flags show how much volume could be classified.
* NIFTY **spot has no volume**; volume, CVD, VWAP (exchange average traded price) and OI come from the **futures**. Spot VWAP is not available.
* PDH/PDL use stored 1d history; unavailable if history is empty. Holidays are not modelled.
* Warm-up: volume baselines need 8 bars, so early-session signals use fewer components (data quality drops accordingly). No history is fabricated for derivatives.
* Historical option/futures *bars* are stored from the moment you run the app (`flow_bars`), not backfilled.
* **All thresholds and weights are unvalidated starting points.** Score bands (Weak/Developing/Moderate/Strong/Very Strong) are display thresholds only.

## Validation workflow
Signals, component scores, quality, regime and (later) outcomes at 1/3/5/10/15 bars are stored in SQLite (`flow_signals`, `flow_outcomes`).
Outcome = direction-adjusted move from the signal bar's close, minus `FLOW_COST_BPS_ROUND_TRIP` and 2 x `FLOW_SLIPPAGE_BPS_PER_SIDE` (defaults 1 and 1 bps: **assumptions, set them from your broker's real charges**), with MFE/MAE, hit and false-signal rates, frequency per session, split by type / timeframe / regime (`/api/flow/validation?group_by=type|tf|regime`).
`/api/flow/validation/walkforward?type=CALL_BUY&tf=1m&horizon=5` picks a score threshold on earlier days only and reports pooled out-of-sample results. Weights are never fitted by the app; if you change weights, judge them on days you did not tune on.
`python -m app.flow.replay --db data/nifty_live.db --tf 1m` re-runs the detectors causally over stored bars. Use paper/observation mode for several weeks before trusting any signal type; mock-data statistics are meaningless.

## Flow API
`GET /api/flow/status` - `/api/flow/state?tf=` - `/api/flow/signals?tf=&since=&limit=&full=` - `/api/flow/signal/{id}` - `/api/flow/validation` - `/api/flow/validation/walkforward`.
WebSocket additions: `flow_signal`, `flow_state` messages; `snapshot` carries lightweight `flow_signals` and `flow_state`.

## Flow code map (`app/flow/`)
`normalizer` (SNAP_QUOTE) - `contracts` (master -> contracts/ATM window) - `greeks` (Black-76) - `tradeclass` (side estimate, CVD) - `bars` (timer-closed bars) -
`positioning`, `pressure`, `options`, `absorption`, `traps`, `regime`, `quality` (engines) - `analyzer` (orchestrator) - `validation`, `store`, `replay` - `engine` (live wiring) - `mock` (demo data).
