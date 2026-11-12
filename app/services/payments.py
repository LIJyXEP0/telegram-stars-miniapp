from __future__ import annotations

import html
import logging
import time
from decimal import Decimal

from aiogram import Bot

from app.config import Settings
from app.db.store import Store
from app.domain.orders import OrderStatus
from app.payments.base import PaymentWatcher
from app.services.matching import process_transfer

log = logging.getLogger(__name__)


async def watch_payments(
    bot: Bot,
    settings: Settings,
    store: Store,
    watcher: PaymentWatcher,
    *,
    ton_receive_address: str,
    tolerance: Decimal,
) -> None:
    """Poll one chain, persist all transfers idempotently, then checkpoint its cursor."""
    cursor = await store.get_cursor(watcher.asset)
    transfers, next_cursor = await watcher.fetch(cursor)
    for transfer in transfers:
        classification, order = await process_transfer(
            store,
            transfer,
            receiving_ton_address=ton_receive_address,
            tolerance=tolerance,
            grace_seconds=settings.payment_grace_seconds,
        )
        if classification in {"duplicate", "paid"} and not order:
            continue
        if classification == "paid" and order:
            text = (
                f"✅ <b>Payment matched</b>\nOrder <code>{html.escape(order['order_id'])}</code>\n"
                f"{html.escape(transfer.asset.upper())} {html.escape(str(transfer.amount))}\n"
                f"Tx: <code>{html.escape(transfer.tx_hash)}</code>"
            )
        elif classification == "late_payment" and order:
            text = (
                f"⏳ <b>Late payment needs approval</b>\nOrder <code>{html.escape(order['order_id'])}</code>\n"
                f"Amount: {html.escape(str(transfer.amount))} {html.escape(transfer.asset.upper())}\n"
                f"Tx: <code>{html.escape(transfer.tx_hash)}</code>"
            )
        elif classification in {"underpaid", "overpaid"} and order:
            text = (
                f"⚠️ <b>{html.escape(classification.replace('_', ' ').title())}</b>\n"
                f"Order <code>{html.escape(order['order_id'])}</code>\n"
                f"Received {html.escape(str(transfer.amount))}; expected {html.escape(str(order['expected_amount']))} "
                f"{html.escape(transfer.asset.upper())}\nTx: <code>{html.escape(transfer.tx_hash)}</code>"
            )
        elif classification == "unmatched_transfer":
            text = (
                f"❓ <b>Unmatched incoming transfer ({html.escape(transfer.asset.upper())})</b>\n"
                f"Amount: {html.escape(str(transfer.amount))}\nFrom: <code>{html.escape(transfer.sender)}</code>\n"
                f"Tx: <code>{html.escape(transfer.tx_hash)}</code>"
            )
        else:
            continue
        try:
            await bot.send_message(settings.owner_id, text, parse_mode="HTML")
        except Exception:
            log.exception("Payment alert delivery failed", extra={"order_id": order.get("order_id", "-") if order else "-"})
    await store.save_cursor(watcher.asset, next_cursor)


async def expire_orders(bot: Bot, settings: Settings, store: Store) -> None:
    """Expire unpaid orders; the watcher still accepts payments inside the grace window."""
    for order in await store.active_orders():
        if int(order["expires_at"]) >= time.time():
            continue
        try:
            await store.transition(
                order["order_id"], OrderStatus.AWAITING_PAYMENT, OrderStatus.EXPIRED,
                {"expiry_worker": True},
            )
        except Exception:
            current = await store.get_order(order["order_id"])
            if not current or current["status"] != OrderStatus.EXPIRED.value:
                raise
        try:
            await bot.send_message(
                settings.owner_id,
                f"⌛ Order <code>{html.escape(order['order_id'])}</code> expired without a matched payment.",
                parse_mode="HTML",
            )
        except Exception:
            log.exception("Expiry alert delivery failed", extra={"order_id": order["order_id"]})
