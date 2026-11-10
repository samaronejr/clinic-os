"""Pure PSP contract tests; no Django startup, database or network is required."""

from __future__ import annotations

import hashlib
import hmac
import json
import multiprocessing
import os
import signal
from dataclasses import asdict, replace
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from apps.billing.psp_contracts import (
    PaymentAuthenticationError,
    PaymentInputError,
    PaymentRequest,
    PaymentStatus,
    StatementLine,
)
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_flow import dispatch_payment, receive_event
from apps.billing.psp_policy import (
    classify_payment,
    match_statement,
    validate_installments,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from multiprocessing.sharedctypes import Synchronized

KEY = UUID("10000000-0000-0000-0000-000000000057")
TARGET = UUID("20000000-0000-0000-0000-000000000057")
SECRET = b"synthetic-psp-fixture-authentication"
REQUEST = PaymentRequest(KEY, "charge", TARGET, "pix", 10000)
PAID = PaymentStatus("fixture-psp-57", "settled", 10000, "BRL", 2, 100)


def signed_event(**changes: object) -> tuple[dict[str, str], bytes]:
    payload = {
        "event_id": "fixture-event-57",
        "provider_reference": PAID.provider_reference,
        "status": PAID.status,
        "amount_minor": PAID.amount_minor,
        "currency": PAID.currency,
        "version": PAID.version,
        **changes,
    }
    body = json.dumps(payload, sort_keys=True).encode()
    return {"x-fixture-psp-signature": hmac.digest(SECRET, body, "sha256").hex()}, body


def provider() -> RecordedPaymentProvider:
    return RecordedPaymentProvider(secret=SECRET, records={KEY: PAID})


class EventStore:
    def __init__(self, request: PaymentRequest = REQUEST) -> None:
        self.request = request
        self.resolved = 0
        self.recorded: list[str] = []

    def resolve(self, provider: str, reference: str) -> PaymentRequest | None:
        self.resolved += 1
        return self.request if reference == PAID.provider_reference else None

    def record(
        self,
        request: PaymentRequest,
        event_id: str,
        authoritative: PaymentStatus | None,
        decision: str,
    ) -> str:
        if event_id in self.recorded:
            return "duplicate"
        self.recorded.append(event_id)
        return decision


def test_forged_raw_body_cannot_reach_scope_resolution() -> None:
    headers, body = signed_event()
    store = EventStore()
    with pytest.raises(PaymentAuthenticationError):
        receive_event(
            provider=provider(),
            headers=headers,
            body=body + b" ",
            store=store,
        )
    assert store.resolved == 0
    assert not store.recorded


def test_callback_ignores_payload_tenant_and_replay_converges() -> None:
    headers, body = signed_event(
        tenant_id="forged", organization_id="forged", clinic_id="forged"
    )
    store = EventStore()
    assert (
        receive_event(provider=provider(), headers=headers, body=body, store=store)
        == "verified"
    )
    assert (
        receive_event(provider=provider(), headers=headers, body=body, store=store)
        == "duplicate"
    )
    assert len(store.recorded) == 1


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"amount_minor": 9999}, "quarantined"),
        ({"currency": "USD"}, "quarantined"),
        ({"status": "pending", "version": 1}, "superseded"),
        ({"status": "reversed"}, "quarantined"),
    ],
)
def test_callback_disagreements_never_verify(
    changes: Mapping[str, object], expected: str
) -> None:
    headers, body = signed_event(**changes)
    assert (
        receive_event(
            provider=provider(), headers=headers, body=body, store=EventStore()
        )
        == expected
    )


def test_provider_lookup_failure_is_unknown_not_permission_to_send() -> None:
    response = dispatch_payment(
        provider=RecordedPaymentProvider(secret=SECRET, records={}),
        request=REQUEST,
        recovery=True,
    )
    assert response is None


def test_charge_creation_is_pending_and_never_a_settlement() -> None:
    response = dispatch_payment(
        provider=RecordedPaymentProvider(secret=SECRET, records={}),
        request=REQUEST,
    )
    assert response is not None
    assert response.status == "pending"
    assert response.payment_code.startswith("SYNTHETIC-NOT-PAYABLE|")
    assert classify_payment(REQUEST, response) == "recorded"


def test_card_link_uses_an_invalid_host_and_frozen_installments() -> None:
    request = replace(REQUEST, method="card", installments=3)
    response = provider().create_charge(request=request)
    assert response.payment_url.startswith("https://payments.invalid/")
    assert not response.payment_code
    assert response.status == "pending"


@pytest.mark.parametrize("installments", [True, 0, -1, 4, 1.0])
def test_installments_reject_values_outside_exact_clinic_policy(
    installments: int,
) -> None:
    with pytest.raises(PaymentInputError):
        validate_installments("card", installments, maximum=3)


def test_pix_cannot_installment_even_when_card_policy_allows_it() -> None:
    with pytest.raises(PaymentInputError):
        validate_installments("pix", 2, maximum=12)


@pytest.mark.parametrize("field", ["provider_reference", "amount_minor", "currency"])
def test_statement_match_requires_all_three_exact_fields(field: str) -> None:
    line = StatementLine(
        "fixture-statement-57", PAID.provider_reference, 10000, "BRL", 100
    )
    if field == "provider_reference":
        line = replace(line, provider_reference="different")
    elif field == "amount_minor":
        line = replace(line, amount_minor=9999)
    else:
        line = replace(line, currency="USD")
    assert match_statement(REQUEST, PAID, line) == "quarantined"


def test_statement_preserves_gross_collection_and_separate_psp_fee() -> None:
    line = StatementLine(
        "fixture-statement-57", PAID.provider_reference, 10000, "BRL", 100
    )
    assert match_statement(REQUEST, PAID, line) == "matched"
    assert asdict(line)["amount_minor"] == 10000
    assert line.fee_minor == 100


class KillAfterRemoteEffect(RecordedPaymentProvider):
    def __init__(self, effects: Synchronized[int]) -> None:
        super().__init__(secret=SECRET, records={KEY: PAID})
        self.effects = effects

    def lookup(self, *, request: PaymentRequest) -> PaymentStatus | None:
        return PAID if self.effects.value else None

    def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
        self.effects.value += 1
        os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError


def _send_and_die(effects: Synchronized[int]) -> None:
    dispatch_payment(provider=KillAfterRemoteEffect(effects), request=REQUEST)


def test_sigkill_after_remote_effect_recovers_without_a_second_create() -> None:
    context = multiprocessing.get_context("fork")
    effects = context.Value("i", 0)
    worker = context.Process(target=_send_and_die, args=(effects,))
    worker.start()
    worker.join(timeout=10)
    try:
        assert worker.exitcode == -signal.SIGKILL
        assert effects.value == 1
        assert (
            dispatch_payment(
                provider=KillAfterRemoteEffect(effects), request=REQUEST, recovery=True
            )
            == PAID
        )
        assert effects.value == 1
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=10)
        worker.close()


def test_callback_signature_is_exact_raw_bytes_not_reencoded_json() -> None:
    headers, body = signed_event()
    changed = json.dumps(json.loads(body), indent=2).encode()
    assert hashlib.sha256(body).digest() != hashlib.sha256(changed).digest()
    with pytest.raises(PaymentAuthenticationError):
        provider().authenticate(headers=headers, body=changed)
