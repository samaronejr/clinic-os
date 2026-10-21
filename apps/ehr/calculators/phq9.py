"""PHQ-9 item-score arithmetic, without instrument text or diagnostic thresholds."""

from collections.abc import Mapping
from typing import Final

from apps.ehr.calculators.contracts import Calculation, Refusal

SOURCE: Final = (
    "https://www.phqscreeners.com/images/sites/g/files/g10016261/"
    "f/201412/instructions.pdf"
)
ITEMS: Final = tuple(f"item_{index}" for index in range(1, 10))
SCORES: Final = frozenset({"0", "1", "2", "3"})


def compute(inputs: Mapping[str, str]) -> Calculation | Refusal:
    """Sum all nine official item scores; never prorate missing responses."""
    if set(inputs) != set(ITEMS):
        return Refusal(
            "missing_inputs" if set(ITEMS) - inputs.keys() else "invalid_inputs"
        )
    if any(value not in SCORES for value in inputs.values()):
        return Refusal("invalid_inputs")
    return Calculation(
        "phq9", 1, str(sum(int(inputs[key]) for key in ITEMS)), "1", SOURCE
    )
