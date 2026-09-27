"""Direct SQL oracles for the todo 21 scheduling guards (resources, capacity).

Three of the guards are triggers, so an oracle fires the trigger with the one
statement it guards instead of calling the function. Role expectations come
from the versioned permission bundles, never from the SQL bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.identity.permissions import BUNDLES_V1
from apps.scheduling.models import Appointment, AvailabilityBlock, Resource
from apps.scheduling.resource_services import (
    ResourceInput,
    ServiceInput,
    TemplateInput,
    create_resource,
    create_service_type,
    create_template,
    generate_availability,
)
from apps.scheduling.services import (
    AppointmentLocalRange,
    ServiceBooking,
    create_availability,
    create_service_appointment,
)
from apps.tenancy.db import tenant_context
from django.db import connection

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from identity.legacy_parity_support import LegacyWorld

DAY = date(2035, 7, 10)
# legacy_parity_support.world binds the physician role to graph.physician, the
# practitioner of the seeded service booking; every other role is a new actor.
PRACTITIONER_ROLE = "physician"


def permitted(*permissions: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            role
            for role, bundle in BUNDLES_V1.items()
            if any(permission in bundle for permission in permissions)
        )
    )


def own_permitted(any_permission: str, own_permission: str) -> tuple[str, ...]:
    """Holders of the clinic-wide permission, plus own-scope holders acting as
    the booked practitioner (only the world's physician is that practitioner)."""
    own = {PRACTITIONER_ROLE} & set(permitted(own_permission))
    return tuple(sorted({*permitted(any_permission), *own}))


@dataclass(frozen=True)
class SchedulingSubjects:
    room: UUID
    appointment: UUID


def seed_scheduling(actor: LegacyWorld, enrollment: UUID) -> SchedulingSubjects:
    graph = actor.graph
    booker = graph.shared_user
    assert set(permitted("appointment.book")) & {"receptionist"}, "seed booker"
    with runtime_role(), tenant_context(booker, graph.organization_a):
        room = create_resource(
            clinic_id=actor.clinic,
            content=ResourceInput(name="Sintetico census room", kind="room"),
        )
        service = create_service_type(
            clinic_id=actor.clinic,
            content=ServiceInput(
                name="Sintetico census service",
                duration_min=30,
                required_resource_kinds=("room",),
            ),
        )
        template = create_template(
            clinic_id=actor.clinic,
            content=TemplateInput(
                resource_id=room.pk,
                weekdays=(DAY.weekday(),),
                start_local=time(8),
                end_local=time(12),
                valid_from=DAY,
                valid_to=DAY,
            ),
        )
        generate_availability(
            clinic_id=actor.clinic,
            template_id=template.pk,
            start_date=DAY,
            end_date=DAY,
        )
        create_availability(
            clinic_id=actor.clinic,
            practitioner_id=graph.physician,
            start_local=f"{DAY.isoformat()}T08:00",
            end_local=f"{DAY.isoformat()}T12:00",
            idempotency_key=uuid4(),
        )
        appointment = create_service_appointment(
            clinic_id=actor.clinic,
            enrollment_id=enrollment,
            practitioner_id=graph.physician,
            booking=ServiceBooking(
                AppointmentLocalRange(
                    f"{DAY.isoformat()}T09:00", f"{DAY.isoformat()}T09:30"
                ),
                service.pk,
                (room.pk,),
            ),
            idempotency_key=uuid4(),
        )
    return SchedulingSubjects(room.pk, appointment.pk)


def _clinic(actor: LegacyWorld, valid: bool) -> UUID:
    # Same organization, no membership: only the permission decision differs.
    return actor.clinic if valid else actor.graph.clinic_b


def _foreign_actor() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id', %s, true)", [str(uuid4())]
        )


def insert_definition(actor: LegacyWorld, valid: bool) -> bool:
    Resource.objects.create(
        organization_id=actor.graph.organization_a,
        clinic_id=_clinic(actor, valid),
        name="Sintetico census definition",
        kind="room",
    )
    return True


def insert_resource_block(
    actor: LegacyWorld, subjects: SchedulingSubjects, valid: bool
) -> bool:
    start = datetime(2035, 8, 1, 13, tzinfo=UTC)
    AvailabilityBlock.objects.create(
        organization_id=actor.graph.organization_a,
        clinic_id=_clinic(actor, valid),
        resource_id=subjects.room,
        start_at=start,
        end_at=start + timedelta(hours=1),
        idempotency_key=uuid4(),
        create_fingerprint=b"c" * 32,
    )
    return True


def update_service_booking(subjects: SchedulingSubjects, valid: bool) -> bool:
    if not valid:
        _foreign_actor()
    booking = Appointment.objects.get(pk=subjects.appointment)
    return (
        Appointment.objects.filter(pk=subjects.appointment).update(
            updated_at=booking.updated_at
        )
        == 1
    )


def insert_service_booking(subjects: SchedulingSubjects, valid: bool) -> bool:
    if not valid:
        _foreign_actor()
    booking = Appointment.objects.get(pk=subjects.appointment)
    shift = timedelta(minutes=90)
    Appointment.objects.create(
        **{
            field.attname: getattr(booking, field.attname)
            for field in Appointment._meta.concrete_fields
        }
        | {
            "id": uuid4(),
            "idempotency_key": uuid4(),
            "start_at": booking.start_at + shift,
            "end_at": booking.end_at + shift,
        }
    )
    return True
