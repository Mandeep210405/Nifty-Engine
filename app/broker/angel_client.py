"""Angel One SmartAPI integration: login, WebSocket 2.0 market-data feed, historical backfill.

READ-ONLY: this module only authenticates, streams market data and reads historical candles.
No order / trade / portfolio endpoints are ever called.

Threading: the SDK's websocket client blocks, so the feed runs in one daemon thread and hands
raw packets to the asyncio engine through callbacks. Reconnect uses bounded exponential
backoff. Credentials, JWT and feed token live only in this object's memory.
"""
from __future__ import annotations

import logging
import random
import ssl
import threading
import time
from typing import Any, Callable, Optional

import certifi
import pyotp
import websocket

from app.broker.history import REQUEST_GAP_S, fetch_history
from app.broker.instrument_master import InstrumentMismatch, load_master, verify_instrument
from app.config import Settings
from app.flow.contracts import filter_master
from app.sessions import in_cash_session
from app.timeframes import TIMEFRAMES

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Login rejected (bad credentials/TOTP/app registration). Not retried automatically."""


def backoff_delay(attempt: int, base: float, cap: float, jitter: bool = True) -> float:
    d = min(cap, base * (2 ** max(0, attempt)))
    return d * random.uniform(0.8, 1.0) if jitter else d


def _make_ws_class():
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2

    class _Ws(SmartWebSocketV2):
        """SDK client with (a) verified TLS, (b) tolerant frame parsing, (c) no SDK-side retry
        (our supervisor loop owns reconnects), (d) error capture."""

        verify_tls = True
        last_error: str = ""
        malformed = 0

        def connect(self):
            headers = {"Authorization": self.auth_token, "x-api-key": self.api_key,
                       "x-client-code": self.client_code, "x-feed-token": self.feed_token}
            sslopt = ({"cert_reqs": ssl.CERT_REQUIRED, "ca_certs": certifi.where()}
                      if self.verify_tls else {"cert_reqs": ssl.CERT_NONE})
            self.wsapp = websocket.WebSocketApp(
                self.ROOT_URI, header=headers, on_open=self._h_open, on_error=self._h_error,
                on_close=self._h_close, on_data=self._on_data, on_ping=self._on_ping, on_pong=self._on_pong)
            self.wsapp.run_forever(sslopt=sslopt, ping_interval=self.HEART_BEAT_INTERVAL)

        def _h_open(self, wsapp, *a):
            self.on_open(wsapp)

        def _h_error(self, wsapp, error=None, *a):
            self.last_error = f"{type(error).__name__}: {str(error)[:200]}"
            self.close_connection()

        def _h_close(self, wsapp, *a):
            self.on_close(wsapp)

        def _on_data(self, wsapp, data, data_type=2, continue_flag=True):
            if data_type != 2:
                return
            try:
                parsed = self._parse_binary_data(data)
            except Exception:
                self.malformed += 1
                return
            self.on_data(wsapp, parsed)

    return _Ws


class AngelFeed:
    """Supervises login + websocket with reconnect. Callbacks are invoked from worker threads."""

    def __init__(self, settings: Settings,
                 on_tick: Callable[[dict, float], None],
                 on_state: Callable[[str, str], None],
                 on_history: Callable[[str, list, dict], None],
                 on_instrument: Callable[[bool, str], None],
                 on_master: Optional[Callable[[list], None]] = None,
                 on_greeks: Optional[Callable[[str, list], None]] = None,
                 get_option_expiry: Optional[Callable[[], Optional[str]]] = None):
        self.s = settings
        self._on_master, self._on_greeks, self._get_expiry = on_master, on_greeks, get_option_expiry
        self._extra: list[dict] = []          # NFO subscriptions requested by the flow layer
        self._ws_open = False
        self._greeks_thread: Optional[threading.Thread] = None
        self._sub_lock = threading.Lock()
        self._on_tick, self._on_state, self._on_history, self._on_instrument = on_tick, on_state, on_history, on_instrument
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._smart = None
        self._ws = None
        self._tokens: dict[str, str] = {}
        self._logged_in_at = 0.0
        self._needs_relogin = False
        self._verified = False
        self.reconnect_attempt = 0

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="angel-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_ws()
        if self._thread:
            self._thread.join(timeout=8)
        self._logout()

    def request_reconnect(self) -> None:
        """Drop the current socket; the supervisor loop reconnects with backoff."""
        self._close_ws()

    def _close_ws(self) -> None:
        ws = self._ws
        if ws is not None:
            try:
                ws.close_connection()
            except Exception:
                pass

    def _logout(self) -> None:
        smart, self._smart = self._smart, None
        if smart is not None and self._tokens:
            try:
                smart.terminateSession(self.s.client_code)
            except Exception:
                log.debug("logout failed (ignored)")
        self._tokens = {}

    # -- login -------------------------------------------------------------------------
    def _login(self) -> None:
        from SmartApi import SmartConnect
        kwargs: dict[str, Any] = {"api_key": self.s.api_key}
        if self.s.client_public_ip:
            kwargs["clientPublicIP"] = self.s.client_public_ip
        try:
            smart = SmartConnect(**kwargs)
            totp = pyotp.TOTP(self.s.totp_secret).now()
            resp = smart.generateSession(self.s.client_code, self.s.pin, totp)
        except (ValueError, TypeError) as e:        # e.g. malformed TOTP secret
            raise AuthError("Invalid TOTP secret format in .env") from e
        except Exception as e:
            name = type(e).__name__
            if name in {"TokenException", "PermissionException", "InputException"}:
                raise AuthError(f"Login rejected by Angel One ({name})") from e
            raise                                   # network-type error -> retry with backoff
        if not isinstance(resp, dict) or not resp.get("status") or not resp.get("data"):
            code = str((resp or {}).get("errorcode", "")) if isinstance(resp, dict) else ""
            msg = str((resp or {}).get("message", "login failed"))[:140] if isinstance(resp, dict) else "login failed"
            raise AuthError(f"Login failed ({code}): {msg}".replace("()", "").strip())
        d = resp["data"]
        self._tokens = {"jwt": d["jwtToken"], "feed": d["feedToken"], "refresh": d.get("refreshToken", "")}
        self._smart = smart
        self._logged_in_at = time.time()
        self._needs_relogin = False
        self._verified = False                # re-read the master after a fresh login (expiry rollover)
        log.info("Angel One login OK")

    # -- supervisor loop ---------------------------------------------------------------
    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            reason = ""
            try:
                self._on_state("connecting", "Logging in")
                if self._smart is None or self._needs_relogin or time.time() - self._logged_in_at > 20 * 3600:
                    self._logout()
                    self._login()
                    threading.Thread(target=self._backfill, name="angel-history", daemon=True).start()
                    if self._on_greeks and (self._greeks_thread is None or not self._greeks_thread.is_alive()):
                        self._greeks_thread = threading.Thread(target=self._greeks_loop, name="angel-greeks", daemon=True)
                        self._greeks_thread.start()
                self._verify_instrument()
                got_data = self._run_ws()
                if got_data:
                    attempt = 0
                reason = self._ws.last_error if self._ws and self._ws.last_error else "connection closed"
                if any(x in reason for x in ("401", "403", "Unauthorized", "Forbidden")):
                    self._needs_relogin = True
            except AuthError as e:
                self._on_state("auth_failed", str(e))
                log.error("Authentication failed: %s", e)
                return
            except InstrumentMismatch as e:
                self._on_state("error", str(e))
                log.error("Instrument verification failed: %s", e)
                return
            except Exception as e:
                reason = f"{type(e).__name__}"
                log.warning("Feed error: %s", reason)
            if self._stop.is_set():
                break
            delay = backoff_delay(attempt, self.s.backoff_base_s, self.s.backoff_max_s)
            attempt += 1
            self.reconnect_attempt = attempt
            self._on_state("reconnecting", f"{reason[:120]}; retry #{attempt} in {delay:.0f}s")
            self._stop.wait(delay)
        self._on_state("disconnected", "stopped")

    def _verify_instrument(self) -> None:
        if self._verified:
            return
        try:
            entries = load_master(self.s.data_dir)
        except Exception as e:
            self._on_instrument(False, f"Instrument master unavailable ({type(e).__name__}); using configured token unverified")
            log.warning("Could not verify instrument against master: %s", type(e).__name__)
            self._verified = True
            return
        if self._on_master:
            try:
                self._on_master(filter_master(entries, self.s.flow.underlying_name))
            except Exception:
                log.exception("flow master hand-off failed")
        e = verify_instrument(self.s.instrument, entries)
        self._on_instrument(True, f"Verified: {e.get('symbol')} / token {e.get('token')} / {e.get('exch_seg')}")
        self._verified = True

    def _run_ws(self) -> bool:
        Ws = _make_ws_class()
        Ws.input_request_dict = {}            # SDK keeps this at class level; reset per connection
        inst = self.s.instrument
        ws = Ws(self._tokens["jwt"], self.s.api_key, self.s.client_code, self._tokens["feed"],
                max_retry_attempt=0)
        ws.verify_tls = self.s.ws_verify_tls
        ws.last_error, ws.malformed = "", 0
        got = {"data": False}

        def on_open(_w):
            ws.subscribe("niftylive01", self.s.subscription_mode,
                         [{"exchangeType": inst.exchange_type, "tokens": [inst.token]}])
            with self._sub_lock:
                self._ws_open = True
                extra = [{"exchangeType": d["exchangeType"], "tokens": list(d["tokens"])} for d in self._extra]
            if extra:
                try:
                    ws.subscribe("niftyflow1", 3, extra)      # SNAP_QUOTE: depth + OI for NFO tokens
                except Exception as e:
                    log.warning("NFO subscribe failed: %s", type(e).__name__)
            self._on_state("subscribed", f"Subscribed mode {self.s.subscription_mode}")

        def on_data(_w, msg):
            got["data"] = True
            self._on_tick(msg, time.time())

        ws.on_open, ws.on_data = on_open, on_data
        ws.on_close = lambda _w: None
        ws.on_error = lambda *a: None
        self._ws = ws
        try:
            ws.connect()                      # blocks until closed
        finally:
            self._ws = ws
            with self._sub_lock:
                self._ws_open = False                     # keep for last_error read; cleared by next run
        return got["data"]

    # -- extra (NFO) subscriptions requested by the flow layer ------------------------------------------
    def add_subscriptions(self, token_list: list[dict]) -> None:
        """Remember tokens for every (re)connect and subscribe immediately if the socket is up."""
        with self._sub_lock:
            for d in token_list:
                for e in self._extra:
                    if e["exchangeType"] == d["exchangeType"]:
                        e["tokens"] = sorted(set(e["tokens"]) | set(d["tokens"]))
                        break
                else:
                    self._extra.append({"exchangeType": d["exchangeType"], "tokens": sorted(set(d["tokens"]))})
            live = self._ws if self._ws_open else None
        if live is not None:
            try:
                live.subscribe("niftyflow1", 3, [{"exchangeType": d["exchangeType"], "tokens": list(d["tokens"])} for d in token_list])
            except Exception as e:
                log.warning("NFO subscribe failed: %s", type(e).__name__)

    # -- broker Greeks (optional, one REST call per minute) -------------------------------------------
    def _greeks_loop(self) -> None:
        fails = 0
        while not self._stop.wait(60.0 * (1 + min(fails, 4))):
            smart, expiry = self._smart, (self._get_expiry() if self._get_expiry else None)
            if smart is None or not expiry or not in_cash_session(time.time()):
                continue
            try:
                resp = smart.optionGreek({"name": self.s.flow.underlying_name, "expirydate": expiry})
            except Exception as e:
                fails += 1
                log.debug("optionGreek failed: %s", type(e).__name__)
                continue
            if isinstance(resp, dict) and resp.get("status") and isinstance(resp.get("data"), list):
                fails = 0
                self._on_greeks(expiry, resp["data"])
            else:
                fails += 1

    # -- history backfill --------------------------------------------------------------
    def _backfill(self) -> None:
        smart = self._smart
        for tf in TIMEFRAMES.values():
            if self._stop.is_set() or smart is None:
                return
            candles, status = fetch_history(smart, self.s.instrument, tf, self.s.history_lookback_days)
            self._on_history(tf.key, candles, status)
            if tf.angel_interval:
                self._stop.wait(REQUEST_GAP_S)
