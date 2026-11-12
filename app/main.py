from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path

import aiohttp
import yaml
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiohttp import web
from pydantic import ValidationError

from app.bot.handlers import build_router
from app.config import Settings
from app.db.store import Store
from app.fulfillment.mock import MockProvider
from app.payments.ton import TonCenterWatcher
from app.payments.tron import TronGridWatcher
from app.services.payments import expire_orders, watch_payments
from app.services.worker import fulfill_paid_orders, reconcile_processing
from app.web import build_site


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        rendered = re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b", "[BOT_TOKEN]", rendered)
        rendered = re.sub(r"(?i)(seed|mnemonic|api[_ -]?key|token)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", rendered)
        words = rendered.split()
        if 12 <= len(words) <= 24 and all(re.fullmatch(r"[a-zA-Z]+", word.strip(",.")) for word in words):
            rendered = "[POSSIBLE_MNEMONIC_REDACTED]"
        record.msg = rendered
        record.args = ()
        if not hasattr(record, "order_id"):
            record.order_id = "-"
        return True


class DryRunRates:
    async def get(self) -> Decimal:
        return Decimal(5)


def check_env_permissions() -> None:
    env_path = Path(".env")
    if sys.platform == "linux" and env_path.exists() and env_path.stat().st_mode & 0o077:
        raise RuntimeError("On Linux, protect .env with chmod 600")


async def supervised(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    backoff = 1.0
    logger = logging.getLogger("app.supervisor")
    while True:
        try:
            await factory()
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background task crashed: %s", name, extra={"order_id": "-"})
            await asyncio.sleep(min(backoff, 60) + random.random())
            backoff = min(backoff * 2, 60)


async def fulfillment_loop(bot: Bot, settings: Settings, store: Store, provider: MockProvider) -> None:
    while True:
        await fulfill_paid_orders(bot, settings, store, provider)
        await asyncio.sleep(settings.poll_seconds)


async def backup_loop(store: Store, settings: Settings) -> None:
    while True:
        now = datetime.now(UTC)
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=10, second=0, microsecond=0)
        await asyncio.sleep(max(1, (next_run - now).total_seconds()))
        target = settings.backup_dir / f"{settings.database_path.stem}-backup-{datetime.now(UTC):%Y%m%d}.sqlite3"
        await store.backup(target)
        store.prune_backups(settings.backup_dir, 14)


async def payment_loop(
    bot: Bot,
    settings: Settings,
    store: Store,
    watcher: TonCenterWatcher | TronGridWatcher,
    asset_tolerance: Decimal,
) -> None:
    while True:
        await watch_payments(
            bot,
            settings,
            store,
            watcher,
            ton_receive_address=settings.ton_receive_address,
            tolerance=asset_tolerance,
        )
        await asyncio.sleep(settings.poll_seconds)


async def expiry_loop(bot: Bot, settings: Settings, store: Store) -> None:
    while True:
        await expire_orders(bot, settings, store)
        await asyncio.sleep(min(settings.poll_seconds, 60))


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s order_id=%(order_id)s %(name)s: %(message)s",
    )
    redactor = SecretRedactionFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
    check_env_permissions()
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    with settings.config_path.open("r", encoding="utf-8") as source:
        app_config = yaml.safe_load(source)
    if not settings.dry_run:
        raise SystemExit(
            "Production checkout is disabled until a seedless Fragment quote, recipient, delivery, and status contract is independently verified."
        )
    store = Store(settings.database_path)
    await store.open()
    bot = Bot(
        settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    provider = MockProvider(Decimal(app_config["pricing"]["dry_run_mock_unit_cost_usd"]))
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(settings, store))
    site_app = build_site(settings, store, provider, DryRunRates())
    runner = web.AppRunner(site_app, access_log=None)
    tasks: list[asyncio.Task[None]] = []
    watchers: list[tuple[TonCenterWatcher | TronGridWatcher, Decimal]] = []
    http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)
    )
    if not settings.dry_run:
        if settings.ton_receive_address:
            watchers.append((
                TonCenterWatcher(
                    settings.ton_receive_address,
                    settings.toncenter_api_key.get_secret_value() if settings.toncenter_api_key else "",
                    http_session,
                ),
                settings.ton_amount_tolerance,
            ))
        if settings.trc20_receive_address:
            watchers.append((
                TronGridWatcher(
                    settings.trc20_receive_address,
                    settings.trongrid_api_key.get_secret_value() if settings.trongrid_api_key else "",
                    http_session,
                    settings.usdt_contract_address,
                ),
                settings.usdt_amount_tolerance,
            ))
    try:
        await reconcile_processing(bot, settings, store, provider)
        await runner.setup()
        await web.TCPSite(runner, settings.site_host, settings.site_port).start()
        tasks.append(asyncio.create_task(supervised(
            "fulfillment", lambda: fulfillment_loop(bot, settings, store, provider)
        )))
        tasks.append(asyncio.create_task(supervised(
            "database_backup", lambda: backup_loop(store, settings)
        )))
        tasks.append(asyncio.create_task(supervised(
            "order_expiry", lambda: expiry_loop(bot, settings, store)
        )))
        for watcher, tolerance in watchers:
            tasks.append(asyncio.create_task(supervised(
                f"payment_watcher_{watcher.asset}",
                partial(payment_loop, bot, settings, store, watcher, tolerance),
            )))
        if settings.mini_app_url:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Мои заказы",
                    web_app=WebAppInfo(url=settings.mini_app_url),
                )
            )
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        await http_session.close()
        await bot.session.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(run())
