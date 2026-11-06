"""PSP payment visibility follows the canonical released-charge alias authority."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.compensation import prepare_invoice_terms
from apps.billing.models import Invoice
from apps.billing.money_types import NO_ADJUSTMENTS
from apps.billing.presentation import patient_charge
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_presentation import PatientPspPayment, patient_psp_payment
from apps.billing.psp_services import prepare_psp_charge
from apps.billing.psp_worker import register_payment_provider
from apps.billing.services import create_invoice, issue_invoice, release_invoice
from apps.comms.models import IntegrationOperation
from apps.core import integration
from apps.intake import services
from apps.intake.patient_access import patient_session_context
from django.template.loader import render_to_string
from django.utils.translation import gettext

from billing.journal_support import (
    FinanceSetup,
    finance_scope,
    finance_setup,
    fresh_finance_session,
)
from billing.test_psp_reconciliation import sandbox
from patient_service_support import runtime_role
from patients.duplicate_support import _pair

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


@pytest.mark.parametrize("method", ["pix", "card", "empty"])
def test_native_psp_partial_labels_nonpayable_instructions_and_installments(
    method: str,
) -> None:
    payment = PatientPspPayment(
        "SYNTHETIC-NOT-PAYABLE|fixture" if method == "pix" else "",
        "https://payments.invalid/fixture" if method == "card" else "",
        3 if method == "card" else 1,
        10000,
        None,
        None,
    )
    html = render_to_string(
        "billing/partials/psp_instructions.html", {"psp_payment": payment}
    )
    if method == "empty":
        assert "data-psp-instructions" not in html
        return
    assert (
        gettext(
            "Recorded sandbox rehearsal. No real payment or card data is collected."
        )
        in html
    )
    if method == "pix":
        assert "SYNTHETIC-NOT-PAYABLE|fixture" in html
        assert "readonly" in html
        assert gettext("Non-payable PIX rehearsal code") in html
    else:
        assert 'href="https://payments.invalid/fixture"' in html
        assert 'rel="noreferrer noopener"' in html
        assert (
            gettext("Installments agreed by the clinic: %(count)s.") % {"count": 3}
        ) in html


def test_survivor_sees_psp_instructions_until_unmerge_without_reparenting_money(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox(finance_setup, settings)
    monkeypatch.setattr(integration, "_dispatch", lambda operation_id: None)
    first, second, candidate = _pair(finance_setup.graph, services)
    key = fresh_finance_session(finance_setup)
    register_payment_provider(
        RecordedPaymentProvider(secret=b"synthetic-patient", records={})
    )
    with finance_scope(finance_setup):
        invoice = create_invoice(
            clinic_id=finance_setup.clinic_id,
            patient_id=second.patient.pk,
            amount_minor=10000,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=finance_setup.clinic_id,
            invoice_id=invoice.pk,
            expected_revision=invoice.revision,
        )
        release_invoice(clinic_id=finance_setup.clinic_id, invoice_id=invoice.pk)
        prepare_invoice_terms(
            clinic_id=finance_setup.clinic_id,
            invoice_id=invoice.pk,
            payer="patient",
            adjustments=NO_ADJUSTMENTS,
            payout_rule_id=None,
        )
        intent = prepare_psp_charge(
            clinic_id=finance_setup.clinic_id, invoice_id=invoice.pk, method="pix"
        )
        operation_id = IntegrationOperation.objects.get(subject_id=intent.pk).pk
        invitation = services.issue_invitation(
            clinic_id=finance_setup.clinic_id, enrollment_id=first.enrollment.pk
        )
    with runtime_role():
        session = services.redeem_invitation(finance_setup.clinic_id, invitation.secret)
        assert session is not None
        assert integration.execute_operation(operation_id) == "succeeded"
        with patient_session_context(session):
            assert patient_psp_payment(invoice_id=invoice.pk) is None
    with (
        finance_scope(finance_setup),
        services.duplicate_review_session(
            clinic_id=finance_setup.clinic_id, session_key=key
        ),
    ):
        decision = services.adjudicate_duplicate(
            clinic_id=finance_setup.clinic_id,
            candidate_id=candidate.pk,
            decision="merge",
            survivor_patient_id=first.patient.pk,
            field_choices={},
            reason="Synthetic retained PSP history",
        )
    with runtime_role(), patient_session_context(session):
        payment = patient_psp_payment(invoice_id=invoice.pk)
        assert payment is not None
        assert payment.payment_code.startswith("SYNTHETIC-NOT-PAYABLE|")
        charge = patient_charge(invoice_id=invoice.pk)
        assert charge is not None
        assert charge.charge.state == "pending"
    with (
        finance_scope(finance_setup),
        services.duplicate_review_session(
            clinic_id=finance_setup.clinic_id, session_key=key
        ),
    ):
        assert Invoice.objects.get(pk=invoice.pk).patient_id == second.patient.pk
        services.unmerge(
            clinic_id=finance_setup.clinic_id,
            merge_decision_id=decision.pk,
            reason="Synthetic PSP lineage reversal",
        )
        fresh_invitation = services.issue_invitation(
            clinic_id=finance_setup.clinic_id, enrollment_id=first.enrollment.pk
        )
    with runtime_role():
        fresh_session = services.redeem_invitation(
            finance_setup.clinic_id, fresh_invitation.secret
        )
        assert fresh_session is not None
        with patient_session_context(fresh_session):
            assert patient_psp_payment(invoice_id=invoice.pk) is None
