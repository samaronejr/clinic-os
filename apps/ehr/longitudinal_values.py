"""Versioned local observation vocabulary and exact decimal arithmetic."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise


class ProvenanceTag(models.TextChoices):
    """Origin is retained separately when an explicit review confirms an item."""

    REPORTED = "reported_by_patient", _("Reported by patient")
    OBSERVED = "observed", _("Observed")
    IMPORTED = "imported", _("Imported")
    AI_DRAFTED = "ai_drafted", _("AI draft")
    CONFIRMED = "clinician_confirmed", _("Clinician confirmed")


ORIGINS: Final = tuple(p for p in ProvenanceTag.values if p != ProvenanceTag.CONFIRMED)
CODE_SET_VERSION: Final = 1
INVALID_NUMBER: Final = _("Enter a decimal number without thousands separators.")
INVALID_UNIT: Final = _("Choose a valid unit for this observation.")
INVALID_VALUE: Final = _("Enter a valid value for this observation.")


@dataclass(frozen=True, slots=True)
class ObservationCode:
    """A code's unit and representation are immutable within its version."""

    label: StrOrPromise
    unit: str
    choices: tuple[str, ...] = ()


CODES: Final = MappingProxyType(
    {
        "systolic_bp": ObservationCode(_("Systolic blood pressure"), "mm[Hg]"),
        "diastolic_bp": ObservationCode(_("Diastolic blood pressure"), "mm[Hg]"),
        "heart_rate": ObservationCode(_("Heart rate"), "/min"),
        "respiratory_rate": ObservationCode(_("Respiratory rate"), "/min"),
        "temperature": ObservationCode(_("Temperature"), "Cel"),
        "spo2": ObservationCode(_("Oxygen saturation"), "%"),
        "weight": ObservationCode(_("Weight"), "kg"),
        "height": ObservationCode(_("Height"), "cm"),
        "bmi": ObservationCode(_("Body mass index"), "kg/m2"),
        "pain_present": ObservationCode(
            _("Pain reported"), "1", ("yes", "no", "unknown")
        ),
    }
)


def decimal_string(value: str) -> str:
    """Preserve decimal precision, accepting one comma or point, never a float."""
    if (
        not isinstance(value, str)
        or re.fullmatch(r"-?[0-9]{1,12}(?:[.,][0-9]{1,6})?", value.strip()) is None
    ):
        raise ValidationError(INVALID_NUMBER, code="invalid_decimal")
    return value.strip().replace(",", ".")


def observation_value(*, kind: str, value: str, unit: str) -> str:
    """Validate a local code's representation without clinical interpretation."""
    code = CODES.get(kind)
    if code is None or code.unit != unit:
        raise ValidationError(INVALID_UNIT, code="invalid_observation_unit")
    if code.choices:
        if value not in code.choices:
            raise ValidationError(INVALID_VALUE, code="invalid_observation_value")
        return value
    return decimal_string(value)


def bmi(*, weight: str, height: str) -> str:
    """Compute kg/(cm/100)^2, rounded once to 0.01 with decimal half-even."""
    kilograms = Decimal(decimal_string(weight))
    centimetres = Decimal(decimal_string(height))
    if kilograms <= 0 or centimetres <= 0:
        raise ValidationError(INVALID_VALUE, code="invalid_bmi_inputs")
    with localcontext() as context:
        context.prec = 48
        return str(
            (kilograms / (centimetres / Decimal(100)) ** 2).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_EVEN
            )
        )
