"""Clinic-local multi-resource day grid: practitioners and rooms by time.

The grid reuses the agenda's authorization (booking staff or physician-own)
and civil-day bounds. It adds the room dimension: each appointment appears in its
practitioner's column and in the column of every room it occupies. Moving is
done by ``move_appointment`` with the revision the grid rendered, so a stale
screen can never overwrite a newer change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.identity.current_context import (
    MANAGER_ROLES,
    CurrentActorError,
    require_current_actor_clinic_roles,
    require_permission,
)
from apps.scheduling.access import (
    AvailabilityAccessDeniedError,
    AvailabilityViewScope,
    authorized_agenda_scope,
)
from apps.scheduling.agenda_queries import (
    AGENDA_STATUSES,
    AgendaInputError,
    _inputs,
    _labels,
)
from apps.scheduling.appointment_locking import due_hold
from apps.scheduling.models import Appointment, Resource
from apps.scheduling.resource_booking import service_practitioners
from apps.scheduling.timezones import civil_day_bounds, format_local_minute

if TYPE_CHECKING:
    from datetime import date

SLOT_MINUTES: Final = 30
FIRST_SLOT: Final = 7 * 60
LAST_SLOT_END: Final = 20 * 60
MAX_GRID_APPOINTMENTS: Final = 200
LOCAL_TIME: Final = slice(11, 16)


@dataclass(frozen=True, slots=True)
class GridColumn:
    """One practitioner or room column, in display order."""

    kind: str
    column_id: UUID
    label: str


@dataclass(frozen=True, slots=True)
class GridAppointment:
    """One visible appointment with its grid placement and move revision."""

    appointment_id: UUID
    patient_display_name: str
    practitioner_id: UUID
    room_ids: tuple[UUID, ...]
    start_local: str
    end_local: str
    status: str
    revision: int
    movable: bool

    @property
    def start_time(self) -> str:
        """Clinic-local HH:MM start."""
        return self.start_local[LOCAL_TIME]

    @property
    def end_time(self) -> str:
        """Clinic-local HH:MM end."""
        return self.end_local[LOCAL_TIME]

    @property
    def duration_minutes(self) -> int:
        """Whole minutes between the clinic-local start and end."""
        return _minute(self.end_time) - _minute(self.start_time)

    @property
    def occupies(self) -> bool:
        """Whether the lifecycle state consumes practitioner and room capacity."""
        return self.status in Appointment.OCCUPYING_STATUSES

    @property
    def appearance(self) -> str:
        """Reuse the resource grid's semantic lifecycle treatments."""
        appearances: dict[str, str] = {
            Appointment.Status.REQUESTED: "held",
            Appointment.Status.HELD: "held",
            Appointment.Status.COMPLETED: "done",
            Appointment.Status.CANCELLED: "inactive",
            Appointment.Status.NO_SHOW: "inactive",
        }
        return appearances.get(self.status, "booked")


@dataclass(frozen=True, slots=True)
class DayGrid:
    """One authorized clinic-local day across practitioner and room columns."""

    date: date
    timezone_key: str
    columns: tuple[GridColumn, ...]
    slots: tuple[str, ...]
    appointments: tuple[GridAppointment, ...]
    can_move: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class _MoveRules:
    """Which bookings the viewer's move would be accepted for."""

    legacy: bool
    service: bool
    own: UUID | None

    @property
    def any(self) -> bool:
        return self.legacy or self.service or self.own is not None

    def allows(self, row: Appointment) -> bool:
        if row.status != Appointment.Status.SCHEDULED:
            return False
        if row.service_type_id is None:
            return self.legacy
        return self.service or self.own == row.practitioner_id


