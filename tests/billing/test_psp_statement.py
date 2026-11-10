"""Statement facts reconcile receipts and fees without netting practitioner pay."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.billing.models import JournalEntry
from apps.billing.psp_contracts import PaymentStatus, StatementLine
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_models import PspEvidence
from apps.billing.psp_posting import apply_psp_evidence
from apps.billing.psp_reconciliation import reconcile_psp_statement
from apps.billing.psp_services import prepare_psp_charge
from apps.billing.psp_worker import register_payment_provider
from apps.core import integration

from billing.journal_support import (
    FinanceSetup,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from billing.test_accounting_policy import balance
from billing.test_psp_reconciliation import sandbox
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def test_statement_reimport_posts_one_net_bank_transfer_and_separate_fee(
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
    status = PaymentStatus("fixture-statement-charge", "settled", 10000, "BRL", 2, 100)
    register_payment_provider(
        RecordedPaymentProvider(
            secret=b"synthetic-statement",
            records={intent.pk: status},
            statements=(
                StatementLine(
                    "fixture-statement-line",
                    status.provider_reference,
                    10000,
                    "BRL",
                    100,
                ),
            ),
        )
    )
    with runtime_role():
        assert reconcile_psp_statement(intent_id=intent.pk) == ("matched",)
        assert reconcile_psp_statement(intent_id=intent.pk) == ("duplicate",)
    with finance_scope(finance_setup):
        evidence = PspEvidence.objects.get(intent=intent, source="statement")
        apply_psp_evidence(clinic_id=finance_setup.clinic_id, evidence_id=evidence.pk)
        apply_psp_evidence(clinic_id=finance_setup.clinic_id, evidence_id=evidence.pk)
        assert balance(finance_setup.book_id, "bank") == 9900
        assert balance(finance_setup.book_id, "psp_clearing") == 0
        assert balance(finance_setup.book_id, "psp_fees") == 100
        assert balance(finance_setup.book_id, "practitioner_payables") == -3000
        assert JournalEntry.objects.filter(source_kind="manual").count() == 1


def test_statement_disagreement_is_quarantined_with_its_actual_amount(
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
    status = PaymentStatus("fixture-statement-mismatch", "settled", 10000, "BRL", 2)
    register_payment_provider(
        RecordedPaymentProvider(
            secret=b"synthetic-statement",
            records={intent.pk: status},
            statements=(
                StatementLine(
                    "fixture-mismatch-line", status.provider_reference, 9999, "BRL"
                ),
            ),
        )
    )
    with runtime_role():
        assert reconcile_psp_statement(intent_id=intent.pk) == ("quarantined",)
    with finance_scope(finance_setup):
        evidence = PspEvidence.objects.get(intent=intent, source="statement")
        assert evidence.amount_minor == 9999
        assert evidence.decision == "quarantined"
        assert not JournalEntry.objects.filter(source_kind="collection").exists()
