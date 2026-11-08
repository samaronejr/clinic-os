"""Observe the actual patient and durable-worker PSP authority channels."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.billing.psp_contracts import PaymentRequest
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_presentation import patient_psp_payment
from apps.billing.psp_storage import PspScope
from apps.billing.psp_worker import PreparedPayment, PspSendAdapter, _eligible
from apps.comms.adapters import OperationScope, PermanentSendError
from django.utils import timezone

from identity.nonstaff_differential import DifferentialProbe

if TYPE_CHECKING:
    from identity.nonstaff_subjects import NonstaffSubjects


def _missing_send(data: NonstaffSubjects) -> bool:
    """Exercise a real stored-authority lookup with an unknown immutable intent."""
    world = data.legacy
    operation, intent = uuid4(), uuid4()
    prepared = PreparedPayment(
        operation,
        PaymentRequest(intent, "charge", data.op.invoice.pk, "pix", 100),
        PspScope(intent, world.graph.organization_a, world.clinic, data.actor.pk),
        uuid4(),
        timezone.now() + timedelta(minutes=30),
    )
    adapter = PspSendAdapter(
        RecordedPaymentProvider(secret=b"synthetic-census-psp", records={})
    )
    try:
        adapter.send(prepared, operation_id=operation)
    except PermanentSendError:
        return False
    return True


def psp_probes(data: NonstaffSubjects) -> list[DifferentialProbe]:
    """Keep worker staff reads observed instead of exempting them as providers."""
    world = data.legacy
    return [
        DifferentialProbe(
            "apps.billing.psp_presentation.patient_psp_payment",
            lambda: patient_psp_payment(invoice_id=data.op.invoice.pk),
            lambda result: result is None,
            patient_session=data.op.patient_session,
            patient_context=True,
            owns_transaction=True,
        ),
        DifferentialProbe(
            "apps.billing.psp_worker._eligible",
            lambda: _eligible(
                OperationScope(
                    uuid4(), world.graph.organization_a, world.clinic, data.actor.pk
                )
            ),
            expected=False,
        ),
        DifferentialProbe(
            "apps.billing.psp_worker.PspSendAdapter.send",
            lambda: _missing_send(data),
            expected=False,
            owns_transaction=True,
        ),
    ]
