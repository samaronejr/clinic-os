from __future__ import annotations

from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from apps.scheduling.locks import acquire_advisory_locks, clinic_lock_key
from apps.scheduling.models import Appointment, AvailabilityBlock
from apps.scheduling.services import (
    AvailabilityHasAppointmentsError,
    retire_availability,
)
from apps.tenancy.db import tenant_context
from django.db import transaction

from patient_service_support import runtime_role
from scheduling.appointment_concurrency_support import (
    start_appointment_canceller,
    wait_for_advisory_waiter,
)
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_cancel_waits_behind_failed_availability_retirement_without_deadlock(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
        block = AvailabilityBlock.objects.get(
            clinic_id=setup.clinic_id,
            practitioner_id=setup.practitioner_id,
            retired_at__isnull=True,
        )
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
        transaction.atomic(),
    ):
        acquire_advisory_locks((clinic_lock_key(setup.clinic_id),))
        worker = start_appointment_canceller(
            actor_id=setup.actor_id,
            organization_id=setup.organization_id,
            appointment_id=appointment.pk,
            reason=Appointment.CancellationReason.PATIENT_REQUEST,
            start_barrier=Barrier(1),
        )
        wait_for_advisory_waiter(worker[1].get(timeout=5), worker[2])
        with pytest.raises(AvailabilityHasAppointmentsError):
            retire_availability(
                clinic_id=setup.clinic_id,
                availability_id=block.pk,
            )
    worker[0].join(timeout=10)

    assert not worker[0].is_alive()
    outcome = worker[2].get_nowait()
    assert isinstance(outcome, Appointment)
    assert getattr(outcome, "sqlstate", None) != "40P01"
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment.refresh_from_db()
        block.refresh_from_db()
        assert appointment.status == Appointment.Status.CANCELLED
        assert block.retired_at is None
