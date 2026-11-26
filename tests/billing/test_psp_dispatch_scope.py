"""Only a clinic-bound machine credential can inspect dispatch state."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.psp_observer_key import observer_key
from apps.billing.psp_worker import register_payment_provider
from apps.comms.models import IntegrationOperation
from apps.core import integration
from django.db import connection

from billing.journal_support import FinanceSetup, finance_scope, finance_setup
from billing.test_psp_offboarding import AcceptedBeforeWorkerLoss, WorkerLostError
from billing.test_psp_review import prepared_payment
from identity.permission_support import permission_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from apps.billing.psp_models import PspIntent

__all__ = ("finance_setup", "prepared_payment")
pytestmark = pytest.mark.django_db(transaction=True)


def test_dispatch_uuid_helper_is_private() -> None:
    with runtime_role(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_function_privilege(current_user,"
            "'clinic_app.billing_psp_dispatched(uuid)','EXECUTE')"
        )
        assert cursor.fetchone() == (False,)


@pytest.mark.parametrize(
    "caller",
    ["wrong-clinic", "foreign-proof", "unknown", "non-observer", "mixed-staff"],
)
def test_dispatch_state_is_clinic_and_observer_bound(
    prepared_payment: tuple[FinanceSetup, PspIntent], caller: str
) -> None:
    setup, intent = prepared_payment
    register_payment_provider(AcceptedBeforeWorkerLoss())
    with finance_scope(setup):
        operation = IntegrationOperation.objects.get(subject_id=intent.pk)
    query = "SELECT clinic_app.billing_psp_dispatch_state(%s,%s,%s)"
    proof = observer_key(clinic_id=setup.clinic_id)
    with runtime_role(), connection.cursor() as cursor:
        cursor.execute(query, [intent.pk, setup.clinic_id, proof])
        assert cursor.fetchone() == (False,)
    with runtime_role(), pytest.raises(WorkerLostError):
        integration.execute_operation(operation.pk)
    with runtime_role(), connection.cursor() as cursor:
        cursor.execute(query, [intent.pk, setup.clinic_id, proof])
        assert cursor.fetchone() == (True,)
    context = (
        permission_context(setup.graph, intent.requested_by_id)
        if caller == "mixed-staff"
        else runtime_role()
    )
    with context, connection.cursor() as cursor:
        clinic = setup.graph.clinic_b if caller == "wrong-clinic" else setup.clinic_id
        # Change clinic and credential independently. Proving possession of the
        # real target's key still cannot authorize a different requested clinic.
        key = (
            None
            if caller == "non-observer"
            else (
                observer_key(clinic_id=setup.graph.clinic_b)
                if caller == "foreign-proof"
                else proof
            )
        )
        cursor.execute(
            query, [uuid4() if caller == "unknown" else intent.pk, clinic, key]
        )
        assert cursor.fetchone() == (None,)
        cursor.execute(query, [uuid4(), clinic, key])
        assert cursor.fetchone() == (None,)
