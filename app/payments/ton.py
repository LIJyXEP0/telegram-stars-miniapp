from __future__ import annotations

import base64
from decimal import Decimal
from typing import Any

import aiohttp
from tonsdk.boc import Cell
from tonsdk.utils import Address

from app.payments.base import Transfer


def normalize_ton_address(value: str) -> str:
    parsed = Address(value)
    return f"{parsed.wc}:{bytes(parsed.hash_part).hex()}"


def decode_ton_comment(body_boc: str) -> str | None:
    try:
        cell = Cell.one_from_boc(base64.b64decode(body_boc, validate=True))
        body = cell.begin_parse()
        if body.read_uint(32) != 0:
            return None
        return body.read_string()
    except (ValueError, TypeError, IndexError):
        return None


class TonCenterWatcher:
    asset = "ton"
    base_url = "https://toncenter.com/api/v3"

    def __init__(self, address: str, api_key: str, session: aiohttp.ClientSession) -> None:
        self.address = address.strip()
        self.normalized_address = normalize_ton_address(address)
        self.api_key = api_key
        self.session = session

    async def fetch(self, cursor: dict[str, Any]) -> tuple[list[Transfer], dict[str, Any]]:
        start_lt = max(0, int(cursor.get("lt", 0)) - 1)
        offset = 0
        transfers: list[Transfer] = []
        next_lt = int(cursor.get("lt", 0))
        for _ in range(20):
            params: dict[str, str | int] = {
                "account": self.address,
                "limit": 1000,
                "offset": offset,
                "sort": "asc",
                "start_lt": start_lt,
            }
            headers = {"X-API-Key": self.api_key} if self.api_key else {}
            async with self.session.get(
                f"{self.base_url}/transactions", params=params, headers=headers
            ) as response:
                response.raise_for_status()
                payload = await response.json()
            rows = payload.get("transactions", [])
            for tx in rows:
                in_msg = tx.get("in_msg") or {}
                destination = str(in_msg.get("destination", ""))
                try:
                    if normalize_ton_address(destination) != self.normalized_address:
                        continue
                except (ValueError, TypeError):
                    continue
                value_nano = int(in_msg.get("value") or 0)
                if value_nano <= 0:
                    continue
                lt = int(tx.get("lt", 0))
                next_lt = max(next_lt, lt)
                content = in_msg.get("message_content") or {}
                transfers.append(
                    Transfer(
                        tx_hash=str(tx.get("hash", "")),
                        asset=self.asset,
                        sender=str(in_msg.get("source", "")),
                        destination=destination,
                        amount=Decimal(value_nano) / Decimal(1_000_000_000),
                        timestamp=int(tx.get("now", 0)),
                        memo=decode_ton_comment(str(content.get("body", ""))),
                        raw=tx,
                    )
                )
            if len(rows) < 1000:
                break
            offset += len(rows)
        return transfers, {"lt": next_lt}
