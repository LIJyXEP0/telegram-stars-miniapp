from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Transfer:
    tx_hash: str
    asset: str
    sender: str
    destination: str
    amount: Decimal
    timestamp: int
    memo: str | None
    raw: dict[str, Any]


class PaymentWatcher(Protocol):
    asset: str

    async def fetch(self, cursor: dict[str, Any]) -> tuple[list[Transfer], dict[str, Any]]:
        """Return confirmed incoming transfers and the cursor for the next poll."""
