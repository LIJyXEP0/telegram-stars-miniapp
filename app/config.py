from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: SecretStr
    owner_id: int
    admin_ids: str = ""
    site_host: str = "127.0.0.1"
    site_port: int = Field(default=8080, ge=1, le=65535)
    public_base_url: str = "http://127.0.0.1:8080"
    mini_app_url: str = ""
    mini_app_origin: str = ""
    database_path: Path = Field(default=Path("app-orders.sqlite3"), validation_alias="APP_DATABASE_PATH")
    config_path: Path = Path("config.yml")
    backup_dir: Path = Path("backups")
    order_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    payment_grace_seconds: int = Field(default=600, ge=0, le=86400)
    poll_seconds: int = Field(default=15, ge=5, le=300)
    trc20_receive_address: str = Field(default="", validation_alias="USDT_TRC20_ADDRESS")
    ton_receive_address: str = Field(default="", validation_alias="TON_ADDRESS")
    trongrid_api_key: SecretStr | None = None
    toncenter_api_key: SecretStr | None = None
    usdt_contract_address: str = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    fulfillment_mode: Literal["mock", "fragment"] = "mock"
    dry_run: bool = True
    allow_third_party_signing: bool = False
    hot_wallet_balance_usd: Decimal | None = None
    max_active_orders_per_user: int = Field(default=3, ge=1, le=50)
    max_tron_offset_usdt: Decimal = Decimal("0.05")
    usdt_amount_tolerance: Decimal = Decimal(0)
    ton_amount_tolerance: Decimal = Decimal(0)
    rates_max_deviation_percent: Decimal = Decimal(10)
    support_username: str = ""

    @model_validator(mode="after")
    def validate_production(self) -> Settings:
        if self.mini_app_url:
            mini_app_parts = urlsplit(self.mini_app_url)
            if mini_app_parts.scheme != "https" or not mini_app_parts.hostname:
                raise ValueError("MINI_APP_URL must be a valid HTTPS URL")
        if self.mini_app_origin:
            origin_parts = urlsplit(self.mini_app_origin)
            if (
                origin_parts.scheme != "https"
                or not origin_parts.hostname
                or origin_parts.path not in {"", "/"}
                or origin_parts.query
                or origin_parts.fragment
            ):
                raise ValueError("MINI_APP_ORIGIN must be an HTTPS origin without a path")
        if not self.dry_run:
            if self.fulfillment_mode != "fragment":
                raise ValueError("Production sales require FULFILLMENT_MODE=fragment")
            if not self.trc20_receive_address and not self.ton_receive_address:
                raise ValueError("At least one dedicated receiving address is required")
            if not self.public_base_url.startswith("https://"):
                raise ValueError("Production checkout site must use HTTPS")
            if self.allow_third_party_signing:
                raise ValueError(
                    "Third-party signing is unsupported: wallet seeds are never sent to remote APIs"
                )
        return self

    @property
    def admin_ids_set(self) -> set[int]:
        values = {self.owner_id}
        for token in self.admin_ids.split(","):
            token = token.strip()
            if token:
                values.add(int(token))
        return values
