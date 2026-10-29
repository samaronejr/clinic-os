"""Live authorized Settings discovery while the retained workspace stays narrow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django import template

from apps.core.navigation import (
    DESTINATIONS,
    allows,
    destination_url,
    granted_permissions,
)
from apps.identity.current_context import CurrentActorError, current_actor_id
from apps.identity.models import UserClinicRole

if TYPE_CHECKING:
    from uuid import UUID

register = template.Library()


@dataclass(frozen=True, slots=True)
class SchedulingSettingsLink:
    """A clinic-only link, never a patient or clinical selector."""

    name: str
    url: str


@register.simple_tag
def scheduling_settings_links() -> tuple[SchedulingSettingsLink, ...]:
    """Resolve the actor's own live clinics through the real Settings registry."""
    try:
        actor_id = current_actor_id()
    except CurrentActorError:
        return ()
    assignments = list(
        UserClinicRole.objects.filter(user_id=actor_id, clinic_id__isnull=False)
        .select_related("clinic")
        .order_by("clinic_id", "role")
    )
    settings = next(place for place in DESTINATIONS if place.key == "settings")
    seen: set[UUID] = set()
    links = []
    for assignment in assignments:
        clinic = assignment.clinic
        if clinic.pk in seen:
            continue
        seen.add(clinic.pk)
        roles = frozenset(row.role for row in assignments if row.clinic_id == clinic.pk)
        granted = granted_permissions(clinic.pk)
        for place in settings.section():
            if allows(place, roles=roles, granted=granted):
                links.append(
                    SchedulingSettingsLink(
                        clinic.name, destination_url(place, clinic.pk)
                    )
                )
                break
    return tuple(links)
