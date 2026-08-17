"""Phase 1A scheduling service entrypoints."""

from apps.scheduling.access import (
    AppointmentAccessDeniedError,
    AvailabilityAccessDeniedError,
)
from apps.scheduling.appointment_creation import create_appointment
from apps.scheduling.appointment_errors import (
    AppointmentAvailabilityError,
    AppointmentCreateInputError,
    AppointmentIdempotencyConflictError,
    AppointmentPractitionerError,
    SlotConflict,
)
from apps.scheduling.appointment_values import AppointmentLocalRange
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
    "AppointmentAccessDeniedError",
    "AppointmentAvailabilityError",
    "AppointmentCreateInputError",
    "AppointmentIdempotencyConflictError",
    "AppointmentLocalRange",
    "AppointmentPractitionerError",
    "AvailabilityAccessDeniedError",
    "AvailabilityCreateInputError",
    "AvailabilityHasAppointmentsError",
    "AvailabilityIdempotencyConflictError",
    "AvailabilityOverlapError",
    "AvailabilityPractitionerError",
    "AvailabilityViewItem",
    "SlotConflict",
    "create_appointment",
    "create_availability",
    "retire_availability",
    "view_availability",
)
