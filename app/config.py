"""Central configuration. Everything is read from environment / .env once.

The instrument is defined ONLY here (and overridable via env) - nothing else in the
code base hardcodes NIFTY details.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from app.flow.config import FlowSettings, load_flow_settings

ROOT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"


class ConfigError(RuntimeError):
    """Raised for invalid/missing configuration. Messages never contain secret values."""


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None


@dataclass(frozen=True)
class Instrument:
    name: str = "NIFTY 50"            # display name
    symbol: str = "Nifty 50"          # symbol as it appears in the Angel One instrument master
    exchange: str = "NSE"             # exch_seg in the instrument master
    exchange_type: int = 1            # WebSocket 2.0 exchangeType (1 = NSE cash)
    token: str = "99926000"           # symboltoken (verified against the master at start-up)

    def public(self) -> dict:
        return {"name": self.name, "symbol": self.symbol, "exchange": self.exchange,
                "exchange_type": self.exchange_type, "token": self.token}


@dataclass(frozen=True)
class Settings:
    mode: str
    instrument: Instrument
    host: str
    port: int
    log_level: str
    data_dir: Path
    # broker credentials (server side only)
    api_key: str = field(default="", repr=False)
    client_code: str = field(default="", repr=False)
    pin: str = field(default="", repr=False)
    totp_secret: str = field(default="", repr=False)
    client_public_ip: str = field(default="", repr=False)
    # feed tuning
    subscription_mode: int = 2
    stale_after_s: float = 15.0
    reconnect_after_stale_s: float = 90.0
    backoff_base_s: float = 2.0
    backoff_max_s: float = 60.0
    stale_tick_max_age_s: float = 120.0
    candle_session_only: bool = True
    store_ticks: bool = True
    tick_retention_days: int = 3
    ws_verify_tls: bool = True
    snapshot_limit: int = 1500
    history_lookback_days: int = 5
    enabled_indicators: tuple[str, ...] = ()
    # mock
    mock_backfill_minutes: int = 180
    mock_with_volume: bool = True
    mock_tick_interval_s: float = 0.5
    mock_seed: int | None = None
    flow: FlowSettings = field(default_factory=FlowSettings)

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"

    @property
    def db_path(self) -> Path:
        # Mock data lives in its own DB so it can never be mistaken for real captured data.
        return self.data_dir / ("nifty_mock.db" if self.is_mock else "nifty_live.db")

    def secret_values(self) -> list[str]:
        return [s for s in (self.api_key, self.client_code, self.pin, self.totp_secret) if s and len(s) >= 4]


REQUIRED_LIVE_VARS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET")
_PLACEHOLDER_HINTS = ("your_", "_here", "changeme")


def load_settings(env_file: str | os.PathLike | None = None, *, override_env: bool = False) -> Settings:
    """Load settings. ``env_file`` defaults to ``<project>/.env`` (missing file is fine)."""
    load_dotenv(env_file or ROOT_DIR / ".env", override=override_env)

    mode = os.getenv("FEED_MODE", "mock").strip().lower()
    if mode not in {"mock", "live"}:
        raise ConfigError("FEED_MODE must be 'mock' or 'live'")

    if mode == "live":
        missing = [v for v in REQUIRED_LIVE_VARS if not os.getenv(v, "").strip()]
        if missing:
            raise ConfigError(
                "FEED_MODE=live but these variables are missing/empty in .env: "
                + ", ".join(missing) + ". Fill them in locally, or use FEED_MODE=mock for the demo."
            )
        placeholders = [v for v in REQUIRED_LIVE_VARS
                        if any(h in os.getenv(v, "").lower() for h in _PLACEHOLDER_HINTS)]
        if placeholders:
            raise ConfigError("These variables still contain .env.example placeholders: "
                              + ", ".join(placeholders))

    inst = Instrument(
        name=os.getenv("INSTRUMENT_NAME", "NIFTY 50"),
        symbol=os.getenv("INSTRUMENT_SYMBOL", "Nifty 50"),
        exchange=os.getenv("INSTRUMENT_EXCHANGE", "NSE"),
        exchange_type=_int("INSTRUMENT_EXCHANGE_TYPE", 1),
        token=os.getenv("INSTRUMENT_TOKEN", "99926000").strip(),
    )
    sub_mode = _int("SUBSCRIPTION_MODE", 2)
    if sub_mode not in (1, 2, 3):
        raise ConfigError("SUBSCRIPTION_MODE must be 1 (LTP), 2 (QUOTE) or 3 (SNAP_QUOTE)")

    seed_raw = os.getenv("MOCK_SEED", "").strip()
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = ROOT_DIR / data_dir

    return Settings(
        mode=mode,
        instrument=inst,
        host=os.getenv("HOST", "127.0.0.1"),
        port=_int("PORT", 8000),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        data_dir=data_dir,
        api_key=os.getenv("ANGEL_API_KEY", "").strip(),
        client_code=os.getenv("ANGEL_CLIENT_CODE", "").strip(),
        pin=os.getenv("ANGEL_PIN", "").strip(),
        totp_secret=os.getenv("ANGEL_TOTP_SECRET", "").strip().replace(" ", ""),
        client_public_ip=os.getenv("ANGEL_CLIENT_PUBLIC_IP", "").strip(),
        subscription_mode=sub_mode,
        stale_after_s=_float("STALE_AFTER_SECONDS", 15.0),
        reconnect_after_stale_s=_float("RECONNECT_AFTER_STALE_SECONDS", 90.0),
        backoff_max_s=_float("BACKOFF_MAX_SECONDS", 60.0),
        stale_tick_max_age_s=_float("STALE_TICK_MAX_AGE_SECONDS", 120.0),
        candle_session_only=_bool(os.getenv("CANDLE_SESSION_ONLY"), True),
        store_ticks=_bool(os.getenv("STORE_TICKS"), True),
        tick_retention_days=_int("TICK_RETENTION_DAYS", 3),
        ws_verify_tls=_bool(os.getenv("WS_VERIFY_TLS"), True),
        history_lookback_days=_int("HISTORY_LOOKBACK_DAYS", 5),
        enabled_indicators=tuple(x.strip().lower() for x in os.getenv("ENABLED_INDICATORS", "").split(",") if x.strip()),
        mock_backfill_minutes=_int("MOCK_BACKFILL_MINUTES", 180),
        mock_with_volume=_bool(os.getenv("MOCK_WITH_VOLUME"), True),
        mock_tick_interval_s=_float("MOCK_TICK_INTERVAL_SECONDS", 0.5),
        mock_seed=int(seed_raw) if seed_raw else None,
        flow=_flow(),
    )


def _flow() -> FlowSettings:
    try:
        return load_flow_settings()
    except ValueError as e:
        raise ConfigError(str(e)) from None


class RedactingFilter(logging.Filter):
    """Masks known secret values in any log record (defence in depth; we also never log them)."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for s in self._secrets:
            if s in text:
                text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            try:
                msg = record.getMessage()
            except Exception:
                return True
            red = self.redact(msg)
            if red != msg:
                record.msg, record.args = red, None
        return True


def setup_logging(settings: Settings) -> RedactingFilter:
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    flt = RedactingFilter(settings.secret_values())
    for h in logging.getLogger().handlers:
        h.addFilter(flt)
    logging.getLogger("websocket").setLevel(logging.WARNING)
    return flt
