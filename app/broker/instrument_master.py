"""Verify the configured instrument against Angel One's official instrument master."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable

import requests

from app.config import Instrument

log = logging.getLogger(__name__)

MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_MAX_AGE_S = 24 * 3600


class InstrumentMismatch(RuntimeError):
    pass


def verify_instrument(inst: Instrument, entries: Iterable[dict[str, Any]]) -> dict:
    """Return the matching master entry or raise InstrumentMismatch."""
    for e in entries:
        if str(e.get("token")) == inst.token and str(e.get("exch_seg", "")).upper() == inst.exchange.upper():
            sym = str(e.get("symbol", "")).strip().lower()
            name = str(e.get("name", "")).strip().lower()
            if inst.symbol.strip().lower() in (sym, name):
                return e
            raise InstrumentMismatch(
                f"Token {inst.token} on {inst.exchange} is '{e.get('symbol')}' in the instrument master, "
                f"not '{inst.symbol}'. Fix INSTRUMENT_SYMBOL/INSTRUMENT_TOKEN.")
    raise InstrumentMismatch(f"Token {inst.token} on {inst.exchange} was not found in the instrument master.")


def load_master(cache_dir: Path, timeout: float = 60) -> list[dict]:
    """Download (cached for 24h) the instrument master. Raises on network failure."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / "OpenAPIScripMaster.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_MAX_AGE_S:
        return json.loads(cache.read_text())
    log.info("Downloading Angel One instrument master")
    tmp = cache.with_suffix(".tmp")
    with requests.get(MASTER_URL, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    tmp.replace(cache)
    return json.loads(cache.read_text())
