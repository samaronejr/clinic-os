"""Manual draft classes, bounded clinician text and explicit structured units."""

from typing import Final

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.prescription.class_values import CLASSES_CONTRACT, PrescriptionClass

SYNTHETIC_CATEGORY = "synthetic_non_controlled"
SYNTHETIC_CONTRACT = "synthetic-draft-v1"
MAX_ITEMS = 20
ITEM_LIMITS = {
    "medication_description": 240,
    "strength_form": 160,
    "dose": 160,
    "route": 80,
    "frequency": 160,
    "duration": 160,
    "quantity": 80,
    "instructions": 2000,
}
DOSE_UNIT_LABELS: Final = {
    "tablet": _("Tablet"),
    "capsule": _("Capsule"),
    "drop": _("Drop"),
    **{unit: unit for unit in ("mL", "mg", "g", "ug", "IU")},
}


def validate_category(category: str | None) -> str:
    """Never infer a category or drug safety from medication text.

    A known class permits draft preparation, not electronic issuance.
    None remains unset; no service or catalog chooses a class on its behalf.
    """
    if category is not None and category not in PrescriptionClass.values:
        message = _("Choose a valid prescription class.")
        raise ValidationError(message, code="unsupported_category")
    return SYNTHETIC_CONTRACT if category == SYNTHETIC_CATEGORY else CLASSES_CONTRACT


def validate_items(items: list[dict[str, str]]) -> None:
    """Validate bounded explicit text without trimming or altering entered content."""
    if not 1 <= len(items) <= MAX_ITEMS:
        message = "Informe de 1 a 20 itens."
        raise ValidationError(message, code="invalid_items")
    optional = {"dose_unit": 16, "catalog_entry_id": 36, "medication_statement_id": 36}
    limits = {**ITEM_LIMITS, **optional}
    for item in items:
        if not set(ITEM_LIMITS) <= set(item) <= set(limits) or any(
            not isinstance(value, str)
            or len(value) > limits[field]
            or "\x00" in value
            or (
                field in ITEM_LIMITS
                and field != "instructions"
                and not value.strip()
                and not (field == "quantity" and item.get("dose_unit"))
            )
            for field, value in item.items()
        ):
            message = "Revise os campos do medicamento."
            raise ValidationError(message, code="invalid_item")
