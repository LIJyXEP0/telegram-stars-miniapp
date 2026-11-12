from __future__ import annotations

from app.fulfillment.base import Delivery, FulfillmentError, Quote


class FragmentContractUnverified(FulfillmentError):
    """Raised until an owner supplies a verifiable, seedless provider contract."""


class FragmentProvider:
    """Safe fail-closed adapter; no request is sent to an undocumented endpoint."""

    def __init__(self) -> None:
        self.enabled = False

    async def check_recipient(self, username: str, product: str, quantity: int) -> bool:
        raise FragmentContractUnverified(
            "Fragment recipient-check contract is not verified; production checkout is disabled"
        )

    async def quote(self, product: str, quantity: int) -> Quote:
        raise FragmentContractUnverified(
            "Fragment quote contract is not verified; production checkout is disabled"
        )

    async def deliver(
        self, username: str, product: str, quantity: int, idempotency_key: str
    ) -> Delivery:
        raise FragmentContractUnverified(
            "Fragment delivery contract is not verified; no seed is sent to any HTTP service"
        )

    async def status(self, request_id: str) -> Delivery:
        raise FragmentContractUnverified(
            "Fragment status contract is not verified; manual reconciliation is required"
        )
