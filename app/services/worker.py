from __future__ import annotations

import html
import logging

from aiogram import Bot

from app.config import Settings
from app.db.store import Store
from app.domain.orders import OrderStatus
from app.fulfillment.base import FulfillmentError, FulfillmentProvider

log = logging.getLogger(__name__)


async def notify_owner(bot: Bot, settings: Settings, text: str) -> None:
    try:
        await bot.send_message(settings.owner_id, text[:3900], parse_mode="HTML")
    except Exception:
        log.exception("Owner notification failed")


async def fulfill_paid_orders(
    bot: Bot, settings: Settings, store: Store, provider: FulfillmentProvider
) -> None:
    for order in await store.orders_in_status(OrderStatus.PAID):
        try:
            await store.transition(
                order["order_id"], OrderStatus.PAID, OrderStatus.PROCESSING,
                {"idempotency_key": order["idempotency_key"]},
            )
            delivered = await provider.deliver(
                order["recipient"], order["product"], int(order["quantity"]),
                order["idempotency_key"],
            )
            await store.update_provider_request(order["order_id"], delivered.request_id)
            await store.update_provider_result(order["order_id"], delivered.result)
            if delivered.status == "completed":
                await store.transition(
                    order["order_id"], OrderStatus.PROCESSING, OrderStatus.COMPLETED,
                    {"provider_request_id": delivered.request_id},
                )
            elif delivered.status == "failed":
                await store.transition(
                    order["order_id"], OrderStatus.PROCESSING, OrderStatus.FAILED,
                    {"provider_request_id": delivered.request_id},
                )
                await store.transition(
                    order["order_id"], OrderStatus.FAILED, OrderStatus.REFUND_PENDING,
                    {"reason": "provider reported definitive failure"},
                )
                await notify_owner(
                    bot, settings,
                    f"⚠️ <b>Refund required</b>\nOrder <code>{html.escape(order['order_id'])}</code>\n"
                    f"Asset: {html.escape(order['payment_asset'])} · amount: <code>{html.escape(order['payment_amount'] or order['expected_amount'])}</code>\n"
                    f"Tx: <code>{html.escape(order['tx_hash'] or 'n/a')}</code>\n"
                    f"Sender: <code>{html.escape(order['tx_sender'] or 'n/a')}</code>\n"
                    f"Refund address: <code>{html.escape(order['refund_address'] or 'not provided')}</code>",
                )
            else:
                await store.transition(
                    order["order_id"], OrderStatus.PROCESSING, OrderStatus.NEEDS_MANUAL_REVIEW,
                    {"reason": "provider response was not conclusive"},
                )
                await notify_owner(bot, settings, f"Order {html.escape(order['order_id'])} requires manual provider review.")
        except FulfillmentError as exc:
            log.exception("Fulfillment requires manual review", extra={"order_id": order["order_id"]})
            current = await store.get_order(order["order_id"])
            if current and current["status"] == OrderStatus.PROCESSING.value:
                await store.transition(
                    order["order_id"], OrderStatus.PROCESSING, OrderStatus.NEEDS_MANUAL_REVIEW,
                    {"sanitized_error": type(exc).__name__},
                )
            await notify_owner(
                bot, settings,
                f"Order <code>{html.escape(order['order_id'])}</code> needs manual review. Error type: <code>{html.escape(type(exc).__name__)}</code>",
            )


async def reconcile_processing(
    bot: Bot, settings: Settings, store: Store, provider: FulfillmentProvider
) -> None:
    for order in await store.orders_in_status(OrderStatus.PROCESSING):
        if not order["provider_request_id"]:
            await store.transition(
                order["order_id"], OrderStatus.PROCESSING, OrderStatus.NEEDS_MANUAL_REVIEW,
                {"reason": "process stopped before provider request ID was persisted; automatic retry prohibited"},
            )
            await notify_owner(bot, settings, f"Order <code>{html.escape(order['order_id'])}</code> stopped during delivery and needs manual review.")
            continue
        try:
            status = await provider.status(order["provider_request_id"])
        except FulfillmentError as exc:
            await store.transition(
                order["order_id"], OrderStatus.PROCESSING, OrderStatus.NEEDS_MANUAL_REVIEW,
                {"reason": f"status inconclusive: {type(exc).__name__}"},
            )
            await notify_owner(bot, settings, f"Provider status for order <code>{html.escape(order['order_id'])}</code> is inconclusive; manual review required.")
            continue
        if status.status == "completed":
            await store.update_provider_result(order["order_id"], status.result)
            await store.transition(order["order_id"], OrderStatus.PROCESSING, OrderStatus.COMPLETED, {"reconciled": True})
        else:
            await store.transition(
                order["order_id"], OrderStatus.PROCESSING, OrderStatus.NEEDS_MANUAL_REVIEW,
                {"reason": "provider status is not conclusive"},
            )
            await notify_owner(bot, settings, f"Order <code>{html.escape(order['order_id'])}</code> needs manual review.")
