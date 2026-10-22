"""BMI = kilograms / metres squared; CDC formula, no percentile interpretation."""

from collections.abc import Mapping
from decimal import Decimal, localcontext
from typing import Final

from apps.ehr.calculators.contracts import (
    ARITHMETIC_CONTEXT,
    Calculation,
    Refusal,
    number,
)

SOURCE: Final = "https://www.cdc.gov/bmi/about/index.html"
KEYS: Final = frozenset({"weight", "weight_unit", "height", "height_unit"})


def compute(inputs: Mapping[str, str]) -> Calculation | Refusal:
    """Convert explicit SI units, then round once to two decimal places."""
    if set(inputs) != KEYS:
        return Refusal("missing_inputs" if KEYS - inputs.keys() else "invalid_inputs")
    weight = number(inputs["weight"])
    height = number(inputs["height"])
    weight_factor = {"kg": Decimal(1), "g": Decimal("0.001")}.get(inputs["weight_unit"])
    height_factor = {"m": Decimal(1), "cm": Decimal("0.01")}.get(inputs["height_unit"])
    if (
        weight is None
        or height is None
        or weight_factor is None
        or height_factor is None
        or weight <= 0
        or height <= 0
    ):
        return Refusal("invalid_inputs")
    with localcontext(ARITHMETIC_CONTEXT):
        result = (weight * weight_factor / (height * height_factor) ** 2).quantize(
            Decimal("0.01")
        )
    return Calculation("bmi", 1, str(result), "kg/m2", SOURCE)
