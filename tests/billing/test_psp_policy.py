"""Pure callback adversaries and exact PSP boundaries, without Django or a DB."""

from __future__ import annotations

import hmac
from dataclasses import replace

import pytest
from apps.billing.psp_contracts import (
    PaymentAuthenticationError,
    PaymentInputError,
    PaymentLookupError,
    PaymentRequest,
    PaymentStatus,
)
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_flow import dispatch_payment, receive_event

from billing.test_psp_adapter import (
    KEY,
    PAID,
    REQUEST,
    SECRET,
    EventStore,
    provider,
    signed_event,
)


def test_replayed_refund_callback_retains_one_confirmed_fact() -> None:
    request = replace(
        REQUEST, kind="refund", original_reference="fixture-original-charge"
    )
    confirmed = replace(PAID, status="confirmed", fee_minor=0)
    client = RecordedPaymentProvider(secret=SECRET, records={KEY: confirmed})
    store = EventStore(request)
    headers, body = signed_event(status="confirmed")
    assert (
        receive_event(provider=client, headers=headers, body=body, store=store)
        == "verified"
    )
    assert (
        receive_event(provider=client, headers=headers, body=body, store=store)
        == "duplicate"
    )
    assert len(store.recorded) == 1


def test_status_lookup_timeout_never_attempts_another_effect() -> None:
    class Unavailable(RecordedPaymentProvider):
        def lookup(self, *, request: PaymentRequest) -> PaymentStatus | None:
            raise PaymentLookupError

        def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
            pytest.fail("lookup ambiguity must never send money")

    with pytest.raises(PaymentLookupError):
        dispatch_payment(
            provider=Unavailable(secret=SECRET, records={}), request=REQUEST
        )


@pytest.mark.parametrize("amount", [True, 0, -1, 2**63, 100.0])
def test_exact_money_boundary_rejects_non_centavo_inputs(amount: int) -> None:
    with pytest.raises(PaymentInputError):
        dispatch_payment(
            provider=provider(), request=replace(REQUEST, amount_minor=amount)
        )


@pytest.mark.parametrize(
    "body",
    [
        b'{"event_id":true}',
        b'{"event_id":"fixture","amount_minor":true}',
        b"[]",
        b"\xff",
    ],
)
def test_signed_but_malformed_events_never_resolve_scope(body: bytes) -> None:
    store = EventStore()
    headers = {"x-fixture-psp-signature": hmac.digest(SECRET, body, "sha256").hex()}
    with pytest.raises(PaymentAuthenticationError):
        receive_event(provider=provider(), headers=headers, body=body, store=store)
    assert store.resolved == 0
