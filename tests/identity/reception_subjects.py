"""Valid reception subjects reuse the parity world's existing availability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.reception import (
    IssuedDisplayDevice,
    issue_display_device,
    mark_arrived,
)
from apps.scheduling.resource_services import ResourceInput, create_resource
from apps.scheduling.services import create_availability
from apps.tenancy.db import tenant_context

from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    AppointmentSetup,
    create_synthetic_appointment,
)
from scheduling.test_arrivals_queue import ReceptionSetup

if TYPE_CHECKING:
    from apps.scheduling.models import Appointment, QueueEntry

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True)
class ReceptionSubjects:
    setup: ReceptionSetup
    queue: QueueEntry
    device: IssuedDisplayDevice = field(repr=False)


def seed_reception(graph: RbacGraph, appointment: Appointment) -> ReceptionSubjects:
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=appointment.clinic_id, patient_id=appointment.patient_id
        )
        booking = AppointmentSetup(
            organization_id=graph.organization_a,
            clinic_id=appointment.clinic_id,
            actor_id=graph.shared_user,
            practitioner_id=appointment.practitioner_id,
            enrollment_id=enrollment.pk,
            patient_id=appointment.patient_id,
        )
        create_availability(
            clinic_id=booking.clinic_id,
            practitioner_id=booking.practitioner_id,
            start_local="2035-06-20T08:00",
            end_local="2035-06-20T12:00",
            idempotency_key=uuid4(),
        )
        pending = create_synthetic_appointment(
            booking, start_local="2035-06-20T10:00", end_local="2035-06-20T10:30"
        )
        queued = create_synthetic_appointment(
            booking, start_local="2035-06-20T10:30", end_local="2035-06-20T11:00"
        )
        location = create_resource(
            clinic_id=booking.clinic_id,
            content=ResourceInput(name="Reception parity location", kind="location"),
        )
        room = create_resource(
            clinic_id=booking.clinic_id,
            content=ResourceInput(name="Reception parity room", kind="room"),
        )
        queue = mark_arrived(
            clinic_id=booking.clinic_id,
            appointment_id=queued.pk,
            location_id=location.pk,
            expected_revision=queued.revision,
            command_id=uuid4(),
        )
        device = issue_display_device(clinic_id=booking.clinic_id)
    return ReceptionSubjects(
        ReceptionSetup(booking, pending, location, room), queue, device
    )
