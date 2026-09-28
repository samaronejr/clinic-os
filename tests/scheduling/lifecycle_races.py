"""Barrier-synchronized lifecycle races (no sleeps): real commits, two sessions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.models import AppointmentTransition
from apps.scheduling.services import (
    AppointmentLifecycleError,
    AppointmentLocalRange,
    SlotConflict,
    create_appointment,
    create_hold,
    expire,
)
from apps.tenancy.db import tenant_context
from django.db import close_old_connections, connections

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from scheduling.lifecycle_world import pinned_clock

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from scheduling.lifecycle_world import LifecycleWorld

TIMEOUT = 10


def _enrollments(world: LifecycleWorld) -> tuple[UUID, UUID]:
    with owner_context(world.graph.organization_a):
        other = (
            PatientClinicEnrollment.objects.filter(clinic_id=world.setup.clinic_id)
            .exclude(pk=world.setup.enrollment_id)
            .order_by("pk")
            .values_list("pk", flat=True)
            .first()
        )
    assert other is not None
    return world.setup.enrollment_id, other


def _run(calls: list[Callable[[], object]]) -> list[object]:
    barrier = Barrier(len(calls), timeout=TIMEOUT)

    def worker(call: Callable[[], object]) -> object:
        close_old_connections()
        try:
            barrier.wait()
            return call()
        except (SlotConflict, AppointmentLifecycleError) as error:
            return error
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(worker, call) for call in calls]
        return [future.result(timeout=TIMEOUT) for future in futures]


def race_two_holds(world: LifecycleWorld) -> tuple[int, int]:
    """Two patients' holds for one practitioner slot: the exclusion admits one."""
    setup = world.setup
    first, second = _enrollments(world)

    def hold_for(enrollment: UUID) -> Callable[[], object]:
        def call() -> object:
            with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
                return create_hold(
                    clinic_id=setup.clinic_id,
                    enrollment_id=enrollment,
                    practitioner_id=setup.practitioner_id,
                    local_range=AppointmentLocalRange(
                        "2035-06-02T11:00", "2035-06-02T11:30"
                    ),
                    idempotency_key=uuid4(),
                )

        return call

    results = _run([hold_for(first), hold_for(second)])
    winners = sum(1 for result in results if getattr(result, "status", "") == "held")
    losers = sum(1 for result in results if isinstance(result, SlotConflict))
    return winners, losers


def race_expiry_and_booking(
    world: LifecycleWorld, database_url: str
) -> tuple[int, int]:
    """At the exact deadline the W job and a rebooking race for one due hold."""
    setup = world.setup
    held = world.rows["held"]
    _, other = _enrollments(world)
    assert held.hold_expires_at is not None

    def expire_call() -> object:
        with runtime_role():
            return expire(
                clinic_id=setup.clinic_id,
                appointment_id=held.pk,
                expected_revision=held.revision,
                command_id=uuid4(),
            )

    def book_call() -> object:
        with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
            return create_appointment(
                clinic_id=setup.clinic_id,
                enrollment_id=other,
                practitioner_id=setup.practitioner_id,
                local_range=AppointmentLocalRange(
                    "2035-06-02T08:30", "2035-06-02T09:00"
                ),
                idempotency_key=uuid4(),
            )

    with pinned_clock(database_url, held.hold_expires_at):
        results = _run([expire_call, book_call])
    booked = sum(
        1 for result in results if getattr(result, "status", "") == "scheduled"
    )
    with owner_context(world.graph.organization_a):
        receipts = AppointmentTransition.objects.filter(
            appointment_id=held.pk, to_status="expired"
        ).count()
    return receipts, booked
