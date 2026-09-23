from __future__ import annotations

from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.scheduling.locks import acquire_advisory_locks, clinic_lock_key
from apps.scheduling.models import Appointment, AvailabilityBlock
from apps.scheduling.services import (
    AppointmentLocalRange,
    AppointmentTerminalError,
    AvailabilityHasAppointmentsError,
    SlotConflict,
    create_availability,
    retire_availability,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from patient_service_support import runtime_role
from scheduling.appointment_concurrency_support import (
    start_appointment_canceller,
    start_appointment_rescheduler,
    wait_for_advisory_waiter,
)
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
    seed_cross_clinic_appointment_setups,
)

if TYPE_CHECKING:
    from conftest import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = pytest.mark.django_db(transaction=True)


def _has_deadlock(outcome: Appointment | Exception) -> bool:
    return (
        getattr(outcome, "sqlstate", None) == "40P01"
        or getattr(getattr(outcome, "__cause__", None), "sqlstate", None) == "40P01"
    )


def _replace_availability_with_adjacent_blocks(
    setups: tuple[AppointmentSetup, ...],
) -> None:
    for setup in setups:
        clinic_id = setup.clinic_id
        practitioner_id = setup.practitioner_id
        block = AvailabilityBlock.objects.get(
            clinic_id=clinic_id,
            practitioner_id=practitioner_id,
            retired_at__isnull=True,
        )
        retire_availability(clinic_id=clinic_id, availability_id=block.pk)
        for start_local, end_local in (
            ("2035-06-02T08:00", "2035-06-02T10:00"),
            ("2035-06-02T10:00", "2035-06-02T12:00"),
        ):
            create_availability(
                clinic_id=clinic_id,
                practitioner_id=practitioner_id,
                start_local=start_local,
                end_local=end_local,
                idempotency_key=uuid4(),
            )


def test_cross_clinic_same_patient_swap_reschedules_terminate_without_deadlock(
    rbac_graph: RbacGraph,
) -> None:
    first, second = seed_cross_clinic_appointment_setups(rbac_graph)
    with runtime_role(), tenant_context(first.actor_id, first.organization_id):
        _replace_availability_with_adjacent_blocks((first, second))
        first_appointment = create_synthetic_appointment(first)
        second_appointment = create_synthetic_appointment(
            second,
            start_local="2035-06-02T10:00",
            end_local="2035-06-02T11:00",
        )

    barrier = Barrier(2)
    workers = (
        start_appointment_rescheduler(
            actor_id=first.actor_id,
            organization_id=first.organization_id,
            appointment_id=first_appointment.pk,
            local_range=AppointmentLocalRange(
                "2035-06-02T10:00",
                "2035-06-02T11:00",
            ),
            start_barrier=barrier,
        ),
        start_appointment_rescheduler(
            actor_id=second.actor_id,
            organization_id=second.organization_id,
            appointment_id=second_appointment.pk,
            local_range=AppointmentLocalRange(
                "2035-06-02T09:00",
                "2035-06-02T10:00",
            ),
            start_barrier=barrier,
        ),
    )
    for thread, _, _ in workers:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread, _, _ in workers)
    outcomes = [queue.get_nowait() for _, _, queue in workers]
    assert all(isinstance(outcome, SlotConflict) for outcome in outcomes)
    assert not any(_has_deadlock(outcome) for outcome in outcomes)
    with runtime_role(), tenant_context(first.actor_id, first.organization_id):
        rows = list(
            Appointment.objects.filter(patient_id=first.patient_id)
            .order_by("start_at")
            .values_list("pk", "start_at", "end_at")
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.scheduling_appointment AS appt "
                "WHERE appt.status = 'scheduled' AND NOT EXISTS (SELECT 1 "
                "FROM clinic_app.scheduling_availabilityblock AS availability "
                "WHERE availability.organization_id = appt.organization_id "
                "AND availability.clinic_id = appt.clinic_id "
                "AND availability.practitioner_id = appt.practitioner_id "
                "AND availability.retired_at IS NULL "
                "AND availability.start_at <= appt.start_at "
                "AND availability.end_at >= appt.end_at)"
            )
            orphan_count = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.rescheduled'"
            )
            audit_count = cursor.fetchone()

    assert [row[0] for row in rows] == [first_appointment.pk, second_appointment.pk]
    assert len({(row[1], row[2]) for row in rows}) == 2
    assert orphan_count == (0,)
    assert audit_count == (0,)


def test_cancel_and_reschedule_race_has_one_terminal_consistent_state(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
    barrier = Barrier(2)
    rescheduler = start_appointment_rescheduler(
        actor_id=setup.actor_id,
        organization_id=setup.organization_id,
        appointment_id=appointment.pk,
        local_range=AppointmentLocalRange(
            "2035-06-02T10:00",
            "2035-06-02T11:00",
        ),
        start_barrier=barrier,
    )
    canceller = start_appointment_canceller(
        actor_id=setup.actor_id,
        organization_id=setup.organization_id,
        appointment_id=appointment.pk,
        reason=Appointment.CancellationReason.PRACTITIONER_UNAVAILABLE,
        start_barrier=barrier,
    )
    for thread, _, _ in (rescheduler, canceller):
        thread.join(timeout=10)

    assert not rescheduler[0].is_alive()
    assert not canceller[0].is_alive()
    reschedule_outcome = rescheduler[2].get_nowait()
    cancel_outcome = canceller[2].get_nowait()
    assert isinstance(cancel_outcome, Appointment)
    assert isinstance(reschedule_outcome, (Appointment, AppointmentTerminalError))
    assert not _has_deadlock(reschedule_outcome)
    assert not _has_deadlock(cancel_outcome)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment.refresh_from_db()
        assert appointment.status == Appointment.Status.CANCELLED
        assert appointment.cancellation_reason == (
            Appointment.CancellationReason.PRACTITIONER_UNAVAILABLE
        )


def test_reschedule_waits_behind_availability_retirement_gate(
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
        wait_for_advisory_waiter(worker[1].get(timeout=5), worker[2])
        with pytest.raises(AvailabilityHasAppointmentsError):
            retire_availability(
                clinic_id=setup.clinic_id,
                availability_id=block.pk,
            )
    worker[0].join(timeout=10)

    assert not worker[0].is_alive()
    assert isinstance(worker[2].get_nowait(), Appointment)
