from __future__ import annotations

import base64
import hashlib
import html
import io
import re
import secrets
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote as url_quote

import qrcode
import yaml
from aiohttp import web

from app.config import Settings
from app.db.store import Store
from app.domain.money import customer_price, quantize
from app.domain.orders import OrderStatus
from app.fulfillment.base import FulfillmentProvider
from app.miniapp_auth import InvalidTelegramInitData, validate_init_data


class RateProvider(Protocol):
    async def get(self) -> Decimal: ...

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def _load_yaml(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as source:
        value = yaml.safe_load(source)
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a YAML mapping")
    return value


def _qr_data(data: str) -> str:
    output = io.BytesIO()
    qrcode.make(data).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def build_site(
    settings: Settings,
    store: Store,
    provider: FulfillmentProvider,
    rates: RateProvider,
) -> web.Application:
    @web.middleware
    async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        origin = request.headers.get("Origin", "")
        allowed_origin = settings.mini_app_origin.rstrip("/")
        if request.method == "OPTIONS":
            if origin and (not allowed_origin or origin != allowed_origin):
                raise web.HTTPForbidden(text="Origin is not allowed")
            response: web.StreamResponse = web.Response(status=204)
        else:
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                headers = {
                    key: value for key, value in exc.headers.items()
                    if key.lower() != "content-type"
                }
                response = web.Response(
                    status=exc.status,
                    text=exc.text,
                    headers=headers,
                    content_type=exc.content_type,
                )
        if origin and allowed_origin and origin == allowed_origin:
            response.headers["Access-Control-Allow-Origin"] = allowed_origin
            response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Vary"] = "Origin"
        return response

    app = web.Application(client_max_size=16_384, middlewares=[cors_middleware])
    site_cfg = _load_yaml(str(settings.config_path))
    catalog = site_cfg["catalog"]
    supported_assets = set(site_cfg["payments"]["assets"])
    bot_token = settings.bot_token.get_secret_value()

    async def telegram_user_id(init_data: str) -> int:
        try:
            verified = validate_init_data(init_data, bot_token)
            verified_id = verified["user_id"]
            if isinstance(verified_id, int):
                user = verified["user"]
                if isinstance(user, dict):
                    username = user.get("username")
                    language = str(user.get("language_code", "ru"))
                    await store.upsert_buyer(
                        verified_id,
                        username if isinstance(username, str) else None,
                        "en" if language.startswith("en") else "ru",
                    )
                return verified_id
            raise InvalidTelegramInitData("Invalid Telegram user ID")
        except (InvalidTelegramInitData, KeyError, TypeError, ValueError) as exc:
            raise web.HTTPUnauthorized(text="Open this app from Telegram and try again") from exc

    async def miniapp(request: web.Request) -> web.StreamResponse:
        if request.path == "/miniapp":
            raise web.HTTPPermanentRedirect(location="/miniapp/")
        path = Path(__file__).resolve().parent.parent / "miniapp" / "index.html"
        return web.FileResponse(path)

    async def miniapp_config_js(_: web.Request) -> web.StreamResponse:
        path = Path(__file__).resolve().parent.parent / "miniapp" / "config.js"
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def miniapp_config(_: web.Request) -> web.Response:
        support = settings.support_username.strip().lstrip("@")
        return web.json_response({
            "support_url": f"https://t.me/{url_quote(support)}" if support else "",
        })

    async def miniapp_orders(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (ValueError, web.HTTPException) as exc:
            raise web.HTTPBadRequest(text="Invalid request") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Invalid request")
        user_id = await telegram_user_id(str(payload.get("init_data", "")))
        return web.json_response({"orders": await store.user_orders(user_id)})

    async def miniapp_claim(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (ValueError, web.HTTPException) as exc:
            raise web.HTTPBadRequest(text="Invalid request") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Invalid request")
        user_id = await telegram_user_id(str(payload.get("init_data", "")))
        order_id = str(payload.get("order_id", "")).strip().upper()
        tracking_token = str(payload.get("tracking_token", "")).strip()
        if not re.fullmatch(r"[A-F0-9]{32}", order_id) or not re.fullmatch(r"[A-Za-z0-9_-]{30,64}", tracking_token):
            raise web.HTTPBadRequest(text="Check the order number and tracking code")
        token_hash = hashlib.sha256(tracking_token.encode()).hexdigest()
        order = await store.claim_order(order_id, user_id, token_hash)
        if order is None:
            raise web.HTTPNotFound(text="Order or tracking code was not found, or it belongs to another Telegram account")
        return web.json_response({"order": order})

    async def index(_: web.Request) -> web.Response:
        options: list[str] = []
        if catalog["stars"]["enabled"]:
            options.extend(
                f'<option value="stars:{int(q)}">{int(q)} Telegram Stars</option>'
                for q in catalog["stars"]["packages"]
            )
        if catalog["premium"]["enabled"]:
            options.extend(
                f'<option value="premium:{int(m)}">Telegram Premium · {int(m)} months</option>'
                for m in catalog["premium"]["durations_months"]
            )
        asset_options = "".join(
            f'<option value="{html.escape(asset)}">{html.escape(asset.upper())}</option>'
            for asset in sorted(supported_assets)
        )
        notice = "<p class='notice'>Тестовый режим: создаются только симуляции, реальные платежи не принимаются.</p>" if settings.dry_run else ""
        page = f"""<!doctype html><html lang="ru"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1"><title>Stars & Premium</title>
        <style>body{{font:16px system-ui;background:#101827;color:#eef4ff;margin:0;padding:3vh 4vw}}main{{max-width:560px;margin:auto;background:#19263a;padding:28px;border-radius:18px}}label{{display:block;margin:16px 0 6px}}input,select,button{{width:100%;padding:13px;border-radius:9px;border:1px solid #40536d;background:#111d2d;color:#fff;box-sizing:border-box}}button{{margin-top:22px;background:#388bfd;border:0;font-weight:700}}.notice{{color:#ffd36b}}</style>
        <main><h1>⭐ Stars & Premium</h1><p>Оплата на отдельном сайте. Переводи только точную сумму из карточки заказа.</p>{notice}
        <form method="post" action="/orders"><label>Товар</label><select name="product" required>{''.join(options)}</select>
        <label>Username получателя</label><input name="recipient" required minlength="5" maxlength="33" placeholder="@username">
        <label>Оплата</label><select name="asset" required>{asset_options}</select>
        <label>Код возвратного TON/USDT-адреса (необязательно)</label><input name="refund_address" maxlength="128">
        <label><input type="checkbox" name="accepted" value="yes" required style="width:auto"> Подтверждаю username и соглашаюсь с <a href="/terms">условиями</a>.</label>
        <button>Продолжить</button></form></main></html>"""
        return web.Response(text=page, content_type="text/html")

    async def create_order(request: web.Request) -> web.Response:
        form = await request.post()
        if form.get("accepted") != "yes":
            raise web.HTTPBadRequest(text="Terms acceptance is required")
        raw_product = str(form.get("product", ""))
        try:
            product, quantity_text = raw_product.split(":", 1)
            quantity = int(quantity_text)
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid product selection")
        if product == "stars":
            valid_quantity = quantity in {int(x) for x in catalog["stars"]["packages"]}
        elif product == "premium":
            valid_quantity = quantity in {int(x) for x in catalog["premium"]["durations_months"]}
        else:
            valid_quantity = False
        if not valid_quantity:
            raise web.HTTPBadRequest(text="Product is not available")
        username = str(form.get("recipient", "")).strip().lstrip("@").strip()
        asset = str(form.get("asset", "")).lower()
        refund_address = str(form.get("refund_address", "")).strip()[:128] or None
        if not USERNAME_RE.fullmatch(username):
            raise web.HTTPBadRequest(text="Invalid Telegram username")
        if asset not in supported_assets:
            raise web.HTTPBadRequest(text="Payment asset is not enabled")
        receive_address = settings.trc20_receive_address if asset == "usdt" else settings.ton_receive_address
        if not receive_address and not settings.dry_run:
            raise web.HTTPServiceUnavailable(text="This payment asset is temporarily unavailable")
        try:
            eligible = await provider.check_recipient(username, product, quantity)
            if not eligible:
                raise web.HTTPBadRequest(text="Recipient cannot receive this product")
            quote = await provider.quote(product, quantity)
            fx_rate = Decimal(1) if asset == "usdt" else await rates.get()
            pricing = site_cfg["pricing"]
            price_usd = customer_price(
                quote.provider_cost_usd,
                Decimal(pricing["margin_percent"]),
                Decimal(pricing["fixed_fee_usd"]),
                Decimal(1),
                int(pricing["output_places"]),
            )
        except web.HTTPException:
            raise
        except Exception as exc:
            raise web.HTTPServiceUnavailable(text="Product pricing or recipient check is unavailable") from exc
        order_id = secrets.token_hex(16).upper()
        tracking_token = secrets.token_urlsafe(24)
        ton_memo = f"SF-{order_id}" if asset == "ton" else None
        if asset == "ton":
            expected = quantize(price_usd / fx_rate, 9)
        else:
            expected = quantize(price_usd, 6)
        now = int(time.time())
        expires_at = now + settings.order_ttl_seconds
        order = {
            "order_id": order_id,
            "user_id": 0,
            "product": product,
            "quantity": quantity,
            "recipient": username,
            "payment_asset": asset,
            "expected_amount": expected,
            "provider_cost_usd": quote.provider_cost_usd,
            "price_usd": price_usd,
            "created_at": now,
            "expires_at": expires_at,
            "refund_address": refund_address,
            "ton_memo": ton_memo,
            "tracking_token_hash": hashlib.sha256(tracking_token.encode()).hexdigest(),
            "idempotency_key": f"order:{order_id}",
            "max_amount_offset": settings.max_tron_offset_usdt,
        }
        try:
            await store.create_order(order)
        except RuntimeError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc
        saved = await store.get_order(order_id)
        if saved is None:
            raise web.HTTPInternalServerError(text="Order was not saved")
        final_amount = Decimal(saved["expected_amount"])
        miniapp_note = (
            f"<p>Для привязки заказа в Mini App введи код отслеживания: "
            f"<code>{html.escape(tracking_token)}</code></p>"
            + (f"<p><a href=\"{html.escape(settings.mini_app_url, quote=True)}\">Открыть Mini App</a></p>" if settings.mini_app_url else "")
        )
        if settings.dry_run:
            page = f"""<!doctype html><html lang="ru"><meta charset="utf-8"><main><h1>Тестовый заказ {html.escape(order_id)}</h1>
            <p>{html.escape(product)} · {quantity} · @{html.escape(username)} · {final_amount} {asset.upper()}</p>
            <p>Реальная оплата отключена. Эта симуляция ничего не спишет и не отправит товар.</p>
            <p>Номер заказа: <code>{html.escape(order_id)}</code></p>{miniapp_note}
            <a href="/dev/simulate/{html.escape(order_id)}">Симулировать оплату (только для разработки)</a></main></html>"""
            return web.Response(text=page, content_type="text/html")
        extra = f"<p>Memo: <b>{html.escape(ton_memo)}</b></p>" if ton_memo else ""
        if asset == "ton":
            nano_amount = int(final_amount * Decimal(1_000_000_000))
            qr_value = (
                f"ton://transfer/{url_quote(receive_address, safe='')}?amount={nano_amount}"
                f"&text={url_quote(ton_memo or '', safe='')}"
            )
        else:
            qr_value = receive_address
        qr = _qr_data(qr_value)
        order_expiry = expires_at
        page = f"""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Оплата {html.escape(order_id)}</title><style>body{{font:16px system-ui;background:#101827;color:#eef4ff;padding:5vh 4vw}}main{{max-width:560px;margin:auto;background:#19263a;padding:28px;border-radius:18px}}code{{overflow-wrap:anywhere}}img{{width:220px;background:white;padding:8px;border-radius:8px}}.warn{{color:#ffd36b}}</style>
        <main><h1>Заказ {html.escape(order_id)}</h1><p>{html.escape(product)} · {quantity} · получатель @{html.escape(username)}</p>
        <p>Сеть: <b>{'TRON · TRC20' if asset == 'usdt' else 'TON'}</b></p><p>Сумма: <b>{final_amount} {asset.upper()}</b></p>
        <p>Адрес: <code>{html.escape(receive_address)}</code></p>{extra}<img src="{qr}" alt="QR для реквизитов">
        <p class="warn">Отправь точную сумму до {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(order_expiry))}. Переводы с неверной суммой могут потребовать ручной проверки.</p>
        <p>Статус: <b>{html.escape(saved['status'])}</b>. Обнови страницу статуса через /status/{html.escape(order_id)}.</p>{miniapp_note}</main></html>"""
        return web.Response(text=page, content_type="text/html")

    async def status(request: web.Request) -> web.Response:
        order = await store.get_order(request.match_info["order_id"])
        if not order:
            raise web.HTTPNotFound(text="Order not found")
        return web.json_response({"order_id": order["order_id"], "status": order["status"]})

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def terms(_: web.Request) -> web.Response:
        body = """<!doctype html><html lang=ru><meta charset=utf-8><title>Условия</title>
        <main><h1>Условия</h1><p>Оплаченный заказ направляется на проверку блокчейна и затем поставщику. Ошибки выдачи и платежи после срока рассматриваются поддержкой. При отказе выдачи деньги переходят в состояние возврата; возврат криптовалюты выполняется на подтверждённый адрес покупателя после проверки владельцем.</p><p>Для помощи укажи номер заказа и транзакцию в обращении владельцу магазина.</p></main></html>"""
        return web.Response(text=body, content_type="text/html")

    async def simulate(request: web.Request) -> web.Response:
        if not settings.dry_run or request.remote not in {"127.0.0.1", "::1"}:
            raise web.HTTPNotFound()
        order_id = request.match_info["order_id"]
        await store.transition(order_id, OrderStatus.AWAITING_PAYMENT,
                               OrderStatus.PAID,
                               {"dry_run_simulated": True})
        return web.HTTPFound(f"/status/{url_quote(order_id)}")

    app.router.add_get("/", index)
    app.router.add_get("/miniapp", miniapp)
    app.router.add_get("/miniapp/", miniapp)
    app.router.add_get("/miniapp/config.js", miniapp_config_js)
    app.router.add_get("/api/miniapp/config", miniapp_config)
    app.router.add_post("/api/miniapp/orders", miniapp_orders)
    app.router.add_post("/api/miniapp/claim", miniapp_claim)
    app.router.add_post("/orders", create_order)
    app.router.add_get("/status/{order_id}", status)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/terms", terms)
    app.router.add_get("/dev/simulate/{order_id}", simulate)
    return app
