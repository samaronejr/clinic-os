"""Verified refund callbacks retain one fact; Finance posts one linked reversal."""

from __future__ import annotations

import hmac
import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.models import JournalEntry, SettlementAllocation
from apps.billing.psp_contracts import FIXTURE_PROVIDER, PaymentStatus
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_models import PspEvidence
from apps.billing.psp_posting import apply_psp_evidence
from apps.billing.psp_reconciliation import receive_psp_event, reconcile_psp_intent
from apps.billing.psp_services import prepare_psp_charge, prepare_psp_outgoing
from apps.billing.psp_worker import register_payment_provider
from apps.billing.refunds import approve_refund, request_refund
from apps.billing.security import finance_session
from apps.core import integration

from billing.journal_support import (
    FinanceSetup,
    finance_scope,
    finance_setup,
    fresh_finance_session,
    invoice_terms,
)
from billing.test_psp_reconciliation import sandbox
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)
SECRET = b"synthetic-refund-fixture"


def test_replayed_refund_callback_requires_finance_and_reverses_repasse_once(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox(finance_setup, settings)
    monkeypatch.setattr(integration, "_dispatch", lambda operation_id: None)
    terms = invoice_terms(finance_setup)
    key = fresh_finance_session(finance_setup)
    with finance_scope(finance_setup):
        charge = prepare_psp_charge(
            clinic_id=finance_setup.clinic_id, invoice_id=terms.invoice_id, method="pix"
        )
    register_payment_provider(
        RecordedPaymentProvider(
            secret=SECRET,
            records={
                charge.pk: PaymentStatus(
                    "fixture-original-57", "settled", 10000, "BRL", 2
                )
            },
        )
    )
    with runtime_role():
        assert reconcile_psp_intent(intent_id=charge.pk) == "verified"
    with finance_scope(finance_setup):
        paid = PspEvidence.objects.get(intent=charge, decision="verified")
        apply_psp_evidence(clinic_id=finance_setup.clinic_id, evidence_id=paid.pk)
        allocation = SettlementAllocation.objects.get(command_id=charge.pk)
        refund = request_refund(
            clinic_id=finance_setup.clinic_id,
            allocation_id=allocation.pk,
            amount_minor=5000,
            command_id=uuid4(),
        )
        with finance_session(clinic_id=finance_setup.clinic_id, session_key=key):
            approve_refund(clinic_id=finance_setup.clinic_id, refund_id=refund.pk)
            outgoing = prepare_psp_outgoing(
                clinic_id=finance_setup.clinic_id, kind="refund", target_id=refund.pk
            )
    register_payment_provider(
        RecordedPaymentProvider(
            secret=SECRET,
            records={
                outgoing.pk: PaymentStatus(
                    "fixture-refund-57", "confirmed", 5000, "BRL", 2
                )
            },
        )
    )
    with runtime_role():
        assert reconcile_psp_intent(intent_id=outgoing.pk) == "verified"
        body = json.dumps(
            {
                "event_id": "fixture-refund-event-57",
                "provider_reference": "fixture-refund-57",
                "status": "confirmed",
                "amount_minor": 5000,
                "currency": "BRL",
                "version": 2,
                "tenant_id": "ignored",
            }
        ).encode()
        headers = {"x-fixture-psp-signature": hmac.digest(SECRET, body, "sha256").hex()}
        assert (
            receive_psp_event(provider=FIXTURE_PROVIDER, headers=headers, body=body)
            == "verified"
        )
        assert (
            receive_psp_event(provider=FIXTURE_PROVIDER, headers=headers, body=body)
            == "duplicate"
        )
    with finance_scope(finance_setup):
        refund.refresh_from_db()
        assert refund.state == "sending"
        evidence = PspEvidence.objects.get(intent=outgoing, source="callback")
        with finance_session(clinic_id=finance_setup.clinic_id, session_key=key):
            first = apply_psp_evidence(
                clinic_id=finance_setup.clinic_id, evidence_id=evidence.pk
            )
            second = apply_psp_evidence(
                clinic_id=finance_setup.clinic_id, evidence_id=evidence.pk
            )
        assert first == second == refund.pk
        refund.refresh_from_db()
        assert refund.state == "confirmed"
        assert refund.repasse_reversed_minor == 1500
        assert (
            JournalEntry.objects.filter(
                source_kind="refund_confirmed", source_id=refund.pk
            ).count()
            == 1
        )
        assert (
            JournalEntry.objects.filter(
                source_kind="repasse_reversal", source_id=refund.pk
            ).count()
            == 1
        )
