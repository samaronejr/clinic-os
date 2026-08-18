"""Resolver-only availability presentation for the clinic scheduling screen."""

from __future__ import annotations

from dataclasses import dataclass
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
from apps.scheduling.timezones import format_local_minute

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
