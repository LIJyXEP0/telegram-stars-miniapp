from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def amount(value: str | int | Decimal) -> Decimal:
    """Parse money without ever passing through binary floating point."""
    if isinstance(value, float):
        raise TypeError("Money values must not be floats")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Money amount must be a finite non-negative number")
    return result


def quantize(value: Decimal, places: int) -> Decimal:
    if places < 0 or places > 18:
        raise ValueError("places must be between 0 and 18")
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def customer_price(
    provider_cost_usd: Decimal,
    margin_percent: Decimal,
    fixed_fee_usd: Decimal,
    fx_rate: Decimal,
    output_places: int = 2,
) -> Decimal:
    if provider_cost_usd <= 0 or fx_rate <= 0 or fixed_fee_usd < 0:
        raise ValueError("Costs and FX rate must be positive; fee cannot be negative")
    if margin_percent < 0:
        raise ValueError("Margin cannot be negative")
    usd_total = provider_cost_usd * (Decimal(1) + margin_percent / Decimal(100))
    return quantize((usd_total + fixed_fee_usd) * fx_rate, output_places)
