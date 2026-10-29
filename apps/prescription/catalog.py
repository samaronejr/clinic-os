"""Physician-scoped catalog search and explicit, retained public-entry bookmarks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.audit.services import record_phase1_event
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import require_current_actor_clinic_roles
from apps.identity.models import Clinic, UserClinicRole
from apps.prescription.models import (
    MedicationCatalogEntry,
    MedicationCatalogImport,
    MedicationFavorite,
)

if TYPE_CHECKING:
    from uuid import UUID

MAX_CATALOG_QUERY: Final = 80


def catalog_entries(
    *, clinic_id: UUID, query: str
) -> tuple[MedicationCatalogEntry, ...]:
    """Search public terminology only; the POST body supplies the bounded query."""
    require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    if len(query) > MAX_CATALOG_QUERY or "\x00" in query:
        raise ValidationError(
            _("Enter at most 80 characters."), code="invalid_catalog_query"
        )
    latest = MedicationCatalogImport.objects.order_by(
        "-retrieved_on", "-loaded_at", "-pk"
    ).first()
    if latest is None:
        return ()
    return tuple(
        MedicationCatalogEntry.objects.filter(
            source=latest, description__icontains=query.strip()
        ).select_related("source")[:20]
    )


def favorite_entries(*, clinic_id: UUID) -> tuple[MedicationFavorite, ...]:
    """Read only this physician's active bookmarks under FORCE RLS."""
    actor = require_current_actor_clinic_roles(
        clinic_id, (UserClinicRole.Role.PHYSICIAN,)
    )
    return tuple(
        MedicationFavorite.objects.filter(
            clinic_id=clinic_id, issuer_id=actor, active=True
        )
        .select_related("entry", "entry__source")
        .order_by("entry__description", "pk")[:50]
    )


def set_favorite(
    *, clinic_id: UUID, entry_id: UUID, active: bool
) -> MedicationFavorite:
    """Bookmark public terminology only, never a default class or suggested dose."""
    actor = require_current_actor_clinic_roles(
        clinic_id, (UserClinicRole.Role.PHYSICIAN,)
    )
    clinic = Clinic.objects.filter(pk=clinic_id).first()
    if (
        clinic is None
        or not MedicationCatalogEntry.objects.filter(pk=entry_id).exists()
    ):
        raise ClinicalAccessDeniedError
    with transaction.atomic():
        favorite, created = (
            MedicationFavorite.objects.select_for_update().get_or_create(
                clinic_id=clinic_id,
                issuer_id=actor,
                entry_id=entry_id,
                defaults={"organization_id": clinic.organization_id, "active": active},
            )
        )
        if created or favorite.active != active:
            if not created:
                favorite.active = active
                favorite.save(update_fields=("active",))
            record_phase1_event(
                "prescription.favorite.added"
                if active
                else "prescription.favorite.removed",
                clinic_id=clinic_id,
                affected_record_id=favorite.pk,
            )
        return favorite
