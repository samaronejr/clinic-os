"""Control cases for the post-dispatch system principal."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.billing.psp_observer_key import observer_key
from apps.billing.psp_worker import register_payment_provider
from apps.comms.models import IntegrationOperation
from apps.core import integration
from apps.identity.models import User
from django.db import connection, transaction

from billing.journal_support import FinanceSetup, finance_scope, finance_setup
from billing.test_psp_offboarding import AcceptedBeforeWorkerLoss, WorkerLostError
from billing.test_psp_review import prepared_payment
from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    from apps.billing.psp_models import PspIntent

__all__ = ("finance_setup", "prepared_payment")
pytestmark = pytest.mark.django_db(transaction=True)


def test_observer_needs_a_dispatched_intent_and_current_clinic_credential(
    prepared_payment: tuple[FinanceSetup, PspIntent],
    synthetic_secret_backend: Path,
) -> None:
    setup, intent = prepared_payment
    provider = AcceptedBeforeWorkerLoss()
    register_payment_provider(provider)
    with finance_scope(setup):
        operation = IntegrationOperation.objects.get(subject_id=intent.pk)
    proof = observer_key(clinic_id=setup.clinic_id)
    query = "SELECT id FROM clinic_app.billing_psp_observer_request(%s,%s)"
    with runtime_role(), connection.cursor() as cursor:
        cursor.execute(query, [intent.pk, proof])
        assert cursor.fetchone() is None
    with runtime_role(), pytest.raises(WorkerLostError):
        integration.execute_operation(operation.pk)
    with owner_context(intent.organization_id):
        assert (
            User.objects.filter(pk=intent.requested_by_id).update(is_active=False) == 1
        )
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        for invalid in (bytes(32), observer_key(clinic_id=setup.graph.clinic_b)):
            cursor.execute(query, [intent.pk, invalid])
            assert cursor.fetchone() is None
        cursor.execute(query, [intent.pk, proof])
        assert cursor.fetchone() == (intent.pk,)
    with (
        permission_context(setup.graph, intent.requested_by_id),
        connection.cursor() as cursor,
    ):
        cursor.execute(query, [intent.pk, proof])
        assert cursor.fetchone() is None
    (synthetic_secret_backend / "psp-observer-v1.secret").unlink()
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "reconcile"
    with owner_context(intent.organization_id):
        operation.refresh_from_db()
        assert (
            operation.status,
            operation.attempt_count,
            operation.provider_reference,
        ) == ("in_progress", 1, None)
    assert provider.lookups == provider.effects == [intent.pk]


def test_offboarded_requester_with_only_pending_work_cannot_send(
    prepared_payment: tuple[FinanceSetup, PspIntent],
) -> None:
    setup, intent = prepared_payment
    provider = AcceptedBeforeWorkerLoss()
    register_payment_provider(provider)
    with finance_scope(setup):
        operation = IntegrationOperation.objects.get(subject_id=intent.pk)
    with owner_context(intent.organization_id):
        assert (
            User.objects.filter(pk=intent.requested_by_id).update(is_active=False) == 1
        )
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "cancelled"
    assert provider.lookups == provider.effects == []
