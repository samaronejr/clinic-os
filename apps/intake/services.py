"""Phase >=1 intake service entrypoints."""

from typing import NoReturn

from apps.intake.access import PatientAccessDeniedError
from apps.intake.patient_creation import (
    PatientBirthDateError,
    PatientCreateInputError,
    PatientIdempotencyConflictError,
    PatientRegistration,
    create_patient,
)
from apps.intake.patient_search import (
    PatientSearchInputError,
    PatientSearchItem,
    PatientSearchPage,
    search_patients,
)

__all__ = (
    "PatientAccessDeniedError",
    "PatientBirthDateError",
    "PatientCreateInputError",
    "PatientIdempotencyConflictError",
    "PatientRegistration",
    "PatientSearchInputError",
    "PatientSearchItem",
    "PatientSearchPage",
    "create_patient",
    "search_patients",
    "submit_intake",
)


def submit_intake() -> NoReturn:
    """Submit patient intake when the intake domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)
