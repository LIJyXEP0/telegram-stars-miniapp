import pytest
from tonsdk.utils._exceptions import InvalidAddressError

from app.payments.ton import normalize_ton_address


def test_ton_friendly_and_raw_address_normalize_equally() -> None:
    friendly = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"
    parsed = normalize_ton_address(friendly)
    workchain, address_hash = parsed.split(":")
    raw = f"{workchain}:{address_hash}"
    assert normalize_ton_address(raw) == parsed


def test_invalid_ton_address_is_rejected() -> None:
    with pytest.raises(InvalidAddressError):
        normalize_ton_address("not-a-ton-address")
