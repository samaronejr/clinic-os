"""Real-role PSP/book integration regressions; source-only lanes do not run these."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.models import JournalEntry, JournalSettlement, SettlementAllocation
from apps.billing.psp_contracts import FIXTURE_PROVIDER, PaymentStatus
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_posting import apply_psp_evidence
from apps.billing.psp_reconciliation import reconcile_psp_intent
from apps.billing.psp_services import prepare_psp_charge, set_psp_policy
from apps.billing.psp_storage import PspEvidence
from apps.billing.psp_worker import register_payment_provider
from apps.core import integration
from apps.providers import lifecycle
from apps.tenancy.db import tenant_context

from billing.journal_support import (
    FinanceSetup,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from provider_gate_support import APPROVER

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def sandbox(setup: FinanceSetup, settings: SettingsWrapper) -> None:
    settings.BILLING_SYNTHETIC_PSP = True
    with owner_context(setup.graph.organization_a):
        version = lifecycle.propose_version(
            "pix",
            clinic_id=setup.clinic_id,
            version_input=lifecycle.VersionInput(
                provider=FIXTURE_PROVIDER, environment="sandbox", account="fixture"
            ),
        )
        lifecycle.approve_version("pix", clinic_id=setup.clinic_id, decision=APPROVER)
        version.refresh_from_db()
        version.state = "sandbox"
        version.save(update_fields=("state", "updated_at"))
    with (
        runtime_role(),
        tenant_context(setup.graph.shared_user, setup.graph.organization_a),
    ):
        set_psp_policy(
            clinic_id=setup.clinic_id, max_installments=3, command_id=uuid4()
        )


def test_verified_fixture_posts_gross_once_with_repasse_before_fees(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox(finance_setup, settings)
    monkeypatch.setattr(integration, "_dispatch", lambda operation_id: None)
    terms = invoice_terms(finance_setup)
    with finance_scope(finance_setup):
        intent = prepare_psp_charge(
            clinic_id=finance_setup.clinic_id, invoice_id=terms.invoice_id, method="pix"
        )
        assert not JournalSettlement.objects.exists()
    register_payment_provider(
        RecordedPaymentProvider(
            secret=b"synthetic-only",
            records={
                intent.pk: PaymentStatus(
                    "fixture-paid-57", "settled", 10000, "BRL", 2, 100
                )
            },
        )
    )
    with runtime_role():
        assert reconcile_psp_intent(intent_id=intent.pk) == "verified"
    with finance_scope(finance_setup):
        evidence = PspEvidence.objects.get(intent=intent, decision="verified")
        first = apply_psp_evidence(
            clinic_id=finance_setup.clinic_id, evidence_id=evidence.pk
        )
        second = apply_psp_evidence(
            clinic_id=finance_setup.clinic_id, evidence_id=evidence.pk
        )
        assert first == second
        assert JournalSettlement.objects.count() == 1
        assert SettlementAllocation.objects.get().repasse_minor == 3000
        assert JournalEntry.objects.filter(source_kind="collection").count() == 1


def test_amount_mismatch_is_retained_and_cannot_post(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox(finance_setup, settings)
    monkeypatch.setattr(integration, "_dispatch", lambda operation_id: None)
    terms = invoice_terms(finance_setup)
    with finance_scope(finance_setup):
        intent = prepare_psp_charge(
            clinic_id=finance_setup.clinic_id, invoice_id=terms.invoice_id, method="pix"
        )
    register_payment_provider(
        RecordedPaymentProvider(
            secret=b"synthetic-only",
            records={
                intent.pk: PaymentStatus("fixture-bad-57", "settled", 9999, "BRL", 2)
            },
        )
    )
    with runtime_role():
        assert reconcile_psp_intent(intent_id=intent.pk) == "quarantined"
    with finance_scope(finance_setup):
        assert not JournalSettlement.objects.exists()
        assert PspEvidence.objects.get(intent=intent).decision == "quarantined"
