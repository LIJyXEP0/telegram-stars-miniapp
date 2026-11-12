from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import aiohttp


class RateUnavailable(RuntimeError):
    pass


class TonUsdRates:
    def __init__(self, session: aiohttp.ClientSession, max_deviation: Decimal) -> None:
        self.session = session
        self.max_deviation = max_deviation
        self._cached: tuple[int, Decimal] | None = None
        self._previous: Decimal | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Decimal:
        now = int(time.time())
        if self._cached and now - self._cached[0] < 60:
            return self._cached[1]
        async with self._lock:
            now = int(time.time())
            if self._cached and now - self._cached[0] < 60:
                return self._cached[1]
            rates = await asyncio.gather(self._coingecko(), self._binance(), return_exceptions=True)
            values = [value for value in rates if isinstance(value, Decimal)]
            if len(values) != 2:
                raise RateUnavailable("Both independent TON/USD sources are required")
            low, high = min(values), max(values)
            deviation = (high - low) / ((high + low) / Decimal(2)) * Decimal(100)
            if deviation > self.max_deviation:
                raise RateUnavailable("TON/USD sources differ beyond configured sanity limit")
            current = (low + high) / Decimal(2)
            if self._previous:
                change = abs(current - self._previous) / self._previous * Decimal(100)
                if change > self.max_deviation:
                    raise RateUnavailable("TON/USD rate moved beyond configured sanity limit")
            self._previous = current
            self._cached = (now, current)
            return current

    async def _coingecko(self) -> Decimal:
        async with self.session.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "the-open-network", "vs_currencies": "usd"},
        ) as response:
            response.raise_for_status()
            payload = json.loads(await response.text(), parse_float=Decimal)
        return Decimal(payload["the-open-network"]["usd"])

    async def _binance(self) -> Decimal:
        async with self.session.get(
            "https://api.binance.com/api/v3/ticker/price", params={"symbol": "TONUSDT"}
        ) as response:
            response.raise_for_status()
            payload = json.loads(await response.text(), parse_float=Decimal)
        return Decimal(payload["price"])
