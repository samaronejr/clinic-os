"""Presentation-only configuration: never replace clinical or safety UI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template

from apps.identity.clinic_configuration import latest_configuration

if TYPE_CHECKING:
    from uuid import UUID

    from apps.identity.models import ClinicConfiguration

register = template.Library()


@register.simple_tag
def clinic_branding(clinic_id: UUID) -> ClinicConfiguration | None:
    """Read a staff-visible clinic snapshot under the current RLS context."""
    return latest_configuration(clinic_id)
