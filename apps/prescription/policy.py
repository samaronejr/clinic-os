"""Task-6 approvals are unavailable; only a visibly synthetic draft is supported."""

from django.core.exceptions import ValidationError

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


def validate_category(category: str) -> str:
    """Never infer a category or drug safety from medication text.

    No real category has a confirmed issuance contract in 2026-09-24-v2.
    This synthetic contract authorizes draft rehearsal only, not issuance.
    Controlled, notification, ordinary non-controlled and unknown categories
    all fail closed rather than inheriting the synthetic exception.
    """
    if category != SYNTHETIC_CATEGORY:
        message = (
            "Categoria sem contrato de emissão confirmado. "
            "Use apenas o ensaio sintético."
        )
        raise ValidationError(message, code="unsupported_category")
    return SYNTHETIC_CONTRACT


def validate_items(items: list[dict[str, str]]) -> None:
    """Validate bounded explicit text without trimming or altering entered content."""
    if not 1 <= len(items) <= MAX_ITEMS:
        message = "Informe de 1 a 20 itens."
        raise ValidationError(message, code="invalid_items")
    for item in items:
        if set(item) != set(ITEM_LIMITS) or any(
            not isinstance(value, str)
            or len(value) > ITEM_LIMITS[field]
            or "\x00" in value
            or (field != "instructions" and not value.strip())
            for field, value in item.items()
        ):
            message = "Revise os campos do medicamento."
            raise ValidationError(message, code="invalid_item")
