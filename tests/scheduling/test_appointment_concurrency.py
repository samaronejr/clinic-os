from __future__ import annotations

from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    user_lock_keys,
)
from apps.scheduling.models import Appointment, AvailabilityBlock
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentPractitionerError,
    SlotConflict,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from patient_service_support import runtime_role
from scheduling.appointment_concurrency_support import (
    start_appointment_creator,
    wait_for_advisory_waiter,
    wait_for_row_waiter,
)
from scheduling.appointment_service_support import (
    seed_appointment_setup,
    seed_cross_clinic_appointment_setups,
)

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_create_takes_patient_gate_before_practitioner_gate(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with transaction.atomic():
        acquire_advisory_locks(user_lock_keys((setup.practitioner_id,)))
        thread, backend_pids, outcomes = start_appointment_creator(setup, uuid4())
        backend_pid = backend_pids.get(timeout=5)
        wait_for_advisory_waiter(backend_pid, outcomes)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FILTER (WHERE granted), "
                "count(*) FILTER (WHERE NOT granted) FROM pg_locks "
                "WHERE pid = %s AND locktype = 'advisory'",
                [backend_pid],
            )
            lock_state = cursor.fetchone()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert lock_state == (2, 1)
    assert isinstance(outcomes.get_nowait(), Appointment)


def test_cross_clinic_same_patient_creates_terminate_without_orphans(
    rbac_graph: RbacGraph,
) -> None:
    setups = seed_cross_clinic_appointment_setups(rbac_graph)
    barrier = Barrier(2)
    workers = [
        start_appointment_creator(setup, uuid4(), start_barrier=barrier)
        for setup in setups
    ]
    for thread, _, _ in workers:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread, _, _ in workers)
    results = [outcomes.get_nowait() for _, _, outcomes in workers]
    assert sum(isinstance(result, Appointment) for result in results) == 1
    assert sum(isinstance(result, SlotConflict) for result in results) == 1
    assert all(
        getattr(result, "sqlstate", None) != "40P01"
        and getattr(getattr(result, "__cause__", None), "sqlstate", None) != "40P01"
        for result in results
    )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        connection.cursor() as cursor,
    ):
        assert Appointment.objects.filter(patient_id=setups[0].patient_id).count() == 1
        cursor.execute(
            "SELECT count(*) FROM clinic_app.scheduling_appointment AS appt "
            "WHERE NOT EXISTS (SELECT 1 "
            "FROM clinic_app.scheduling_availabilityblock AS availability "
            "WHERE availability.organization_id = appt.organization_id "
            "AND availability.clinic_id = appt.clinic_id "
            "AND availability.practitioner_id = appt.practitioner_id "
            "AND availability.retired_at IS NULL "
            "AND availability.start_at <= appt.start_at "
            "AND availability.end_at >= appt.end_at)"
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event_tenant "
            "WHERE event_type = 'scheduling.appointment.created'"
        )
        assert cursor.fetchone() == (1,)


def test_post_row_lock_revalidation_observes_physician_revocation(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        _ = AvailabilityBlock.objects.select_for_update().get(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            practitioner_id=setup.practitioner_id,
        )
        thread, backend_pids, outcomes = start_appointment_creator(setup, uuid4())
        backend_pid = backend_pids.get(timeout=5)
        wait_for_row_waiter(backend_pid, outcomes)
        cursor.execute(
            "SELECT count(*) FILTER (WHERE granted), "
            "count(*) FILTER (WHERE NOT granted) FROM pg_locks "
            "WHERE pid = %s AND locktype = 'advisory'",
            [backend_pid],
        )
        advisory_state = cursor.fetchone()
        UserClinicRole.objects.filter(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            user_id=setup.practitioner_id,
            role=UserClinicRole.Role.PHYSICIAN,
        ).delete()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert advisory_state == (3, 0)
    assert isinstance(outcomes.get_nowait(), AppointmentPractitionerError)
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
        connection.cursor() as cursor,
    ):
        assert Appointment.objects.count() == 0
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event_tenant "
            "WHERE event_type = 'scheduling.appointment.created'"
        )
        assert cursor.fetchone() == (0,)


def test_concurrent_equal_key_returns_one_booking_and_one_audit(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    barrier = Barrier(2)
    key = uuid4()
    workers = [
        start_appointment_creator(setup, key, start_barrier=barrier) for _ in range(2)
    ]
    for thread, _, _ in workers:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread, _, _ in workers)
    results = [outcomes.get_nowait() for _, _, outcomes in workers]
    bookings = tuple(result for result in results if isinstance(result, Appointment))
    assert len(bookings) == 2
    assert bookings[0].pk == bookings[1].pk
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
        connection.cursor() as cursor,
    ):
        assert Appointment.objects.count() == 1
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event_tenant "
            "WHERE event_type = 'scheduling.appointment.created'"
        )
        assert cursor.fetchone() == (1,)


def test_concurrent_same_slot_returns_one_opaque_conflict(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    barrier = Barrier(2)
    workers = [
        start_appointment_creator(setup, uuid4(), start_barrier=barrier)
        for _ in range(2)
    ]
    for thread, _, _ in workers:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread, _, _ in workers)
    results = [outcomes.get_nowait() for _, _, outcomes in workers]
    assert sum(isinstance(result, Appointment) for result in results) == 1
    assert sum(isinstance(result, SlotConflict) for result in results) == 1


def test_manager_revocation_winner_denies_waiting_create_without_residue(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        acquire_advisory_locks((clinic_lock_key(setup.clinic_id),))
        thread, backend_pids, outcomes = start_appointment_creator(setup, uuid4())
        backend_pid = backend_pids.get(timeout=5)
        wait_for_advisory_waiter(backend_pid, outcomes)
        UserClinicRole.objects.filter(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            user_id=setup.actor_id,
            role=UserClinicRole.Role.RECEPTIONIST,
        ).delete()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert isinstance(outcomes.get_nowait(), AppointmentAccessDeniedError)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        assert Appointment.objects.count() == 0
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event_tenant "
            "WHERE event_type = 'scheduling.appointment.created'"
        )
        assert cursor.fetchone() == (0,)
