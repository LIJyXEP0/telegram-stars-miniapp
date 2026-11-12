from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from app.domain.orders import OrderStatus, can_transition


class OrderConflict(RuntimeError):
    pass


class Store:
    """A single WAL-enabled SQLite connection shared by all application services."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self._migrate()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with self.lock:
            target = await aiosqlite.connect(destination)
            try:
                await self._db().backup(target)
            finally:
                await target.close()

    def prune_backups(self, directory: Path, retention_days: int) -> None:
        cutoff = time.time() - retention_days * 86400
        for file in directory.glob(f"{self.path.stem}-backup-*.sqlite3"):
            if file.stat().st_mtime < cutoff:
                file.unlink(missing_ok=True)

    def _db(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("Database is not open")
        return self.conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.lock:
            conn = self._db()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    async def _migrate(self) -> None:
        async with self.lock:
            conn = self._db()
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS buyers (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    language TEXT NOT NULL DEFAULT 'ru',
                    banned INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    product TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    recipient TEXT NOT NULL,
                    payment_asset TEXT NOT NULL,
                    expected_amount TEXT NOT NULL,
                    provider_cost_usd TEXT NOT NULL,
                    price_usd TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    refund_address TEXT,
                    ton_memo TEXT UNIQUE,
                    tracking_token_hash TEXT,
                    tx_hash TEXT UNIQUE,
                    tx_sender TEXT,
                    payment_amount TEXT,
                    status TEXT NOT NULL CHECK(status IN (
                      'created','awaiting_payment','paid','processing','completed',
                      'expired','cancelled','underpaid','overpaid','late_payment',
                      'failed','refund_pending','refunded','needs_manual_review','unmatched_transfer'
                    )),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    provider_request_id TEXT,
                    provider_result TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_orders_user_time ON orders(user_id,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_orders_state_expiry ON orders(status,expires_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_usdt_amount
                  ON orders(payment_asset,expected_amount)
                  WHERE payment_asset='usdt' AND status='awaiting_payment';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_ton_memo
                  ON orders(ton_memo)
                  WHERE payment_asset='ton' AND status='awaiting_payment' AND ton_memo IS NOT NULL;
                CREATE TABLE IF NOT EXISTS order_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_order_time ON order_events(order_id,ts);
                CREATE TABLE IF NOT EXISTS incoming_transfers (
                    tx_hash TEXT PRIMARY KEY,
                    asset TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    memo TEXT,
                    timestamp INTEGER NOT NULL,
                    matched_order_id TEXT REFERENCES orders(order_id),
                    classification TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watcher_cursors (
                    asset TEXT PRIMARY KEY,
                    cursor_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_quotes (
                    product TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    cost_usd TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL,
                    PRIMARY KEY(product,quantity)
                );
                """
            )
            row = await (await conn.execute("SELECT MAX(version) AS v FROM schema_migrations")).fetchone()
            version = int(row["v"] or 0) if row else 0
            if version < 1:
                await conn.execute(
                    "INSERT INTO schema_migrations(version,applied_at) VALUES(1,?)",
                    (int(time.time()),),
                )
                version = 1
            columns = await (await conn.execute("PRAGMA table_info(orders)")).fetchall()
            if not any(column["name"] == "tracking_token_hash" for column in columns):
                await conn.execute("ALTER TABLE orders ADD COLUMN tracking_token_hash TEXT")
            if version < 2:
                await conn.execute(
                    "INSERT INTO schema_migrations(version,applied_at) VALUES(2,?)",
                    (int(time.time()),),
                )

    async def create_order(self, order: dict[str, Any]) -> None:
        now = int(time.time())
        async with self.transaction() as conn:
            if order["payment_asset"] == "usdt":
                base = Decimal(order["expected_amount"]).quantize(Decimal("0.000001"))
                max_offset = Decimal(order["max_amount_offset"])
                async with conn.execute(
                    "SELECT expected_amount FROM orders WHERE payment_asset='usdt' AND status='awaiting_payment'"
                ) as cur:
                    used = {Decimal(row[0]) for row in await cur.fetchall()}
                quantum = Decimal("0.000001")
                for step in range(int(max_offset / quantum) + 1):
                    candidate = base + quantum * step
                    if candidate not in used:
                        order["expected_amount"] = candidate
                        break
                else:
                    raise RuntimeError("USDT amount allocation range is full; retry shortly")
            await conn.execute(
                """INSERT INTO orders(order_id,user_id,product,quantity,recipient,payment_asset,
                   expected_amount,provider_cost_usd,price_usd,created_at,expires_at,refund_address,
                   ton_memo,tracking_token_hash,status,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'created',?)""",
                (
                    order["order_id"], order["user_id"], order["product"], order["quantity"],
                    order["recipient"], order["payment_asset"], str(order["expected_amount"]),
                    str(order["provider_cost_usd"]), str(order["price_usd"]),
                    order.get("created_at", now), order["expires_at"],
                    order.get("refund_address"), order.get("ton_memo"),
                    order.get("tracking_token_hash"), order["idempotency_key"],
                ),
            )
            await conn.execute(
                "INSERT INTO order_events(order_id,from_status,to_status,ts,details_json) VALUES(?,NULL,?,?,?)",
                (order["order_id"], OrderStatus.CREATED.value, now, "{}"),
            )
            await self._transition(conn, order["order_id"], OrderStatus.CREATED, OrderStatus.AWAITING_PAYMENT, {"checkout_created": True})

    async def _transition(
        self, conn: aiosqlite.Connection, order_id: str, old: OrderStatus,
        new: OrderStatus, details: dict[str, Any],
    ) -> None:
        if not can_transition(old, new):
            raise OrderConflict(f"Illegal transition {old.value} -> {new.value}")
        cursor = await conn.execute(
            "UPDATE orders SET status=? WHERE order_id=? AND status=?",
            (new.value, order_id, old.value),
        )
        if cursor.rowcount != 1:
            raise OrderConflict(f"Order {order_id} state changed concurrently")
        await conn.execute(
            "INSERT INTO order_events(order_id,from_status,to_status,ts,details_json) VALUES(?,?,?,?,?)",
            (order_id, old.value, new.value, int(time.time()), json.dumps(details, ensure_ascii=False, sort_keys=True)),
        )

    async def transition(
        self, order_id: str, old: OrderStatus, new: OrderStatus,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with self.transaction() as conn:
            await self._transition(conn, order_id, old, new, details or {})

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        async with self.lock:
            row = await (await self._db().execute("SELECT * FROM orders WHERE order_id=?", (order_id,))).fetchone()
        return dict(row) if row else None

    async def upsert_buyer(self, user_id: int, username: str | None, language: str) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                "INSERT INTO buyers(user_id,username,language,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,language=excluded.language",
                (user_id, username, language, int(time.time())),
            )

    async def claim_order(self, order_id: str, user_id: int, token_hash: str) -> dict[str, Any] | None:
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE orders SET user_id=? WHERE order_id=? AND tracking_token_hash=? AND (user_id=0 OR user_id=?)",
                (user_id, order_id, token_hash, user_id),
            )
            row = await (await conn.execute(
                "SELECT order_id,user_id,product,quantity,recipient,payment_asset,expected_amount,status,created_at,expires_at,tx_hash,payment_amount FROM orders WHERE order_id=? AND user_id=? AND tracking_token_hash=?",
                (order_id, user_id, token_hash),
            )).fetchone()
            return dict(row) if row else None

    async def user_orders(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        async with self.lock:
            rows = await (await self._db().execute(
                "SELECT order_id,product,quantity,recipient,payment_asset,expected_amount,status,created_at,expires_at,tx_hash,payment_amount FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )).fetchall()
        return [dict(row) for row in rows]

    async def orders_in_status(self, status: OrderStatus) -> list[dict[str, Any]]:
        async with self.lock:
            rows = await (await self._db().execute(
                "SELECT * FROM orders WHERE status=? ORDER BY created_at", (status.value,)
            )).fetchall()
        return [dict(row) for row in rows]

    async def update_provider_request(self, order_id: str, request_id: str) -> None:
        async with self.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE orders SET provider_request_id=? WHERE order_id=? AND status='processing' AND provider_request_id IS NULL",
                (request_id, order_id),
            )
            if cursor.rowcount != 1:
                raise OrderConflict("Provider request ID could not be persisted")

    async def update_provider_result(self, order_id: str, result: str) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE orders SET provider_result=? WHERE order_id=?", (result[:4000], order_id)
            )

    async def active_orders(self, asset: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM orders WHERE status='awaiting_payment'"
        args: tuple[Any, ...] = ()
        if asset:
            sql += " AND payment_asset=?"
            args = (asset,)
        sql += " ORDER BY created_at"
        async with self.lock:
            rows = await (await self._db().execute(sql, args)).fetchall()
        return [dict(row) for row in rows]

    async def matchable_orders(self, asset: str, transfer_time: int, grace_seconds: int) -> list[dict[str, Any]]:
        async with self.lock:
            rows = await (await self._db().execute(
                "SELECT * FROM orders WHERE payment_asset=? AND created_at<=? AND (status IN ('awaiting_payment','underpaid') OR (status='expired' AND expires_at+? >= ?)) ORDER BY created_at",
                (asset, transfer_time, grace_seconds, transfer_time),
            )).fetchall()
        return [dict(row) for row in rows]

    async def allocate_usdt_amount(self, base: Decimal, maximum_offset: Decimal) -> Decimal:
        quantum = Decimal("0.000001")
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT expected_amount FROM orders WHERE payment_asset='usdt' AND status='awaiting_payment'"
            ) as cur:
                used = {Decimal(row[0]) for row in await cur.fetchall()}
            steps = int(maximum_offset / quantum)
            for step in range(steps + 1):
                candidate = base + quantum * step
                if candidate not in used:
                    return candidate
        raise RuntimeError("USDT order amount range is full; ask the buyer to retry shortly")

    async def save_cursor(self, asset: str, value: dict[str, Any]) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO watcher_cursors(asset,cursor_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(asset) DO UPDATE SET cursor_json=excluded.cursor_json,updated_at=excluded.updated_at""",
                (asset, json.dumps(value, sort_keys=True), int(time.time())),
            )

    async def transfer_exists(self, tx_hash: str) -> bool:
        async with self.lock:
            row = await (await self._db().execute(
                "SELECT 1 FROM incoming_transfers WHERE tx_hash=?", (tx_hash,)
            )).fetchone()
        return row is not None

    async def get_cursor(self, asset: str) -> dict[str, Any]:
        async with self.lock:
            row = await (await self._db().execute("SELECT cursor_json FROM watcher_cursors WHERE asset=?", (asset,))).fetchone()
        return json.loads(row[0]) if row else {}

    async def record_transfer(self, transfer: dict[str, Any], classification: str, order_id: str | None) -> bool:
        try:
            async with self.transaction() as conn:
                await conn.execute(
                    """INSERT INTO incoming_transfers(tx_hash,asset,sender,destination,amount,memo,
                       timestamp,matched_order_id,classification,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (transfer["tx_hash"],transfer["asset"],transfer["sender"],transfer["destination"],
                     str(transfer["amount"]),transfer.get("memo"),transfer["timestamp"],order_id,
                     classification,json.dumps(transfer.get("raw",{}),sort_keys=True)),
                )
            return True
        except aiosqlite.IntegrityError:
            return False

    async def apply_transfer(
        self, transfer: dict[str, Any], classification: str, order_id: str,
        next_status: OrderStatus, details: dict[str, Any],
    ) -> bool:
        async with self.transaction() as conn:
            row = await (await conn.execute(
                "SELECT status,payment_amount FROM orders WHERE order_id=?", (order_id,)
            )).fetchone()
            if not row:
                return False
            try:
                await conn.execute(
                    """INSERT INTO incoming_transfers(tx_hash,asset,sender,destination,amount,memo,
                       timestamp,matched_order_id,classification,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (transfer["tx_hash"],transfer["asset"],transfer["sender"],transfer["destination"],
                     str(transfer["amount"]),transfer.get("memo"),transfer["timestamp"],order_id,
                     classification,json.dumps(transfer.get("raw",{}),sort_keys=True)),
                )
            except aiosqlite.IntegrityError:
                return False
            prior = Decimal(row["payment_amount"] or "0")
            total = prior + Decimal(str(transfer["amount"]))
            expected = Decimal(details["expected"])
            old = OrderStatus(row["status"])
            if next_status == OrderStatus.LATE_PAYMENT or old == OrderStatus.EXPIRED:
                next_status = OrderStatus.LATE_PAYMENT
            elif total < expected:
                next_status = OrderStatus.UNDERPAID
            elif total > expected:
                next_status = OrderStatus.OVERPAID
            elif next_status not in {OrderStatus.LATE_PAYMENT}:
                next_status = OrderStatus.PAID
            await conn.execute(
                "UPDATE orders SET tx_hash=COALESCE(tx_hash,?),tx_sender=COALESCE(tx_sender,?),payment_amount=? WHERE order_id=?",
                (transfer["tx_hash"],transfer["sender"],str(total),order_id),
            )
            details["cumulative_amount"] = str(total)
            await self._transition(conn, order_id, old, next_status, details)
            return True
