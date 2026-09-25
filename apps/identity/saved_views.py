"""Per-user saved workspace views, stored server-side (never in the browser).

Authority comes from the transaction's GUCs: row security limits every read
and write to the current user, the current tenant and a clinic where the user
still holds a role. Parameters are closed slugs, so no saved view can carry
patient data; ``apps.core.saved_views`` decides which destinations and values
the workspace offers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.identity.current_context import current_actor_id
from apps.identity.models import Clinic, SavedView

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

MAX_PARAMS: Final = 4
MAX_ACTIVE_VIEWS: Final = 20
_SLUG: Final = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class SavedViewError(ValueError):
    """Reject a malformed, unknown or foreign saved view without detail."""

    def __init__(self) -> None:
        """Expose one payload-free message."""
        super().__init__("saved view is unavailable")


@dataclass(frozen=True, slots=True)
class SavedViewRecord:
    """One active saved view of the current user."""

    id: UUID
    destination: str
    params: dict[str, str]


def _validated(destination: object, params: object) -> tuple[str, dict[str, str]]:
    if not isinstance(destination, str) or not _SLUG.fullmatch(destination):
        raise SavedViewError
    if not isinstance(params, dict) or len(params) > MAX_PARAMS:
        raise SavedViewError
    clean: dict[str, str] = {}
    for key, value in params.items():
        if not (
            isinstance(key, str)
            and isinstance(value, str)
            and _SLUG.fullmatch(key)
            and _SLUG.fullmatch(value)
        ):
            raise SavedViewError
        clean[key] = value
    return destination, clean


def _record(row: SavedView) -> SavedViewRecord:
    params = row.params if isinstance(row.params, dict) else {}
    return SavedViewRecord(
        id=row.pk,
        destination=row.destination,
        params={str(key): str(value) for key, value in params.items()},
    )


def list_saved_views(*, clinic_id: UUID) -> tuple[SavedViewRecord, ...]:
    """Return the current user's active views in one clinic, oldest first."""
    rows = SavedView.objects.filter(
        clinic_id=clinic_id, archived_at__isnull=True
    ).order_by("created_at", "id")
    return tuple(_record(row) for row in rows)


def save_view(
    *, clinic_id: UUID, destination: str, params: Mapping[str, str]
) -> SavedViewRecord:
    """Store one view for the current user; saving it twice returns the first."""
    destination, clean = _validated(destination, dict(params))
    clinic = Clinic.objects.filter(pk=clinic_id).first()
    if clinic is None:
        raise SavedViewError
    existing = SavedView.objects.filter(
        clinic_id=clinic_id,
        destination=destination,
        params=clean,
        archived_at__isnull=True,
    ).first()
    if existing is not None:
        return _record(existing)
    if (
        SavedView.objects.filter(clinic_id=clinic_id, archived_at__isnull=True).count()
        >= MAX_ACTIVE_VIEWS
    ):
        raise SavedViewError
    try:
        with transaction.atomic():
            row = SavedView.objects.create(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                user_id=current_actor_id(),
                destination=destination,
                params=clean,
            )
    except DatabaseError as error:  # no role in the clinic, or a racing save
        raise SavedViewError from error
    return _record(row)


def archive_saved_view(*, clinic_id: UUID, view_id: UUID) -> None:
    """Archive one of the current user's views; unknown and foreign ids match."""
    updated = SavedView.objects.filter(
        pk=view_id, clinic_id=clinic_id, archived_at__isnull=True
    ).update(archived_at=timezone.now())
    if updated != 1:
        raise SavedViewError
