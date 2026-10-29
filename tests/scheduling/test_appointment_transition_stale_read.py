from __future__ import annotations

from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    patient_lock_key,
    user_lock_keys,
)
from apps.scheduling.models import Appointment
from apps.scheduling.services import AppointmentLocalRange, AppointmentTerminalError
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.utils import timezone

from patient_service_support import runtime_role
from scheduling.appointment_concurrency_support import (
    start_appointment_rescheduler,
    wait_for_advisory_waiter,
)
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_reschedule_reloads_terminal_state_changed_before_patient_gate(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        acquire_advisory_locks(
            (patient_lock_key(setup.organization_id, setup.patient_id),)
        )
        worker = start_appointment_rescheduler(
            actor_id=setup.actor_id,
            organization_id=setup.organization_id,
            appointment_id=appointment.pk,
            local_range=AppointmentLocalRange(
                "2035-06-02T10:00",
                "2035-06-02T11:00",
            ),
            start_barrier=Barrier(1),
        )
        backend_pid = worker[1].get(timeout=5)
        wait_for_advisory_waiter(backend_pid, worker[2])
        cursor.execute(
            "SELECT gates.name, locks.granted "
            "FROM (VALUES ('clinic', %s), ('practitioner', %s), "
            "('patient', %s)) AS gates(name, key) "
            "JOIN pg_locks AS locks "
            "ON (locks.classid::bigint << 32 | locks.objid::bigint) = "
            "hashtextextended(gates.key, 0) "
            "WHERE locks.pid = %s AND locks.locktype = 'advisory'",
            [
                clinic_lock_key(setup.clinic_id),
                user_lock_keys((setup.practitioner_id,))[0],
                patient_lock_key(setup.organization_id, setup.patient_id),
                backend_pid,
            ],
        )
        lock_state = set(cursor.fetchall())
        Appointment.objects.filter(pk=appointment.pk).update(
            status=Appointment.Status.CANCELLED,
            cancellation_reason=Appointment.CancellationReason.CLINIC_REQUEST,
            cancelled_at=timezone.now(),
        )
    worker[0].join(timeout=10)

    assert not worker[0].is_alive()
    assert lock_state == {
        ("clinic", True),
        ("practitioner", True),
        ("patient", False),
    }
    assert isinstance(worker[2].get_nowait(), AppointmentTerminalError)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment.refresh_from_db()
        assert appointment.status == Appointment.Status.CANCELLED
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.rescheduled'"
            )
            assert cursor.fetchone() == (0,)
