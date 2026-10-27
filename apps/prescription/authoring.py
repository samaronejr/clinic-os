"""Native POST catalog actions preserve entered directions without saving them."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.ehr.services import ClinicalAccessDeniedError
from apps.prescription.catalog import catalog_entries, favorite_entries, set_favorite
from apps.prescription.models import MedicationCatalogEntry
from apps.prescription.policy import ITEM_LIMITS, MAX_ITEMS

if TYPE_CHECKING:
    from django.http import HttpRequest, QueryDict

    from apps.prescription.models import MedicationFavorite, PrescriptionDraft


class CatalogContext(TypedDict):
    """Public source rows alongside private bookmarks, never a class suggestion."""

    catalog_query: str
    catalog_rows: tuple[MedicationCatalogEntry, ...]
    favorites: tuple[MedicationFavorite, ...]
    catalog_error: str


def author_data(request: HttpRequest, draft: PrescriptionDraft) -> QueryDict:
    """Apply at most one explicit catalog action to the bound native form."""
    data = request.POST.copy()
    controls = [
        key
        for key in ("use_catalog_id", "favorite_add_id", "favorite_remove_id")
        if data.get(key)
    ]
    if len(controls) > 1 or (controls and data.get("action")):
        raise ClinicalAccessDeniedError
    if not controls:
        if data.get("action") not in {"save", "discard", "catalog_search"}:
            raise ClinicalAccessDeniedError
        return data
    control = controls[0]
    entry_id = UUID(data.get(control, ""))
    if control != "use_catalog_id":
        set_favorite(
            clinic_id=draft.clinic_id,
            entry_id=entry_id,
            active=control == "favorite_add_id",
        )
        return data
    entry = MedicationCatalogEntry.objects.filter(pk=entry_id).first()
    if entry is None:
        raise ClinicalAccessDeniedError
    total = int(data.get("items-TOTAL_FORMS", "0"))
    if not 1 <= total <= MAX_ITEMS:
        raise ClinicalAccessDeniedError
    position = next(
        (
            index
            for index in range(total)
            if not any(
                data.get(f"items-{index}-{field}", "").strip()
                for field in (
                    *ITEM_LIMITS,
                    "dose_unit",
                    "catalog_entry_id",
                    "medication_statement_id",
                )
            )
        ),
        total,
    )
    if position == MAX_ITEMS:
        raise ValidationError(
            _("The draft already has 20 items."), code="invalid_items"
        )
    data["items-TOTAL_FORMS"] = str(max(total, position + 1))
    data[f"items-{position}-medication_description"] = entry.description
    data[f"items-{position}-strength_form"] = entry.strength_form
    data[f"items-{position}-catalog_entry_id"] = str(entry.pk)
    return data


def catalog_context(request: HttpRequest, *, clinic_id: UUID) -> CatalogContext:
    """Keep query errors inline and never drop the author's bound medication input."""
    query = request.POST.get("catalog_query", "") if request.method == "POST" else ""
    rows: tuple[MedicationCatalogEntry, ...] = ()
    error = ""
    if request.POST.get("action") == "catalog_search":
        try:
            rows = catalog_entries(clinic_id=clinic_id, query=query)
        except ValidationError as refusal:
            error = refusal.messages[0]
    return {
        "catalog_query": query,
        "catalog_rows": rows,
        "favorites": favorite_entries(clinic_id=clinic_id),
        "catalog_error": error,
    }
