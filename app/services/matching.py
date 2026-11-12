from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from app.db.store import Store
from app.domain.orders import OrderStatus, match_amount
from app.payments.base import Transfer
from app.payments.ton import normalize_ton_address


async def process_transfer(
    store: Store,
    transfer: Transfer,
    *,
    receiving_ton_address: str,
    tolerance: Decimal,
    grace_seconds: int,
) -> tuple[str, dict[str, Any] | None]:
    now = transfer.timestamp
    if await store.transfer_exists(transfer.tx_hash):
        return "duplicate", None
    orders = await store.matchable_orders(transfer.asset, now, grace_seconds)
    candidate: dict[str, Any] | None = None
    if transfer.asset == "ton":
        try:
            if normalize_ton_address(transfer.destination) != normalize_ton_address(receiving_ton_address):
                await store.record_transfer(asdict(transfer), "unmatched_transfer", None)
                return "unmatched_transfer", None
        except (ValueError, TypeError):
            await store.record_transfer(asdict(transfer), "unmatched_transfer", None)
            return "unmatched_transfer", None
        candidate = next(
            (order for order in orders if order.get("ton_memo") == transfer.memo), None
        )
    elif transfer.asset == "usdt":
        exact = [
            order for order in orders
            if Decimal(order["expected_amount"]) == transfer.amount
        ]
        if len(exact) == 1:
            candidate = exact[0]
        elif not exact:
            near = [
                order for order in orders
                if abs(Decimal(order["expected_amount"]) - transfer.amount) <= tolerance
            ]
            if len(near) == 1:
                candidate = near[0]
    if not candidate:
        await store.record_transfer(asdict(transfer), "unmatched_transfer", None)
        return "unmatched_transfer", None
    expired = now > int(candidate["expires_at"])
    within_grace = now <= int(candidate["expires_at"]) + grace_seconds
    match = match_amount(
        expected=Decimal(candidate["expected_amount"]),
        received=transfer.amount,
        tolerance=tolerance,
        expired=expired,
        within_grace=within_grace,
    )
    if match.status == OrderStatus.UNMATCHED_TRANSFER:
        accepted = await store.record_transfer(asdict(transfer), match.status.value, None)
        if not accepted:
            return "duplicate", candidate
        return match.status.value, None
    details = {
        "tx_hash": transfer.tx_hash,
        "amount": str(transfer.amount),
        "expected": candidate["expected_amount"],
        "difference": str(match.difference),
        "sender": transfer.sender,
    }
    accepted = await store.apply_transfer(
        asdict(transfer), match.status.value, candidate["order_id"], match.status, details
    )
    if not accepted:
        return "duplicate", candidate
    return match.status.value, {**candidate, **details}
