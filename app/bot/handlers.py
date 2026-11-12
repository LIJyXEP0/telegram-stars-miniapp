from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from app.config import Settings
from app.db.store import Store
from app.domain.orders import OrderStatus


def build_router(settings: Settings, store: Store) -> Router:
    router = Router()

    async def is_admin(message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in settings.admin_ids_set)

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        if message.from_user:
            async with store.transaction() as conn:
                await conn.execute(
                    "INSERT INTO buyers(user_id,username,language,created_at) VALUES(?,?,?,strftime('%s','now')) "
                    "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                    (message.from_user.id, message.from_user.username,
                     "en" if (message.from_user.language_code or "ru").startswith("en") else "ru"),
                )
        keyboard = None
        if settings.mini_app_url:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="Мои заказы и поддержка",
                    web_app=WebAppInfo(url=settings.mini_app_url),
                )
            ]])
        await message.answer(
            "Здесь можно проверить сохранённые заказы и открыть поддержку. Mini App не принимает оплату.",
            reply_markup=keyboard,
        )

    @router.message(Command("support", "paysupport"))
    async def support(message: Message) -> None:
        support_name = settings.support_username.strip().lstrip("@")
        if support_name:
            await message.answer(f"Поддержка: @{support_name}. Приложи номер заказа к сообщению.")
        else:
            await message.answer("Напиши номер заказа и кратко опиши проблему. Контакт поддержки ещё не настроен.")

    @router.message(Command("orders"))
    async def user_orders(message: Message) -> None:
        if not message.from_user:
            return
        orders = await store.user_orders(message.from_user.id)
        if not orders:
            await message.answer("Пока нет привязанных заказов. Открой Mini App и привяжи заказ кодом отслеживания.")
            return
        lines = [
            f"<code>{item['order_id']}</code> · {item['product']} {item['quantity']} · {item['status']}"
            for item in orders
        ]
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("health"))
    async def health(message: Message) -> None:
        if not await is_admin(message):
            return
        size = settings.database_path.stat().st_size if settings.database_path.exists() else 0
        active = await store.active_orders()
        await message.answer(
            f"Health: OK\nDB: {size} bytes\nAwaiting orders: {len(active)}\n"
            f"Fulfillment mode: {settings.fulfillment_mode} / dry-run={settings.dry_run}"
        )

    @router.message(Command("pending"))
    async def pending(message: Message) -> None:
        if not await is_admin(message):
            return
        orders = await store.active_orders()
        await message.answer("\n".join(f"{o['order_id']} {o['payment_asset']} {o['expected_amount']} {o['recipient']}" for o in orders) or "No pending orders")

    @router.message(Command("failed"))
    async def failed(message: Message) -> None:
        if not await is_admin(message):
            return
        orders = await store.orders_in_status(OrderStatus.REFUND_PENDING)
        await message.answer("\n".join(
            f"{o['order_id']} {o['payment_asset']} {o['payment_amount']} tx={o['tx_hash']}"
            for o in orders
        ) or "No refunds pending")

    @router.message(Command("stuck"))
    async def stuck(message: Message) -> None:
        if not await is_admin(message):
            return
        orders = await store.orders_in_status(OrderStatus.NEEDS_MANUAL_REVIEW)
        late = await store.orders_in_status(OrderStatus.LATE_PAYMENT)
        combined = orders + late
        await message.answer("\n".join(
            f"{o['order_id']} {o['status']} {o['payment_asset']} {o['payment_amount']}"
            for o in combined
        ) or "No orders need review")

    @router.message(Command("approve_late"))
    async def approve_late(message: Message) -> None:
        if not await is_admin(message):
            return
        parts = (message.text or "").split(maxsplit=1)
        item = await store.get_order(parts[1].strip()) if len(parts) == 2 else None
        if not item or item["status"] != OrderStatus.LATE_PAYMENT.value:
            await message.answer("Укажи заказ со статусом late_payment: /approve_late <order_id>")
            return
        await store.transition(
            item["order_id"], OrderStatus.LATE_PAYMENT, OrderStatus.PAID,
            {"approved_by": message.from_user.id if message.from_user else None},
        )
        await message.answer(f"Заказ {item['order_id']} подтверждён и поставлен на выдачу.")

    @router.message(Command("order"))
    async def order(message: Message) -> None:
        if not await is_admin(message):
            return
        parts = (message.text or "").split(maxsplit=1)
        item = await store.get_order(parts[1].strip()) if len(parts) == 2 else None
        if item:
            await message.answer("\n".join(f"{key}: {value}" for key, value in item.items()))
        else:
            await message.answer("Order not found")

    return router
