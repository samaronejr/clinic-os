"""Clinic-local multi-resource day grid: practitioners and rooms by time.

The grid reuses the agenda's authorization (manager-all or physician-own) and
civil-day bounds. It adds the room dimension: each appointment appears in its
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
    CurrentActorError,
    list_active_clinic_physicians,
)
from apps.scheduling.access import (
    AvailabilityAccessDeniedError,
    AvailabilityViewScope,
    authorized_view_scope,
)
from apps.scheduling.agenda_queries import (
    AGENDA_STATUSES,
    AgendaInputError,
    _inputs,
    _labels,
)
from apps.scheduling.models import Appointment, Resource
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
    """Label every active physician for managers, or only the physician themself."""
    if scope.practitioner_id is not None:
        return _labels(scope, (scope.practitioner_id,))
    try:
        catalog = list_active_clinic_physicians(scope.clinic.pk)
    except CurrentActorError as error:
        raise AvailabilityAccessDeniedError from error
    labels: dict[UUID, str] = {entry.user_id: entry.display_label for entry in catalog}
    labels.update((pk, str(pk)) for pk in sorted(booked - set(labels)))
    return labels


def view_day_grid(*, clinic_id: UUID, date: str) -> DayGrid:
    """Return one authorized clinic-local day by practitioner and room."""
    if type(clinic_id) is not UUID:
        raise AvailabilityAccessDeniedError
    with transaction.atomic():
        scope = authorized_view_scope(clinic_id)
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
        )
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
        practitioners = tuple(labels)
        can_move = scope.practitioner_id is None
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
                movable=can_move and row.status == Appointment.Status.SCHEDULED,
            )
            for row in selected
        )
        grid = DayGrid(
            date=civil_date,
            timezone_key=timezone_key,
            columns=(
                *(
                    GridColumn("practitioner", pk, labels.get(pk, str(pk)))
                    for pk in practitioners
                ),
                *(GridColumn("room", room.pk, room.name) for room in rooms),
            ),
            slots=_slots(appointments),
            appointments=appointments,
            can_move=can_move,
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
    "view_day_grid",
)
