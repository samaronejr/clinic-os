"""Bounded, clinic-local administrative reception projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.db import connection
from django.db.models import Q
from django.utils import timezone

from apps.identity.current_context import CurrentActorError, require_permission
from apps.scheduling.models import (
    Appointment,
    AppointmentResource,
    ClinicianDelay,
    DisplayDevice,
    QueueEntry,
    Resource,
    RoomStatus,
)
from apps.scheduling.reception import (
    ArrivalReadiness,
    ReceptionAccessDeniedError,
    arrival_readiness,
    reception_clinic,
)
from apps.scheduling.timezones import civil_day_bounds
from apps.workflows.models import Task

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

    from apps.identity.models import Clinic

PAGE_LIMIT: Final = 200


@dataclass(frozen=True, slots=True)
class ReceptionArrival:
    """One appointment with administrative readiness and a native arrival command."""

    appointment: Appointment
    time: str
    readiness: ArrivalReadiness
    command_id: UUID
    late: bool
    can_arrive: bool
    room_conflict: bool


@dataclass(frozen=True, slots=True)
class ReceptionRoom:
    """Operational condition and queue occupancy of one room unit."""

    room: Resource
    unit: int
    code: str
    state: str
    revision: int
    ticket: str


@dataclass(frozen=True, slots=True)
class ReceptionDay:
    """Only authorized clinic data, with explicit truncation and unavailable counts."""

    clinic: Clinic
    day: date
    arrivals: tuple[ReceptionArrival, ...]
    queue: tuple[QueueEntry, ...]
    rooms: tuple[ReceptionRoom, ...]
    locations: tuple[Resource, ...]
    delays: tuple[ClinicianDelay, ...]
    devices: tuple[DisplayDevice, ...]
    late_count: int
    conflict_count: int
    pending_tasks: int | None
    limited: bool


def view_reception_day(*, clinic_id: UUID, day: date | None = None) -> ReceptionDay:
    """Compute the clinic-local reception board from committed records."""
    clinic = reception_clinic(clinic_id=clinic_id)
    if not isinstance(clinic.timezone, str):
        raise ReceptionAccessDeniedError
    try:
        require_permission("appointment.move", clinic_id=clinic_id)
        can_arrive = True
    except CurrentActorError:
        can_arrive = False
    now = timezone.now()
    selected_day = day or now.astimezone(ZoneInfo(clinic.timezone)).date()
    lower, upper = civil_day_bounds(selected_day, clinic.timezone)
    appointments = (
        tuple(
            Appointment.objects.filter(
                clinic_id=clinic_id,
                start_at__gte=lower,
                start_at__lt=upper,
                status__in=("scheduled", "arrived", "in_progress", "requested", "held"),
            )
            .select_related("patient")
            .order_by("start_at", "pk")[: PAGE_LIMIT + 1]
        )
        if can_arrive
        else ()
    )
    blocked = {
        (row.room_id, row.unit)
        for row in RoomStatus.objects.filter(clinic_id=clinic_id).exclude(
            state="available"
        )
    }
    conflicts = set(
        AppointmentResource.objects.filter(
            clinic_id=clinic_id, appointment_id__in=[row.pk for row in appointments]
        ).values_list("appointment_id", "resource_id", "unit")
    )
    queued = set(
        QueueEntry.objects.filter(
            clinic_id=clinic_id,
            appointment_id__in=[row.pk for row in appointments],
        ).values_list("appointment_id", flat=True)
    )
    arrivals = tuple(
        ReceptionArrival(
            appointment=row,
            time=row.start_at.astimezone(ZoneInfo(clinic.timezone)).strftime("%H:%M"),
            readiness=arrival_readiness(clinic_id=clinic_id, appointment_id=row.pk),
            command_id=uuid4(),
            late=row.status == "scheduled" and row.start_at < now,
            can_arrive=(
                can_arrive
                and row.status in {"scheduled", "arrived"}
                and row.pk not in queued
            ),
            room_conflict=any(
                record == row.pk and (resource, unit) in blocked
                for record, resource, unit in conflicts
            ),
        )
        for row in appointments[:PAGE_LIMIT]
    )
    queue = tuple(
        QueueEntry.objects.filter(
            Q(state__in=("waiting", "called", "in_room"))
            | Q(created_at__gte=lower, created_at__lt=upper),
            clinic_id=clinic_id,
        )
        .select_related("location", "room", "appointment__patient")
        .order_by("location_id", "position")
    )
    statuses = {
        (row.room_id, row.unit): row
        for row in RoomStatus.objects.filter(clinic_id=clinic_id)
    }
    occupied = {
        (row.room_id, row.room_unit): row.ticket_code
        for row in QueueEntry.objects.filter(
            clinic_id=clinic_id, state__in=("called", "in_room")
        )
    }
    rooms = tuple(
        ReceptionRoom(
            room=room,
            unit=unit,
            code=(
                f"R{statuses[(room.pk, unit)].display_number}"
                if (room.pk, unit) in statuses
                else ""
            ),
            state=(
                "occupied"
                if (room.pk, unit) in occupied
                else statuses[(room.pk, unit)].state
                if (room.pk, unit) in statuses
                else "available"
            ),
            revision=(
                statuses[(room.pk, unit)].revision if (room.pk, unit) in statuses else 0
            ),
            ticket=occupied.get((room.pk, unit), ""),
        )
        for room in Resource.objects.filter(
            clinic_id=clinic_id, kind="room", active=True
        ).order_by("name", "pk")
        for unit in range(1, room.capacity + 1)
    )
    pending = None
    if can_arrive:
        try:
            require_permission("tasks.view", clinic_id=clinic_id)
            pending = Task.objects.filter(
                clinic_id=clinic_id, state__in=("open", "assigned", "in_progress")
            ).count()
        except CurrentActorError:
            pending = None
    return ReceptionDay(
        clinic=clinic,
        day=selected_day,
        arrivals=arrivals,
        queue=queue,
        rooms=rooms,
        locations=tuple(
            Resource.objects.filter(
                clinic_id=clinic_id, kind="location", active=True
            ).order_by("name", "pk")
        ),
        delays=tuple(ClinicianDelay.objects.filter(clinic_id=clinic_id))
        if can_arrive
        else (),
        devices=tuple(
            DisplayDevice.objects.filter(clinic_id=clinic_id, active=True).order_by(
                "created_at", "pk"
            )
        )
        if can_arrive
        else (),
        late_count=sum(row.late for row in arrivals),
        conflict_count=sum(row.room_conflict for row in arrivals),
        pending_tasks=pending,
        limited=len(appointments) > PAGE_LIMIT,
    )


def reception_practitioners(*, clinic_id: UUID) -> tuple[tuple[UUID, str], ...]:
    """Read only active professional labels through the database projection."""
    reception_clinic(clinic_id=clinic_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.reception_practitioners(%s)", [clinic_id]
        )
        return tuple(cursor.fetchall())
