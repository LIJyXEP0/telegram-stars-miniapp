from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from app.miniapp_auth import InvalidTelegramInitData, validate_init_data


def signed_init_data(bot_token: str, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAEAAE",
        "user": json.dumps({"id": 123456789, "first_name": "Test"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(bot_token.encode(), b"WebAppData", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_telegram_init_data_returns_verified_user() -> None:
    result = validate_init_data(signed_init_data("123:secret", 1_700_000_000), "123:secret", now=1_700_000_010)
    assert result["user_id"] == 123456789


def test_tampered_or_expired_init_data_is_rejected() -> None:
    value = signed_init_data("123:secret", 1_700_000_000)
    with pytest.raises(InvalidTelegramInitData):
        validate_init_data(value.replace("Test", "Evil"), "123:secret", now=1_700_000_010)
    with pytest.raises(InvalidTelegramInitData, match="Expired"):
        validate_init_data(value, "123:secret", now=1_700_100_000)
