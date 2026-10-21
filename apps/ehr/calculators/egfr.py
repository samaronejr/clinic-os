"""Published 2021 CKD-EPI creatinine equation, adults only, without a race term."""

from collections.abc import Mapping
from decimal import Decimal, localcontext
from typing import Final

from apps.ehr.calculators.contracts import (
    ARITHMETIC_CONTEXT,
    Calculation,
    Refusal,
    number,
)

SOURCE: Final = "https://www.kidney.org/ckd-epi-creatinine-equation-2021"
KEYS: Final = frozenset({"age", "sex", "creatinine", "creatinine_unit"})
ADULT_YEARS: Final = 18


def compute(inputs: Mapping[str, str]) -> Calculation | Refusal:
    """Return indexed eGFR, not an AKI assessment, dose or disease classification."""
    if set(inputs) != KEYS:
        return Refusal("missing_inputs" if KEYS - inputs.keys() else "invalid_inputs")
    age = number(inputs["age"])
    creatinine = number(inputs["creatinine"])
    factor = {"mg/dL": Decimal(1), "umol/L": Decimal("88.4")}.get(
        inputs["creatinine_unit"]
    )
    parameters = {
        "female": (Decimal("0.7"), Decimal("-0.241"), Decimal("1.012")),
        "male": (Decimal("0.9"), Decimal("-0.302"), Decimal(1)),
    }.get(inputs["sex"])
    if (
        age is None
        or creatinine is None
        or age < ADULT_YEARS
        or age != age.to_integral_value()
        or creatinine <= 0
        or factor is None
        or parameters is None
    ):
        return Refusal("invalid_inputs")
    kappa, alpha, coefficient = parameters
    with localcontext(ARITHMETIC_CONTEXT):
        ratio = creatinine / factor / kappa
        value = (
            Decimal(142)
            * min(ratio, Decimal(1)) ** alpha
            * max(ratio, Decimal(1)) ** Decimal("-1.200")
            * Decimal("0.9938") ** age
            * coefficient
        ).quantize(Decimal("0.01"))
    return Calculation("egfr", 1, str(value), "mL/min/1.73m2", SOURCE)
