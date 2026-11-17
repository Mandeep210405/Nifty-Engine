"""MOCK tick generator (DEMO ONLY - synthetic random walk, not market data).

Emits raw packets shaped exactly like SmartWebSocketV2 output (paise integers, ms timestamps)
so the real normaliser / filter / aggregator path is exercised.
"""
from __future__ import annotations

import random
import time
from typing import Iterator

MOCK_BASE_PRICE = 24000.0


class MockTickGenerator:
    def __init__(self, token: str, exchange_type: int, *, seed: int | None = None,
                 with_volume: bool = True, base_price: float = MOCK_BASE_PRICE):
        self.token, self.exchange_type = token, exchange_type
        self.rng = random.Random(seed)
        self.price = base_price * (1 + self.rng.uniform(-0.002, 0.002))
        self.prev_close = round(self.price * (1 + self.rng.uniform(-0.004, 0.004)), 2)
        self.with_volume = with_volume
        self.seq = 0
        self.volume = 0
        self.drift = 0.0          # used by the flow mock to create trending phases; 0 = original behaviour

    def next_raw(self, ts: float) -> dict:
        self.seq += 1
        self.price = max(1.0, self.price + self.drift + self.rng.gauss(0, 1.6))
        raw = {
            "subscription_mode": 2, "exchange_type": self.exchange_type, "token": self.token,
            "sequence_number": self.seq, "exchange_timestamp": int(ts * 1000),
            "last_traded_price": int(round(self.price * 100)),
            "closed_price": int(round(self.prev_close * 100)),
        }
        if self.with_volume:
            self.volume += self.rng.randint(50, 900)
            raw["volume_trade_for_the_day"] = self.volume
        else:
            raw["volume_trade_for_the_day"] = 0
        return raw

    def backfill(self, end_ts: float, minutes: int, step_s: float = 1.0) -> Iterator[dict]:
        t = end_ts - minutes * 60
        while t < end_ts:
            yield self.next_raw(t)
            t += step_s
