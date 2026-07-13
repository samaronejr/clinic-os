"""Phase >=1 ehr service entrypoints."""

from typing import NoReturn


def record_clinical_note() -> NoReturn:
    """Record a clinical note when the ehr domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)
