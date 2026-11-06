"""Pure state-machine oracle shared by services and deterministic regression tests."""

from typing import Final

from apps.insurance.errors import InsuranceConflictError

TRANSITIONS: Final = {
    "guide": {
        ("draft", "validated"),
        ("validated", "sealed"),
    },
    "batch": {
        ("draft", "validated"),
        ("validated", "sealed"),
        ("sealed", "submitting"),
        ("submitting", "submitted"),
        ("submitting", "unknown"),
        ("unknown", "submitted"),
        ("unknown", "acknowledged"),
        ("submitted", "acknowledged"),
        ("acknowledged", "returned"),
    },
    "appeal": {
        ("draft", "approved"),
        ("approved", "submitted"),
    },
}


def require_transition(*, kind: str, current: str, target: str) -> None:
    """Unknown, skipped, reversed and repeated transitions are not stored states."""
    if (current, target) not in TRANSITIONS.get(kind, set()):
        raise InsuranceConflictError
