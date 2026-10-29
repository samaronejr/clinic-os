"""Pure, explicitly versioned dispatch; never execute user-supplied expressions."""

from collections.abc import Mapping
from typing import Final

from apps.ehr.calculators.bmi import compute as compute_bmi
from apps.ehr.calculators.catalog import DEFINITIONS
from apps.ehr.calculators.contracts import Calculation, Refusal
from apps.ehr.calculators.corrected_age import compute as compute_corrected_age
from apps.ehr.calculators.egfr import compute as compute_egfr
from apps.ehr.calculators.gad7 import compute as compute_gad7
from apps.ehr.calculators.phq9 import compute as compute_phq9

IMPLEMENTATIONS: Final = {
    "bmi": compute_bmi,
    "egfr": compute_egfr,
    "corrected_age": compute_corrected_age,
    "phq9": compute_phq9,
    "gad7": compute_gad7,
}


def calculate(
    *, key: str, version: int, inputs: Mapping[str, str]
) -> Calculation | Refusal:
    """Refuse unknown versions and unlicensed data before interpreting any input."""
    definition = DEFINITIONS.get(key)
    if definition is None or type(version) is not int or version != definition.version:
        return Refusal("unknown_calculator")
    if definition.license_status == "pending":
        return Refusal("license_pending")
    return IMPLEMENTATIONS[key](inputs)
