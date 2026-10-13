from __future__ import annotations

from queue import Queue
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.scheduling import appointment_creation
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    patient_lock_key,
    user_lock_keys,
)
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentPractitionerError,
    SlotConflict,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.db.models.functions import Now

from patient_service_support import runtime_role
from scheduling.appointment_concurrency_support import (
    start_appointment_creator,
    wait_for_advisory_waiter,
)
from scheduling.appointment_service_support import (
    seed_appointment_setup,
    seed_cross_clinic_appointment_setups,
)

if TYPE_CHECKING:
    from apps.identity.models import Clinic
    from apps.scheduling.appointment_locking import AppointmentWriteRows
    from apps.scheduling.appointment_values import (
        CreateAppointmentRequest,
        PreparedAppointment,
    )

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_create_takes_practitioner_gate_before_patient_gate(
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
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert lock_state == {("clinic", True), ("practitioner", False)}
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    observed: Queue[tuple[int, int]] = Queue()
    original = appointment_creation._post_lock_revalidate

    def revoke_after_row_locks(
        request: CreateAppointmentRequest,
        expected: PreparedAppointment,
        rows: AppointmentWriteRows,
    ) -> tuple[Clinic, PreparedAppointment, Appointment | None]:
        # Ordinary revocation now shares the booking's user advisory lock.
        # Inject a real maintenance write on the same test-superuser connection
        # after every row lock, preserving both that guard and the recheck.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FILTER (WHERE granted), "
                "count(*) FILTER (WHERE NOT granted) FROM pg_locks "
                "WHERE pid = pg_backend_pid() AND locktype = 'advisory'"
            )
            state = cursor.fetchone()
            assert state is not None
            observed.put((int(state[0]), int(state[1])))
            cursor.execute("SET LOCAL ROLE clinic_owner")
            cursor.execute("SELECT set_config('app.current_user_id', '', true)")
            UserClinicRole.objects.filter(
                organization_id=setup.organization_id,
                clinic_id=setup.clinic_id,
                user_id=setup.practitioner_id,
                role=UserClinicRole.Role.PHYSICIAN,
            ).update(revoked_at=Now())
            cursor.execute("SET LOCAL ROLE clinic_app")
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)",
                [str(setup.actor_id)],
            )
        return original(request, expected, rows)

    monkeypatch.setattr(
        appointment_creation, "_post_lock_revalidate", revoke_after_row_locks
    )
    thread, backend_pids, outcomes = start_appointment_creator(setup, uuid4())
    backend_pids.get(timeout=5)
    thread.join(timeout=10)

    assert not thread.is_alive()
    advisory_state = observed.get_nowait()
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
        ).update(revoked_at=Now())
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
