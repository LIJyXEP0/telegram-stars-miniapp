from decimal import Decimal

import pytest

from app.domain.money import amount, customer_price
from app.domain.orders import OrderStatus, can_transition, match_amount


def test_money_math_uses_decimal_and_rounds_half_up() -> None:
    assert customer_price(Decimal(10), Decimal(20), Decimal(1), Decimal("1.25")) == Decimal("16.25")
    assert amount("0.1") + amount("0.2") == Decimal("0.3")
    with pytest.raises(TypeError):
        amount(0.1)


def test_state_machine_rejects_unsafe_transitions() -> None:
    assert can_transition(OrderStatus.AWAITING_PAYMENT, OrderStatus.PAID)
    assert not can_transition(OrderStatus.COMPLETED, OrderStatus.PROCESSING)
    assert not can_transition(OrderStatus.PROCESSING, OrderStatus.PAID)


def test_payment_matching_exact_under_over_late() -> None:
    expected = Decimal(10)
    assert match_amount(expected=expected, received=expected, tolerance=Decimal(0), expired=False, within_grace=False).status == OrderStatus.PAID
    assert match_amount(expected=expected, received=Decimal(9), tolerance=Decimal(0), expired=False, within_grace=False).status == OrderStatus.UNDERPAID
    assert match_amount(expected=expected, received=Decimal(11), tolerance=Decimal(0), expired=False, within_grace=False).status == OrderStatus.OVERPAID
    late = match_amount(expected=expected, received=expected, tolerance=Decimal(0), expired=True, within_grace=True)
    assert late.status == OrderStatus.LATE_PAYMENT and late.requires_owner_confirmation


def test_unknown_after_grace_is_unmatched() -> None:
    match = match_amount(expected=Decimal(1), received=Decimal(1), tolerance=Decimal(0), expired=True, within_grace=False)
    assert match.status == OrderStatus.UNMATCHED_TRANSFER
