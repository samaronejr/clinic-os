"""Phase 1A scheduling service entrypoints."""

from typing import NoReturn

from apps.scheduling.access import AvailabilityAccessDeniedError
from apps.scheduling.availability_creation import (
    AvailabilityCreateInputError,
    AvailabilityIdempotencyConflictError,
    AvailabilityOverlapError,
    AvailabilityPractitionerError,
    create_availability,
)
from apps.scheduling.availability_retirement import (
    AvailabilityHasAppointmentsError,
    retire_availability,
)
from apps.scheduling.availability_view import AvailabilityViewItem, view_availability

__all__ = (
    "AvailabilityAccessDeniedError",
    "AvailabilityCreateInputError",
    "AvailabilityHasAppointmentsError",
    "AvailabilityIdempotencyConflictError",
    "AvailabilityOverlapError",
    "AvailabilityPractitionerError",
    "AvailabilityViewItem",
    "create_appointment",
    "create_availability",
    "retire_availability",
    "view_availability",
)


def create_appointment() -> NoReturn:
    """Create an appointment when the scheduling domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)
