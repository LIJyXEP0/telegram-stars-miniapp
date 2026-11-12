from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InvalidTelegramInitData(ValueError):
    pass


def validate_init_data(init_data: str, bot_token: str, *, now: int | None = None) -> dict[str, object]:
    if not init_data or len(init_data) > 8192:
        raise InvalidTelegramInitData("Missing or oversized Telegram init data")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise InvalidTelegramInitData("Duplicate init data field")
        fields[key] = value
    supplied_hash = fields.pop("hash", None)
    if not supplied_hash or len(supplied_hash) != 64:
        raise InvalidTelegramInitData("Missing init data signature")
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(bot_token.encode(), b"WebAppData", hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, supplied_hash):
        raise InvalidTelegramInitData("Invalid init data signature")
    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidTelegramInitData("Malformed Telegram user data") from exc
    current_time = int(time.time()) if now is None else now
    if auth_date > current_time + 60 or current_time - auth_date > 86_400:
        raise InvalidTelegramInitData("Expired init data")
    try:
        user = json.loads(fields["user"])
        user_id = int(user["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidTelegramInitData("Malformed Telegram user data") from exc
    if user_id <= 0:
        raise InvalidTelegramInitData("Invalid Telegram user ID")
    return {"user_id": user_id, "user": user, "auth_date": auth_date}
