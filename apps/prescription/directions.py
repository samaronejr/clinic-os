"""Exact arithmetic on explicit quantities; no dose suggestion or conversion."""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Final, TypedDict
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.ehr.longitudinal_models import MedicationStatement
from apps.ehr.longitudinal_values import decimal_string
from apps.ehr.services import ClinicalAccessDeniedError
from apps.prescription.models import MedicationCatalogEntry, PrescriptionItem
from apps.prescription.policy import DOSE_UNIT_LABELS, ITEM_LIMITS

if TYPE_CHECKING:
    from apps.prescription.models import PrescriptionDraft

LINK_FIELDS: Final = ("catalog_entry_id", "medication_statement_id")
OPTIONAL_ITEM_FIELDS: Final = ("dose_unit", *LINK_FIELDS)


class DirectionPayload(TypedDict):
    """Decimal strings stay exact inside the encrypted snapshot."""

    dose_value: str
    dose_unit: str
    route: str
    times_per_day: str
    duration_days: str
    quantity_value: str
    quantity_unit: str


def structured_directions(item: dict[str, str]) -> DirectionPayload | None:
    """Multiply entered dose units by administrations/day and days, without rounding."""
    unit = item.get("dose_unit", "")
    if not unit:
        return None
    if unit not in DOSE_UNIT_LABELS:
        raise ValidationError(_("Choose a valid dose unit."), code="invalid_directions")
    dose, frequency, duration = (
        decimal_string(item[field]) for field in ("dose", "frequency", "duration")
    )
    if any(
        not 0 < Decimal(value) <= maximum
        for value, maximum in ((dose, 1000000), (frequency, 24), (duration, 3650))
    ):
        raise ValidationError(
            _("Check the structured directions."), code="invalid_directions"
        )
    with localcontext() as context:
        context.prec = 42
        quantity = format(
            (Decimal(dose) * Decimal(frequency) * Decimal(duration)).normalize(), "f"
        )
    return {
        "dose_value": dose,
        "dose_unit": unit,
        "route": item["route"],
        "times_per_day": frequency,
        "duration_days": duration,
        "quantity_value": quantity,
        "quantity_unit": unit,
    }


def item_snapshot(
    *, draft: PrescriptionDraft, item: dict[str, str], position: int
) -> PrescriptionItem:
    """Resolve untrusted links against the saved draft's canonical patient scope."""
    links: dict[str, UUID | None] = {}
    for field in LINK_FIELDS:
        try:
            links[field] = UUID(item[field]) if item.get(field) else None
        except (ValueError, TypeError, AttributeError):
            raise ClinicalAccessDeniedError from None
    catalog_id, statement_id = (
        links["catalog_entry_id"],
        links["medication_statement_id"],
    )
    if (
        catalog_id is not None
        and not MedicationCatalogEntry.objects.filter(pk=catalog_id).exists()
    ) or (
        statement_id is not None
        and not MedicationStatement.objects.filter(
            pk=statement_id,
            organization_id=draft.organization_id,
            clinic_id=draft.clinic_id,
            patient_id=draft.patient_id,
        ).exists()
    ):
        raise ClinicalAccessDeniedError
    directions = structured_directions(item)
    text = {field: item[field] for field in ITEM_LIMITS}
    if directions is not None:
        text["quantity"] = directions["quantity_value"]
    return PrescriptionItem(
        organization_id=draft.organization_id,
        draft=draft,
        version=draft.version,
        position=position,
        directions=directions,
        **links,
        **text,
    )


def snapshot_items(draft: PrescriptionDraft) -> list[dict[str, str]]:
    """Expose only saved form fields; no clinical content is put in browser storage."""
    items = []
    for row in PrescriptionItem.objects.filter(draft=draft, version=draft.version):
        item = {field: getattr(row, field) for field in ITEM_LIMITS}
        if row.directions is not None:
            item["dose_unit"] = row.directions["dose_unit"]
        for field in LINK_FIELDS:
            if (value := getattr(row, field)) is not None:
                item[field] = str(value)
        items.append(item)
    return items
