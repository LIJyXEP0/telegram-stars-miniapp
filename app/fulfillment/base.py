from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class FulfillmentError(RuntimeError):
    """A provider operation failed or returned an inconclusive result."""


@dataclass(frozen=True, slots=True)
class Quote:
    provider_cost_usd: Decimal
    provider_fee_usd: Decimal
    expires_at: int


@dataclass(frozen=True, slots=True)
class Delivery:
    request_id: str
    status: str
    result: str


class FulfillmentProvider(Protocol):
    async def check_recipient(self, username: str, product: str, quantity: int) -> bool: ...
    async def quote(self, product: str, quantity: int) -> Quote: ...
    async def deliver(
        self, username: str, product: str, quantity: int, idempotency_key: str
    ) -> Delivery: ...
    async def status(self, request_id: str) -> Delivery: ...
