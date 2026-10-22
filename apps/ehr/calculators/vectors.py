"""Published examples and independently derived formula boundaries, not thresholds."""

from types import MappingProxyType
from typing import Final

from apps.ehr.calculators.bmi import SOURCE as BMI_SOURCE
from apps.ehr.calculators.contracts import BoundaryVector, Calculation, Refusal
from apps.ehr.calculators.corrected_age import SOURCE as CORRECTED_AGE_SOURCE
from apps.ehr.calculators.egfr import SOURCE as EGFR_SOURCE
from apps.ehr.calculators.phq9 import SOURCE as PHQ9_SOURCE


def _vector(inputs: dict[str, str], expected: Calculation | Refusal) -> BoundaryVector:
    return BoundaryVector(tuple(inputs.items()), expected)


VECTORS: Final = MappingProxyType(
    {
        "bmi": (
            _vector(
                {
                    "weight": "16.9",
                    "weight_unit": "kg",
                    "height": "105.4",
                    "height_unit": "cm",
                },
                Calculation("bmi", 1, "15.21", "kg/m2", BMI_SOURCE),
            ),
            _vector(
                {
                    "weight": "70000",
                    "weight_unit": "g",
                    "height": "1.75",
                    "height_unit": "m",
                },
                Calculation("bmi", 1, "22.86", "kg/m2", BMI_SOURCE),
            ),
            _vector(
                {
                    "weight": "70",
                    "weight_unit": "kg",
                    "height": "0",
                    "height_unit": "m",
                },
                Refusal("invalid_inputs"),
            ),
            _vector({}, Refusal("missing_inputs")),
        ),
        "egfr": (
            *tuple(
                _vector(
                    {
                        "age": age,
                        "sex": sex,
                        "creatinine": creatinine,
                        "creatinine_unit": "mg/dL",
                    },
                    Calculation("egfr", 1, value, "mL/min/1.73m2", EGFR_SOURCE),
                )
                for age, sex, creatinine, value in (
                    ("18", "male", "0.9", "126.96"),
                    ("18", "female", "0.7", "128.48"),
                    ("50", "male", "0.6", "117.60"),
                    ("50", "male", "1", "91.69"),
                    ("50", "female", "0.6", "109.28"),
                    ("50", "female", "1.2", "55.15"),
                )
            ),
            _vector({}, Refusal("missing_inputs")),
        ),
        "corrected_age": (
            _vector(
                {"chronological_age": "16", "gestational_age": "32"},
                Calculation("corrected_age", 1, "8.00", "wk", CORRECTED_AGE_SOURCE),
            ),
            _vector(
                {"chronological_age": "7", "gestational_age": "32"},
                Calculation("corrected_age", 1, "-1.00", "wk", CORRECTED_AGE_SOURCE),
            ),
            _vector(
                {"chronological_age": "105", "gestational_age": "32"},
                Refusal("invalid_inputs"),
            ),
            _vector({}, Refusal("missing_inputs")),
        ),
        "growth_percentile": (_vector({}, Refusal("license_pending")),),
        **{
            key: (
                *tuple(
                    _vector(
                        {f"item_{index}": score for index in range(1, count + 1)},
                        Calculation(key, 1, str(count * int(score)), "1", PHQ9_SOURCE),
                    )
                    for score in ("0", "3")
                ),
                _vector({}, Refusal("missing_inputs")),
            )
            for key, count in (("phq9", 9), ("gad7", 7))
        },
    }
)
