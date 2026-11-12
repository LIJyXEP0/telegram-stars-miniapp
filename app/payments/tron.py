from __future__ import annotations

from decimal import Decimal
from typing import Any
from urllib.parse import quote

import aiohttp

from app.payments.base import Transfer

MAINNET_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


class TronGridWatcher:
    asset = "usdt"
    base_url = "https://api.trongrid.io"

    def __init__(self, address: str, api_key: str, session: aiohttp.ClientSession, contract_address: str = MAINNET_USDT) -> None:
        self.address = address.strip()
        self.api_key = api_key
        self.session = session
        self.contract_address = contract_address

    async def fetch(self, cursor: dict[str, Any]) -> tuple[list[Transfer], dict[str, Any]]:
        timestamp = max(0, int(cursor.get("timestamp_ms", 0)) - 120_000)
        fingerprint: str | None = None
        transfers: list[Transfer] = []
        for _ in range(20):
            params: dict[str, str | int] = {
                "only_confirmed": "true",
                "only_to": "true",
                "limit": 200,
                "order_by": "block_timestamp,asc",
                "contract_address": self.contract_address,
                "min_timestamp": timestamp,
            }
            if fingerprint:
                params["fingerprint"] = fingerprint
            async with self.session.get(
                f"{self.base_url}/v1/accounts/{quote(self.address, safe='')}/transactions/trc20",
                params=params,
                headers={"TRON-PRO-API-KEY": self.api_key} if self.api_key else {},
            ) as response:
                response.raise_for_status()
                payload = await response.json()
            for item in payload.get("data", []):
                contract = str(item.get("token_info", {}).get("address", ""))
                if contract != self.contract_address:
                    continue
                token = item.get("token_info") or {}
                decimals = int(token.get("decimals", 6))
                integer_value = int(item["value"])
                transfers.append(
                    Transfer(
                        tx_hash=str(item["transaction_id"]),
                        asset=self.asset,
                        sender=str(item.get("from", "")),
                        destination=str(item.get("to", "")),
                        amount=Decimal(integer_value) / (Decimal(10) ** decimals),
                        timestamp=int(item["block_timestamp"]) // 1000,
                        memo=None,
                        raw=item,
                    )
                )
            meta = payload.get("meta") or {}
            fingerprint = meta.get("fingerprint")
            if not fingerprint or not payload.get("data"):
                break
        next_timestamp = max(
            int(cursor.get("timestamp_ms", 0)),
            max((tx.timestamp * 1000 for tx in transfers), default=0),
        )
        return transfers, {"timestamp_ms": next_timestamp}
