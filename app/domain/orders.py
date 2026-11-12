from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class OrderStatus(StrEnum):
    CREATED = "created"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    PROCESSING = "processing"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    UNDERPAID = "underpaid"
    OVERPAID = "overpaid"
    LATE_PAYMENT = "late_payment"
    FAILED = "failed"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"
    UNMATCHED_TRANSFER = "unmatched_transfer"


TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.CREATED: {OrderStatus.AWAITING_PAYMENT, OrderStatus.CANCELLED},
    OrderStatus.AWAITING_PAYMENT: {
        OrderStatus.PAID,
        OrderStatus.UNDERPAID,
        OrderStatus.OVERPAID,
        OrderStatus.LATE_PAYMENT,
        OrderStatus.EXPIRED,
        OrderStatus.CANCELLED,
    },
    OrderStatus.PAID: {
        OrderStatus.PROCESSING,
        OrderStatus.FAILED,
        OrderStatus.NEEDS_MANUAL_REVIEW,
    },
    OrderStatus.PROCESSING: {
        OrderStatus.COMPLETED,
        OrderStatus.FAILED,
        OrderStatus.NEEDS_MANUAL_REVIEW,
    },
    OrderStatus.FAILED: {OrderStatus.REFUND_PENDING, OrderStatus.NEEDS_MANUAL_REVIEW},
    OrderStatus.REFUND_PENDING: {OrderStatus.REFUNDED, OrderStatus.NEEDS_MANUAL_REVIEW},
    OrderStatus.LATE_PAYMENT: {OrderStatus.PAID, OrderStatus.REFUNDED, OrderStatus.NEEDS_MANUAL_REVIEW},
    OrderStatus.EXPIRED: {OrderStatus.LATE_PAYMENT, OrderStatus.REFUNDED, OrderStatus.NEEDS_MANUAL_REVIEW},
    OrderStatus.UNDERPAID: {OrderStatus.PAID, OrderStatus.OVERPAID, OrderStatus.UNDERPAID, OrderStatus.REFUNDED, OrderStatus.NEEDS_MANUAL_REVIEW},
    OrderStatus.OVERPAID: {OrderStatus.PAID, OrderStatus.REFUNDED, OrderStatus.NEEDS_MANUAL_REVIEW},
    OrderStatus.NEEDS_MANUAL_REVIEW: {OrderStatus.COMPLETED, OrderStatus.REFUND_PENDING, OrderStatus.REFUNDED},
    OrderStatus.COMPLETED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REFUNDED: set(),
    OrderStatus.UNMATCHED_TRANSFER: set(),
}


def can_transition(old: OrderStatus, new: OrderStatus) -> bool:
    return new in TRANSITIONS[old]


@dataclass(frozen=True, slots=True)
class PaymentMatch:
    status: OrderStatus
    difference: Decimal
    requires_owner_confirmation: bool = False


def match_amount(
    *,
    expected: Decimal,
    received: Decimal,
    tolerance: Decimal,
    expired: bool,
    within_grace: bool,
) -> PaymentMatch:
    if min(expected, received, tolerance) < 0 or expected == 0:
        raise ValueError("Invalid payment amounts")
    difference = received - expected
    if expired:
        if within_grace:
            return PaymentMatch(OrderStatus.LATE_PAYMENT, difference, True)
        return PaymentMatch(OrderStatus.UNMATCHED_TRANSFER, difference)
    if received < expected - tolerance:
        return PaymentMatch(OrderStatus.UNDERPAID, difference)
    if received > expected + tolerance:
        return PaymentMatch(OrderStatus.OVERPAID, difference)
    return PaymentMatch(OrderStatus.PAID, difference)
