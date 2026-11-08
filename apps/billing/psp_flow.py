"""Framework-free PSP orchestration shared by outbox and callback boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from apps.billing.psp_contracts import PaymentLookupError
from apps.billing.psp_policy import classify_payment, validate_request

if TYPE_CHECKING:
    from collections.abc import Mapping

    from apps.billing.psp_contracts import (
        PaymentProvider,
        PaymentRequest,
        PaymentStatus,
    )


class PaymentEventStore(Protocol):
    """Resolve stored scope only after authentication and atomically record facts."""

    def resolve(self, provider: str, reference: str) -> PaymentRequest | None:
        """Resolve only the stored provider/reference pair."""
        ...

    def record(
        self,
        request: PaymentRequest,
        event_id: str,
        authoritative: PaymentStatus | None,
        decision: str,
    ) -> str:
        """Deduplicate and revalidate newer evidence under the book lock."""
        ...


def dispatch_payment(
    *, provider: PaymentProvider, request: PaymentRequest, recovery: bool = False
) -> PaymentStatus | None:
    """Look up the same key first; recovery never sends a second money effect."""
    validate_request(request)
    known = provider.lookup(request=request)
    if known is not None or recovery:
        return known
    if request.kind == "charge":
        return provider.create_charge(request=request)
    if request.kind == "refund":
        return provider.refund(request=request)
    return provider.payout(request=request)


def receive_event(
    *,
    provider: PaymentProvider,
    headers: Mapping[str, str],
    body: bytes,
    store: PaymentEventStore,
) -> str:
    """Authenticate raw bytes, resolve stored scope, then independently verify."""
    event = provider.authenticate(headers=headers, body=body)
    request = store.resolve(provider.provider, event.provider_reference)
    if request is None:
        return "rejected"
    try:
        authoritative = provider.lookup(request=request)
    except PaymentLookupError:
        authoritative = None
    decision = classify_payment(request, authoritative, event)
    return store.record(request, event.event_id, authoritative, decision)
