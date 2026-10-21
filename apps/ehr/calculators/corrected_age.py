"""AAP corrected age in weeks, including explicit negative pre-due-date values."""

from collections.abc import Mapping
from decimal import Decimal, localcontext
from typing import Final

from apps.ehr.calculators.contracts import (
    ARITHMETIC_CONTEXT,
    Calculation,
    Refusal,
    number,
)

SOURCE: Final = (
    "https://www.healthychildren.org/English/ages-stages/baby/preemie/"
    "Pages/Corrected-Age-For-Preemies.aspx"
)
KEYS: Final = frozenset({"chronological_age", "gestational_age"})
TERM_WEEKS: Final = 40
MAX_CHRONOLOGICAL_WEEKS: Final = 104


def compute(inputs: Mapping[str, str]) -> Calculation | Refusal:
    """Subtract weeks before 40; never clamp a not-yet-due result to zero."""
    if set(inputs) != KEYS:
        return Refusal("missing_inputs" if KEYS - inputs.keys() else "invalid_inputs")
    chronological = number(inputs["chronological_age"])
    gestational = number(inputs["gestational_age"])
    if (
        chronological is None
        or gestational is None
        or chronological > MAX_CHRONOLOGICAL_WEEKS
        or gestational <= 0
        or gestational > TERM_WEEKS
    ):
        return Refusal("invalid_inputs")
    with localcontext(ARITHMETIC_CONTEXT):
        value = (chronological - (TERM_WEEKS - gestational)).quantize(Decimal("0.01"))
    return Calculation("corrected_age", 1, str(value), "wk", SOURCE)
