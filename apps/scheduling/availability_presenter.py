"""Resolver-only availability presentation for the clinic scheduling screen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING

from apps.identity.current_context import (
    CurrentActorError,
    list_active_clinic_physicians,
    practitioner_display_label,
)
from apps.scheduling.access import (
    AvailabilityAccessDeniedError,
    authorized_view_scope,
)
from apps.scheduling.timezones import LOCAL_MINUTE_FORMAT, format_local_minute

if TYPE_CHECKING:
    from uuid import UUID

    from apps.scheduling.availability_view import AvailabilityViewItem


@dataclass(frozen=True, slots=True)
class AvailabilityRow:
    """Expose one active block in clinic-local minutes with a safe label."""

    availability_id: UUID
    practitioner_label: str
    start_local: str
    end_local: str


@dataclass(frozen=True, slots=True)
class AvailabilityWindow:
    """One active block as clinic-local civil times for display only."""

    availability_id: UUID
    start: time
    end: time
    start_local: str
    end_local: str


@dataclass(frozen=True, slots=True)
class AvailabilityDay:
    """One clinic-local civil day and its windows in start order."""

    date: date
    windows: tuple[AvailabilityWindow, ...]


@dataclass(frozen=True, slots=True)
class PractitionerAvailability:
    """One practitioner's active windows grouped by clinic-local day."""

    practitioner_label: str
    days: tuple[AvailabilityDay, ...]
    total: int


@dataclass(frozen=True, slots=True)
class AvailabilityScreen:
    """Bind one authorized clinic screen to its role-appropriate controls."""

    timezone_key: str
    can_manage: bool
    choices: tuple[tuple[str, str], ...]


def authorized_screen(clinic_id: UUID) -> AvailabilityScreen:
    """Authorize the clinic once and derive resolver-only manager controls."""
    scope = authorized_view_scope(clinic_id)
    timezone_key = scope.clinic.timezone
    if not isinstance(timezone_key, str) or not timezone_key:
        raise AvailabilityAccessDeniedError
    if scope.practitioner_id is not None:
        return AvailabilityScreen(
            timezone_key=timezone_key,
            can_manage=False,
            choices=(),
        )
    return AvailabilityScreen(
        timezone_key=timezone_key,
        can_manage=True,
        choices=manager_choices(clinic_id),
    )


def manager_choices(clinic_id: UUID) -> tuple[tuple[str, str], ...]:
    """Return the sorted active physician catalog from Todo 2's resolver."""
    try:
        catalog = list_active_clinic_physicians(clinic_id)
    except CurrentActorError as error:
        raise AvailabilityAccessDeniedError from error
    return tuple(
        (str(entry.user_id), entry.display_label)
        for entry in sorted(
            catalog,
            key=lambda item: (item.display_label.casefold(), item.user_id),
        )
    )


def presented_rows(
    clinic_id: UUID,
    items: tuple[AvailabilityViewItem, ...],
    screen: AvailabilityScreen,
) -> tuple[AvailabilityRow, ...]:
    """Format active blocks in clinic-local minutes with resolver labels."""
    known = dict(screen.choices)
    labels: dict[UUID, str] = {}
    for practitioner_id in dict.fromkeys(item.practitioner_id for item in items):
        labels[practitioner_id] = known.get(str(practitioner_id)) or (
            practitioner_display_label(practitioner_id, clinic_id)
        )
    return tuple(
        AvailabilityRow(
            availability_id=item.availability_id,
            practitioner_label=labels[item.practitioner_id],
            start_local=format_local_minute(item.start_at, screen.timezone_key),
            end_local=format_local_minute(item.end_at, screen.timezone_key),
        )
        for item in items
    )


def _civil(local_minute: str) -> datetime:
    # Already converted to the clinic zone by the presenter; never re-zoned.
    return datetime.strptime(local_minute, LOCAL_MINUTE_FORMAT)  # noqa: DTZ007


def grouped_availability(
    rows: tuple[AvailabilityRow, ...],
) -> tuple[PractitionerAvailability, ...]:
    """Group presented rows by practitioner label, then by clinic-local day."""
    by_practitioner: dict[str, dict[date, list[AvailabilityWindow]]] = {}
    for row in rows:
        start = _civil(row.start_local)
        end = _civil(row.end_local)
        days = by_practitioner.setdefault(row.practitioner_label, {})
        days.setdefault(start.date(), []).append(
            AvailabilityWindow(
                availability_id=row.availability_id,
                start=start.time(),
                end=end.time(),
                start_local=row.start_local,
                end_local=row.end_local,
            )
        )
    return tuple(
        PractitionerAvailability(
            practitioner_label=label,
            days=tuple(
                AvailabilityDay(
                    date=day,
                    windows=tuple(
                        sorted(windows, key=lambda w: (w.start, w.availability_id))
                    ),
                )
                for day, windows in sorted(days.items())
            ),
            total=sum(len(windows) for windows in days.values()),
        )
        for label, days in sorted(
            by_practitioner.items(), key=lambda item: item[0].casefold()
        )
    )
