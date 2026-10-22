"""Scoped staff labels for body-only lifecycle controls."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.db import connection

from apps.identity.lifecycle_authority import require_staff_management
from apps.identity.models import UserClinicRole


@dataclass(frozen=True, slots=True)
class StaffMember:
    """Only staff identity and current role, never patient or authenticator data."""

    user_id: UUID
    label: str
    bundle_label: str


def staff_directory(*, clinic_id: UUID) -> tuple[StaffMember, ...]:
    """Read current authorized staff through the actual narrow resolver."""
    require_staff_management(clinic_id=clinic_id)
    labels = dict(UserClinicRole.Role.choices)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.identity_staff_directory(%s)", [clinic_id]
        )
        rows = cursor.fetchall()
    return tuple(
        StaffMember(
            user_id=UUID(str(user_id)),
            label=label,
            bundle_label=str(labels[bundle]),
        )
        for user_id, label, bundle in rows
    )
