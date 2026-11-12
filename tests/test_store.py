import asyncio
import hashlib
import time
from decimal import Decimal

import pytest

from app.db.store import Store
from app.domain.orders import OrderStatus
from app.fulfillment.mock import MockProvider
from app.payments.base import Transfer
from app.services.matching import process_transfer

TON_ADDRESS = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"


def order_data(order_id: str) -> dict:
    return {
        "order_id": order_id, "user_id": 0, "product": "stars", "quantity": 50,
        "recipient": "test_user", "payment_asset": "usdt", "expected_amount": Decimal("1.00"),
        "provider_cost_usd": Decimal("0.80"), "price_usd": Decimal("1.00"),
        "expires_at": 4_000_000_000, "ton_memo": None,
        "idempotency_key": f"order:{order_id}", "max_amount_offset": Decimal("0.05"),
    }


@pytest.mark.asyncio
async def test_concurrent_usdt_amounts_are_unique_and_events_are_written(tmp_path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    await store.open()
    try:
        await asyncio.gather(
            store.create_order(order_data("A00001")),
            store.create_order(order_data("A00002")),
        )
        first = await store.get_order("A00001")
        second = await store.get_order("A00002")
        assert {first["expected_amount"], second["expected_amount"]} == {"1.000000", "1.000001"}
        async with store.lock:
            events = await (await store.conn.execute("SELECT COUNT(*) FROM order_events WHERE order_id IN ('A00001','A00002')")).fetchone()
        assert events[0] == 4
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_duplicate_transfer_is_not_applied_twice(tmp_path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    await store.open()
    try:
        data = order_data("B00001")
        data.update(payment_asset="ton", expected_amount=Decimal(1), ton_memo="SF-B00001")
        await store.create_order(data)
        transfer = Transfer(
            tx_hash="tx-unique-1", asset="ton", sender=TON_ADDRESS, destination=TON_ADDRESS,
            amount=Decimal(1), timestamp=1_800_000_000, memo="SF-B00001", raw={},
        )
        result, _ = await process_transfer(
            store, transfer, receiving_ton_address=TON_ADDRESS, tolerance=Decimal(0), grace_seconds=600
        )
        assert result == "paid"
        duplicate, _ = await process_transfer(
            store, transfer, receiving_ton_address=TON_ADDRESS, tolerance=Decimal(0), grace_seconds=600
        )
        assert duplicate == "duplicate"
        assert (await store.get_order("B00001"))["status"] == OrderStatus.PAID.value
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_payment_inside_grace_window_needs_manual_approval(tmp_path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    await store.open()
    try:
        now = int(time.time())
        data = order_data("B00002")
        data.update(
            payment_asset="ton", expected_amount=Decimal(1), ton_memo="SF-B00002",
            created_at=now - 100, expires_at=now - 10,
        )
        await store.create_order(data)
        transfer = Transfer(
            tx_hash="tx-late-1", asset="ton", sender=TON_ADDRESS, destination=TON_ADDRESS,
            amount=Decimal(1), timestamp=now - 5, memo="SF-B00002", raw={},
        )
        result, _ = await process_transfer(
            store, transfer, receiving_ton_address=TON_ADDRESS,
            tolerance=Decimal(0), grace_seconds=600,
        )
        assert result == "late_payment"
        assert (await store.get_order("B00002"))["status"] == OrderStatus.LATE_PAYMENT.value
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_tracking_code_claim_is_bound_to_one_telegram_user(tmp_path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    await store.open()
    try:
        token = "safe_tracking_code_123456789012"
        data = order_data("C00001")
        data["tracking_token_hash"] = hashlib.sha256(token.encode()).hexdigest()
        await store.create_order(data)
        claimed = await store.claim_order(
            "C00001", 123456789, hashlib.sha256(token.encode()).hexdigest()
        )
        assert claimed is not None
        assert claimed["user_id"] == 123456789
        assert len(await store.user_orders(123456789)) == 1
        stolen = await store.claim_order(
            "C00001", 987654321, hashlib.sha256(token.encode()).hexdigest()
        )
        assert stolen is None
        assert await store.user_orders(987654321) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mock_provider_delivery_is_idempotent() -> None:
    provider = MockProvider(Decimal("0.01"))
    first = await provider.deliver("test_user", "stars", 50, "order-idempotent")
    second = await provider.deliver("test_user", "stars", 50, "order-idempotent")
    assert first == second
