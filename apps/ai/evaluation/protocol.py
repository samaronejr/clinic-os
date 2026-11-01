"""The engineering rubric is fixed before scoring; live thresholds remain EG-3."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import TYPE_CHECKING, Final

import rfc8785

if TYPE_CHECKING:
    from collections.abc import Mapping

ERROR_LABELS: Final = (
    "significant_omission",
    "hallucination",
    "speaker_attribution",
    "negation",
    "chronology",
    "units_decimals",
    "medication_name_dose",
    "allergy_handling",
    "unsupported_citation",
    "incorrect_action",
    "policy_violation",
    "unauthorized_effect",
    "duplicate_effect",
    "missed_escalation",
    "critical_error",
)
CLINICAL_LABELS: Final = ERROR_LABELS[:8]
MAX_CASES: Final = 1000
MIN_CLUSTERS: Final = 2
PARTS_PER_MILLION: Final = 1_000_000
MIN_BOOTSTRAP_ITERATIONS: Final = 100
MAX_BOOTSTRAP_ITERATIONS: Final = 10000
MAX_CORRECTION_MS: Final = 3_600_000
ENGINEERING_THRESHOLDS: Final = {
    "minimum_cases": 12,
    "minimum_encounters": 4,
    "maximum_critical_errors": 0,
    "maximum_error_rate_ppm": 0,
    "minimum_citation_support_ppm": 1_000_000,
    "minimum_action_correctness_ppm": 1_000_000,
    "bootstrap_seed": 47,
    "bootstrap_iterations": 2000,
}


class EvaluationInputError(ValueError):
    """Keep malformed labels, manifests and selectors out of error messages."""

    def __init__(self) -> None:
        """Expose only a stable reason."""
        super().__init__("invalid AI evaluation input")


def threshold_document(thresholds: Mapping[str, int]) -> str:
    """Canonicalize a closed integer-only protocol before its first candidate."""
    if (
        set(thresholds) != set(ENGINEERING_THRESHOLDS)
        or any(type(value) is not int for value in thresholds.values())
        or not 1 <= thresholds["minimum_cases"] <= MAX_CASES
        or not MIN_CLUSTERS
        <= thresholds["minimum_encounters"]
        <= thresholds["minimum_cases"]
        or thresholds["maximum_critical_errors"] != 0
        or not 0 <= thresholds["maximum_error_rate_ppm"] <= PARTS_PER_MILLION
        or not 0 <= thresholds["minimum_citation_support_ppm"] <= PARTS_PER_MILLION
        or not 0 <= thresholds["minimum_action_correctness_ppm"] <= PARTS_PER_MILLION
        or not 0 <= thresholds["bootstrap_seed"] <= 2**31 - 1
        or not MIN_BOOTSTRAP_ITERATIONS
        <= thresholds["bootstrap_iterations"]
        <= MAX_BOOTSTRAP_ITERATIONS
    ):
        raise EvaluationInputError
    return rfc8785.dumps(dict(thresholds)).decode()


def threshold_hash(thresholds: Mapping[str, int]) -> str:
    """Assign a different registration to every threshold or sample-size change."""
    return sha256(threshold_document(thresholds).encode()).hexdigest()


def validate_labels(raw: object) -> dict[str, int]:
    """Adjudication accepts only bounded structured counts, never clinical prose."""
    if not isinstance(raw, dict) or set(raw) != {*ERROR_LABELS, "correction_ms"}:
        raise EvaluationInputError
    labels: dict[str, int] = {}
    for key in (*ERROR_LABELS, "correction_ms"):
        value = raw[key]
        if type(value) is not int or not 0 <= value <= (
            MAX_CORRECTION_MS if key == "correction_ms" else 1
        ):
            raise EvaluationInputError
        labels[key] = value
    return labels


def category(labels: Mapping[str, int]) -> str:
    """Kappa compares the whole binary taxonomy, excluding each rater's duration."""
    return json.dumps([labels[key] for key in ERROR_LABELS], separators=(",", ":"))
