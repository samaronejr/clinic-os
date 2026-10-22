"""Data-only template definitions; no field or phrase supplies a clinical answer."""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from apps.core.json_schema import schema_validator
from apps.identity.overlay_content import validate_overlay_text

if TYPE_CHECKING:
    from uuid import UUID

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "docs/clinical/template-schema-v2.json"
)
SOAP_FIELDS = ("subjective", "objective", "assessment", "plan")
MAX_PROMPT = 1000
_VALIDATOR = schema_validator(SCHEMA_PATH)


def _plain_data(value: object) -> None:
    """Reject executable markup recursively, not serialized JSON punctuation."""
    match value:
        case str():
            validate_overlay_text(value)
        case dict():
            for item in value.values():
                _plain_data(item)
        case list():
            for item in value:
                _plain_data(item)
        case float() if not math.isfinite(value):
            raise ValidationError(_("Invalid template definition."))


def validate_template(prompts: object, *, clinician_id: UUID) -> None:
    """Validate v1 unchanged or v2 against the shipped closed server schema."""
    invalid = _("Invalid template definition.")
    if not isinstance(prompts, dict):
        raise ValidationError(invalid)
    if "schema_version" not in prompts:
        if set(prompts) != set(SOAP_FIELDS) or any(
            not isinstance(value, str) or len(value) > MAX_PROMPT
            for value in prompts.values()
        ):
            raise ValidationError(invalid)
    else:
        if not _VALIDATOR.is_valid(prompts):
            # Schema errors include the instance, so never expose or log them.
            raise ValidationError(invalid)
        fields = prompts["fields"]
        phrases = prompts["phrases"]
        if len({field["id"] for field in fields}) != len(fields) or len(
            {phrase["id"] for phrase in phrases}
        ) != len(phrases):
            raise ValidationError(invalid)
        for field in fields:
            if field["type"] == "number" and (
                field["range"]["min"] > field["range"]["max"]
            ):
                raise ValidationError(invalid)
        for phrase in phrases:
            if phrase["scope"] == "clinician" and phrase["clinician_id"] != str(
                clinician_id
            ):
                raise ValidationError(invalid)
    _plain_data(prompts)
