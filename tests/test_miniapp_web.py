from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.db.store import Store
from app.fulfillment.mock import MockProvider
from app.web import build_site


class DummyRates:
    async def get(self) -> Decimal:
        return Decimal(5)


@pytest.mark.asyncio
async def test_miniapp_routes_render_status_support_without_checkout(tmp_path) -> None:
    store = Store(tmp_path / "miniapp.sqlite3")
    await store.open()
    settings = Settings(
        bot_token="test",
        owner_id=123,
        config_path=Path("config.yml"),
        support_username="shop_support",
    )
    client = TestClient(TestServer(build_site(
        settings, store, MockProvider(Decimal("0.01")), DummyRates()
    )))
    await client.start_server()
    try:
        page = await client.get("/miniapp/")
        assert page.status == 200
        body = await page.text()
        assert "Мои заказы" in body
        assert "Привязать заказ" in body
        assert "telegram-web-app.js" in body
        assert "checkout" not in body.lower()

        config_js = await client.get("/miniapp/config.js")
        assert config_js.status == 200
        config = await client.get("/api/miniapp/config")
        assert (await config.json())["support_url"] == "https://t.me/shop_support"

        unauthorized = await client.post("/api/miniapp/orders", json={"init_data": ""})
        assert unauthorized.status == 401
    finally:
        await client.close()
        await store.close()
