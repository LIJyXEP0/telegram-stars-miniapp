from __future__ import annotations

import asyncio
import hashlib
import time
from decimal import Decimal

from app.fulfillment.base import Delivery, FulfillmentError, Quote


class MockProvider:
    """Deterministic in-memory provider for dry-run and integration tests."""

    def __init__(self, unit_cost_usd: Decimal) -> None:
        self.unit_cost_usd = unit_cost_usd
        self._deliveries: dict[str, Delivery] = {}
        self._lock = asyncio.Lock()

    async def check_recipient(self, username: str, product: str, quantity: int) -> bool:
        return bool(username) and quantity > 0 and product in {"stars", "premium"}

    async def quote(self, product: str, quantity: int) -> Quote:
        if product not in {"stars", "premium"} or quantity <= 0:
            raise ValueError("Unsupported mock product or quantity")
        multiplier = Decimal(quantity) if product == "stars" else Decimal(quantity * 100)
        return Quote(self.unit_cost_usd * multiplier, Decimal(0), int(time.time()) + 60)

    async def deliver(
        self, username: str, product: str, quantity: int, idempotency_key: str
    ) -> Delivery:
        async with self._lock:
            if idempotency_key not in self._deliveries:
                request_id = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
                self._deliveries[idempotency_key] = Delivery(
                    request_id=request_id,
                    status="completed",
                    result=f"dry-run: {product} x{quantity} for @{username}",
                )
            return self._deliveries[idempotency_key]

    async def status(self, request_id: str) -> Delivery:
        for delivery in self._deliveries.values():
            if delivery.request_id == request_id:
                return delivery
        raise FulfillmentError("Mock delivery request not found")
