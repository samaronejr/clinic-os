"""Validate the numeric CNPJ format used by organization registration."""

from __future__ import annotations

from typing import Final

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

CNPJ_LENGTH: Final = 14
CNPJ_BASE_LENGTH: Final = 12
CHECK_MODULUS: Final = 11
ZERO_REMAINDER_LIMIT: Final = 2
CHECK_WEIGHTS: Final = (
    (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
    (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
)


def validate_cnpj(value: str) -> str:
    """Normalize supported punctuation and verify both CNPJ check digits."""
    digits = value.strip().replace(".", "").replace("/", "").replace("-", "")
    if (
        len(digits) != CNPJ_LENGTH
        or not digits.isascii()
        or not digits.isdigit()
        or len(set(digits)) == 1
    ):
        message = _("Enter a valid CNPJ.")
        raise ValidationError(message, code="invalid_cnpj")
    candidate = digits[:CNPJ_BASE_LENGTH]
    for weights in CHECK_WEIGHTS:
        remainder = (
            sum(
                int(digit) * weight
                for digit, weight in zip(candidate, weights, strict=True)
            )
            % CHECK_MODULUS
        )
        check_digit = (
            0 if remainder < ZERO_REMAINDER_LIMIT else CHECK_MODULUS - remainder
        )
        candidate += str(check_digit)
    if candidate != digits:
        message = _("Enter a valid CNPJ.")
        raise ValidationError(message, code="invalid_cnpj")
    return digits
