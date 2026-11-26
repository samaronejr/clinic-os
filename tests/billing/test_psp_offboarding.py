"""A committed send survives worker loss and the requester's offboarding."""

from __future__ import annotations

from uuid import UUID

import pytest
from apps.audit.models import AuditEvent
from apps.audit.services import verify_chain
from apps.billing.psp_contracts import PaymentRequest, PaymentStatus
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_models import PspBinding, PspEvidence, PspIntent, PspReceipt
from apps.billing.psp_reconciliation import reconcile_psp_intent
from apps.billing.psp_worker import register_payment_provider
from apps.comms.models import IntegrationOperation
from apps.core import integration
from apps.identity.models import User
from apps.tenancy.db import tenant_context

from billing.journal_support import FinanceSetup, finance_scope, finance_setup
from billing.test_psp_review import prepared_payment
from identity.permission_support import owner_context
from patient_service_support import runtime_role

__all__ = ("finance_setup", "prepared_payment")
pytestmark = pytest.mark.django_db(transaction=True)
PRINCIPAL = UUID("681a59f4-1475-4b9d-8bf5-73a61fd285e1")


class WorkerLostError(Exception):
    pass


class AcceptedBeforeWorkerLoss(RecordedPaymentProvider):
    def __init__(self) -> None:
        super().__init__(secret=b"synthetic-worker-loss", records={})
        self.lookups: list[UUID] = []
        self.effects: list[UUID] = []
        self.available = True

    def lookup(self, *, request: PaymentRequest) -> PaymentStatus | None:
        self.lookups.append(request.idempotency_key)
        if self.available and request.idempotency_key in self.effects:
            return PaymentStatus(
                "fixture-offboarding", "settled", request.amount_minor, "BRL", 2
            )
        return None

    def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
        self.effects.append(request.idempotency_key)
        # The claim has committed and the fixture PSP has accepted, but neither
        # the worker return path nor the binding/evidence transaction can run.
        raise WorkerLostError


@pytest.mark.parametrize("entry", ["worker", "reconciler"])
def test_accepted_effect_recovers_without_the_requesters_identity(
    prepared_payment: tuple[FinanceSetup, PspIntent], entry: str
) -> None:
    setup, intent = prepared_payment
    client = AcceptedBeforeWorkerLoss()
    register_payment_provider(client)
    with finance_scope(setup):
        operation = IntegrationOperation.objects.get(subject_id=intent.pk)
    with runtime_role(), pytest.raises(WorkerLostError):
        integration.execute_operation(operation.pk)
    with finance_scope(setup):
        operation.refresh_from_db()
        assert (
            operation.status,
            operation.attempt_count,
            operation.provider_reference,
        ) == ("in_progress", 1, None)
        assert not PspBinding.objects.filter(intent=intent).exists()
        assert not PspEvidence.objects.filter(intent=intent).exists()
    with owner_context(intent.organization_id):
        assert (
            User.objects.filter(pk=intent.requested_by_id).update(is_active=False) == 1
        )
    client.available = False
    with runtime_role():
        if entry == "worker":
            assert integration.execute_operation(operation.pk) == "reconcile"
        else:
            assert reconcile_psp_intent(intent_id=intent.pk) == "unknown"
        client.available = True
        if entry == "reconciler":
            assert reconcile_psp_intent(intent_id=intent.pk) == "verified"
        # Even after standalone observation the outbox takes no fresh send.
        assert integration.execute_operation(operation.pk) == "succeeded"
    # The surviving worker/reconciler used only the committed key.
    assert client.effects == [intent.pk]
    assert client.lookups == [intent.pk] * (3 if entry == "worker" else 4)
    with owner_context(intent.organization_id):
        operation.refresh_from_db()
        assert operation.actor_id == intent.requested_by_id
        assert (
            operation.status,
            operation.attempt_count,
            operation.provider_reference,
        ) == ("succeeded", 1, "fixture-offboarding")
    # Use a different, authorized reader; never reactivate the requester.
    with (
        runtime_role(),
        tenant_context(setup.graph.shared_user, intent.organization_id),
    ):
        evidence = PspEvidence.objects.get(intent=intent, decision="verified")
        assert (
            PspBinding.objects.get(intent=intent).provider_reference
            == "fixture-offboarding"
        )
        assert not PspReceipt.objects.filter(intent=intent).exists()
    with owner_context(intent.organization_id):
        event = AuditEvent.objects.get(
            event_type="billing.psp.evidence", affected_record_id=str(evidence.pk)
        )
        completed = AuditEvent.objects.get(
            event_type="comms.operation.succeeded", affected_record_id=str(operation.pk)
        )
        assert completed.actor_user_id == event.actor_user_id == PRINCIPAL
        assert intent.requested_by_id != PRINCIPAL
        assert completed.payload == {
            "clinic_id": str(setup.clinic_id),
            "object_verb": "succeeded",
            "request_id": str(operation.pk),
        }
        assert verify_chain(intent.organization_id).valid
