"""GAD-7 item-score arithmetic, without instrument text or diagnostic thresholds."""

from collections.abc import Mapping
from typing import Final

from apps.ehr.calculators.contracts import Calculation, Refusal
from apps.ehr.calculators.phq9 import SCORES, SOURCE

ITEMS: Final = tuple(f"item_{index}" for index in range(1, 8))


def compute(inputs: Mapping[str, str]) -> Calculation | Refusal:
    """Sum all seven official item scores; never infer or complete an answer."""
    if set(inputs) != set(ITEMS):
        return Refusal(
            "missing_inputs" if set(ITEMS) - inputs.keys() else "invalid_inputs"
        )
    if any(value not in SCORES for value in inputs.values()):
        return Refusal("invalid_inputs")
    return Calculation(
        "gad7", 1, str(sum(int(inputs[key]) for key in ITEMS)), "1", SOURCE
    )
