"""Real-role outbox ambiguity, callback replay and current-authority regressions."""

from __future__ import annotations

import hmac
import json
from typing import TYPE_CHECKING

import pytest
from apps.billing.psp_contracts import (
    FIXTURE_PROVIDER,
    PaymentAuthenticationError,
    PaymentRequest,
    PaymentStatus,
)
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_gate import psp_capability
from apps.billing.psp_models import PspEvidence
from apps.billing.psp_reconciliation import receive_psp_event
from apps.billing.psp_services import prepare_psp_charge
from apps.billing.psp_storage import DatabasePaymentEventStore
from apps.billing.psp_worker import register_payment_provider
from apps.comms.models import IntegrationOperation
from apps.core import integration

from billing.journal_support import (
    FinanceSetup,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from billing.test_psp_reconciliation import sandbox
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


class EffectThenTimeout(RecordedPaymentProvider):
    def __init__(self) -> None:
        super().__init__(secret=b"synthetic-fixture", records={})
        self.effects = 0
        self.known: PaymentStatus | None = None

    def lookup(self, *, request: PaymentRequest) -> PaymentStatus | None:
        return self.known

    def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
        self.effects += 1
        self.known = PaymentStatus(
            "fixture-effect-57", "settled", request.amount_minor, "BRL", 2
        )
        raise TimeoutError


def test_remote_timeout_keeps_in_progress_and_redelivery_uses_lookup_only(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox(finance_setup, settings)
    monkeypatch.setattr(integration, "_dispatch", lambda operation_id: None)
    terms = invoice_terms(finance_setup)
    client = EffectThenTimeout()
    register_payment_provider(client)
    with finance_scope(finance_setup):
        intent = prepare_psp_charge(
            clinic_id=finance_setup.clinic_id, invoice_id=terms.invoice_id, method="pix"
        )
        operation = IntegrationOperation.objects.get(subject_id=intent.pk)
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "reconcile"
    with finance_scope(finance_setup):
        operation.refresh_from_db()
        assert operation.status == "in_progress"
        assert PspEvidence.objects.get(intent=intent).decision == "unknown"
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "succeeded"
    with finance_scope(finance_setup):
        operation.refresh_from_db()
        assert operation.attempt_count == 1
        assert operation.provider_reference == "fixture-effect-57"
        assert (
            PspEvidence.objects.filter(intent=intent, decision="verified").count() == 1
        )
    assert client.effects == 1


def test_sandbox_is_not_real_and_setting_alone_never_authorizes_a_provider(
    finance_setup: FinanceSetup, settings: SettingsWrapper
) -> None:
    settings.BILLING_SYNTHETIC_PSP = True
    closed = psp_capability(clinic_id=finance_setup.clinic_id, method="pix")
    assert closed.sandbox_enabled is False
    assert closed.real_enabled is False
    sandbox(finance_setup, settings)
    allowed = psp_capability(clinic_id=finance_setup.clinic_id, method="pix")
    assert allowed.sandbox_enabled is True
    assert allowed.real_enabled is False
    settings.CLINIC_DATA_MODE = "live"
    assert not psp_capability(
        clinic_id=finance_setup.clinic_id, method="pix"
    ).sandbox_enabled


def test_live_mode_cannot_fall_back_to_recorded_callback_processing(
    settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = b"synthetic-live-refusal"
    register_payment_provider(RecordedPaymentProvider(secret=secret, records={}))
    settings.CLINIC_DATA_MODE = "live"
    settings.BILLING_SYNTHETIC_PSP = True
    body = json.dumps(
        {
            "event_id": "fixture-live-event",
            "provider_reference": "fixture-live-ref",
            "status": "pending",
            "amount_minor": 10000,
            "currency": "BRL",
            "version": 1,
        }
    ).encode()
    headers = {"x-fixture-psp-signature": hmac.digest(secret, body, "sha256").hex()}

    def forbidden_scope(
        self: object, provider: str, reference: str
    ) -> PaymentRequest | None:
        pytest.fail("live mode must not resolve a synthetic callback scope")

    monkeypatch.setattr(DatabasePaymentEventStore, "resolve", forbidden_scope)
    with pytest.raises(PaymentAuthenticationError):
        receive_psp_event(provider=FIXTURE_PROVIDER, headers=headers, body=body)