def _minute(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _slots(appointments: tuple[GridAppointment, ...]) -> tuple[str, ...]:
    first = min((_minute(item.start_time) for item in appointments), default=FIRST_SLOT)
    last = max((_minute(item.end_time) for item in appointments), default=0)
    start = min(FIRST_SLOT, first - first % SLOT_MINUTES)
    end = max(LAST_SLOT_END, last)
    return tuple(_hhmm(minute) for minute in range(start, end, SLOT_MINUTES))


def _columns(scope: AvailabilityViewScope, booked: set[UUID]) -> dict[UUID, str]:
    """Label active professionals and retain agenda labels for retired bookings."""
    if scope.practitioner_id is not None:
        return _labels(scope, (scope.practitioner_id,))
    try:
        catalog = service_practitioners(scope.clinic.pk)
    except CurrentActorError as error:
        raise AvailabilityAccessDeniedError from error
    labels = dict(sorted(catalog, key=lambda entry: (entry[1], str(entry[0]))))
    labels.update(_labels(scope, tuple(sorted(booked - set(labels), key=str))))
    return labels


def _holds(permission: str, clinic_id: UUID) -> UUID | None:
    """Return the actor holding a permission, or no authority."""
    try:
        return require_permission(permission, clinic_id=clinic_id)
    except CurrentActorError:
        return None


def _move_rules(clinic_id: UUID) -> _MoveRules:
    """Mirror the legacy, service and own-service reschedule authority."""
    try:
        require_current_actor_clinic_roles(clinic_id, MANAGER_ROLES)
    except CurrentActorError:
        legacy = False
    else:
        legacy = True
    return _MoveRules(
        legacy=legacy,
        service=_holds("appointment.move", clinic_id) is not None,
        own=_holds("appointment.move_own", clinic_id),
    )


def authorize_grid_move(clinic_id: UUID) -> None:
    """Authorize the response surface before any move can write.

    The service independently authorizes the selected booking. Booking staff
    can view all columns; a physician can view and move their own service
    booking. Other actors receive the unknown-clinic refusal without a write.
    """
    if type(clinic_id) is not UUID:
        raise AvailabilityAccessDeniedError
    authorized_agenda_scope(clinic_id)


def view_day_grid(*, clinic_id: UUID, date: str) -> DayGrid:
    """Return one authorized clinic-local day by practitioner and room."""
    if type(clinic_id) is not UUID:
        raise AvailabilityAccessDeniedError
    with transaction.atomic():
        scope = authorized_agenda_scope(clinic_id)
        timezone_key = scope.clinic.timezone
        if not isinstance(timezone_key, str):
            raise AvailabilityAccessDeniedError
        _, civil_date, _ = _inputs("day", date, 1)
        start_at, end_at = civil_day_bounds(civil_date, timezone_key)
        rows = Appointment.objects.filter(
            organization_id=scope.clinic.organization_id,
            clinic_id=clinic_id,
            start_at__gte=start_at,
            start_at__lt=end_at,
            status__in=AGENDA_STATUSES,
        ).exclude(due_hold())
        if scope.practitioner_id is not None:
            rows = rows.filter(practitioner_id=scope.practitioner_id)
        ordered = tuple(
            rows.select_related("patient").order_by(
                "start_at", "practitioner_id", "pk"
            )[: MAX_GRID_APPOINTMENTS + 1]
        )
        selected = ordered[:MAX_GRID_APPOINTMENTS]
        rooms = tuple(
            Resource.objects.filter(
                clinic_id=clinic_id, kind=Resource.Kind.ROOM, active=True
            ).order_by("name", "pk")
        )
        room_ids = {room.pk for room in rooms}
        labels = _columns(scope, {row.practitioner_id for row in selected})
        rules = _move_rules(clinic_id)
        appointments = tuple(
            GridAppointment(
                appointment_id=row.pk,
                patient_display_name=row.patient.full_name,
                practitioner_id=row.practitioner_id,
                room_ids=tuple(pk for pk in row.resource_ids if pk in room_ids),
                start_local=format_local_minute(row.start_at, timezone_key),
                end_local=format_local_minute(row.end_at, timezone_key),
                status=row.status,
                revision=row.revision,
                movable=rules.allows(row),
            )
            for row in selected
        )
        grid = DayGrid(
            date=civil_date,
            timezone_key=timezone_key,
            columns=(
                *(
                    GridColumn("practitioner", pk, label)
                    for pk, label in labels.items()
                ),
                *(GridColumn("room", room.pk, room.name) for room in rooms),
            ),
            slots=_slots(appointments),
            appointments=appointments,
            can_move=rules.any,
            truncated=len(ordered) > MAX_GRID_APPOINTMENTS,
        )
        record_phase1_event(
            "scheduling.agenda.viewed",
            clinic_id=clinic_id,
            affected_record_id=clinic_id,
        )
        return grid


__all__ = (
    "AgendaInputError",
    "DayGrid",
    "GridAppointment",
    "GridColumn",
    "authorize_grid_move",
    "view_day_grid",
)
