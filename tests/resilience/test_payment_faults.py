"""Todo 72: a faulted payment provider never marks paid or charges twice."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.adapters import PixUnavailableError
from apps.billing.models import Invoice, PixCharge, Receipt
from apps.billing.psp_contracts import PaymentRequest, PaymentStatus
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_models import PspEvidence
from apps.billing.psp_services import prepare_psp_charge
from apps.billing.psp_worker import register_payment_provider
from apps.billing.services import confirm_settlement
from apps.comms.models import IntegrationOperation
from apps.core import integration
from apps.tenancy.db import tenant_context

from billing.journal_support import (
    FinanceSetup,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from billing.test_psp_reconciliation import sandbox
from patient_service_support import runtime_role
from renewal.test_pix_adapter import complete, issued, prepare

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from scheduling.appointment_service_support import AppointmentSetup

__all__ = ("finance_setup", "issued")
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("mode", ["down", "slow", "ambiguous"])
def test_pix_outage_stores_no_code_and_manual_settlement_still_works(
    issued: tuple[AppointmentSetup, Invoice],
    settings: SettingsWrapper,
    mode: str,
) -> None:
    settings.CLINIC_DATA_MODE = "synthetic"
    settings.BILLING_SYNTHETIC_PIX = True
    setup, invoice = issued
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        operation = prepare(setup, invoice)
    settings.CLINIC_SYNTHETIC_FAULTS = {"payment": mode}
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
        pytest.raises(PixUnavailableError),
    ):
        complete(setup, invoice, operation)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert not PixCharge.objects.filter(operation=operation).exists()
        assert Invoice.objects.get(pk=invoice.pk).state == Invoice.State.OPEN
        # Ordinary billing keeps working: the manager confirms a manual payment.
        receipt = confirm_settlement(
            clinic_id=setup.clinic_id,
            invoice_id=invoice.pk,
            confirmation_reference=uuid4(),
            amount_minor=invoice.amount_minor,
        )
        assert Receipt.objects.filter(pk=receipt.pk).exists()
        assert not PixCharge.objects.exists()


class EffectThenFault(RecordedPaymentProvider):
    """The recorded adapter whose provider keeps a charge before any drill fault."""

    def __init__(self) -> None:
        super().__init__(secret=b"synthetic-fixture", records={})
        self.creates = 0

    def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
        self.creates += 1
        self._records[request.idempotency_key] = PaymentStatus(
            f"fixture-{request.idempotency_key.hex}",
            "pending",
            request.amount_minor,
            "BRL",
            1,
        )
        return super().create_charge(request=request)


@pytest.mark.parametrize(
    ("mode", "creates", "recovered"),
    [("down", 0, "reconcile"), ("slow", 1, "succeeded"), ("ambiguous", 1, "succeeded")],
)
def test_psp_faults_stay_unknown_and_recovery_only_looks_up(  # noqa: PLR0913
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    creates: int,
    recovered: str,
) -> None:
    sandbox(finance_setup, settings)
    monkeypatch.setattr(integration, "_dispatch", lambda operation_id: None)
    terms = invoice_terms(finance_setup)
    client = EffectThenFault()
    register_payment_provider(client)
    with finance_scope(finance_setup):
        intent = prepare_psp_charge(
            clinic_id=finance_setup.clinic_id, invoice_id=terms.invoice_id, method="pix"
        )
        operation = IntegrationOperation.objects.get(subject_id=intent.pk)
    settings.CLINIC_SYNTHETIC_FAULTS = {"payment": mode}
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "reconcile"
    with finance_scope(finance_setup):
        operation.refresh_from_db()
        assert operation.status == "in_progress"
        assert list(
            PspEvidence.objects.filter(intent=intent).values_list("decision", flat=True)
        ) == ["unknown"]
    # Recovery after the provider answers again is lookup-only: no second effect.
    settings.CLINIC_SYNTHETIC_FAULTS = {}
    with runtime_role():
        assert integration.execute_operation(operation.pk) == recovered
    with finance_scope(finance_setup):
        operation.refresh_from_db()
        assert operation.status == (
            "in_progress" if recovered == "reconcile" else "succeeded"
        )
        assert operation.attempt_count == 1
        assert not Invoice.objects.filter(
            pk=terms.invoice_id, state=Invoice.State.PAID
        ).exists()
    assert client.creates == creates
