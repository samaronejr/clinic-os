"""Phase 1A scheduling service entrypoints."""

from apps.scheduling.access import (
    AppointmentAccessDeniedError,
    AvailabilityAccessDeniedError,
)
from apps.scheduling.agenda_queries import (
    AGENDA_PAGE_SIZE,
    AgendaInputError,
    AgendaItem,
    AgendaPage,
    view_agenda,
)
from apps.scheduling.appointment_cancellation import cancel_appointment
from apps.scheduling.appointment_creation import create_appointment
from apps.scheduling.appointment_errors import (
    AppointmentAvailabilityError,
    AppointmentCancellationConflictError,
    AppointmentCancellationInputError,
    AppointmentCreateInputError,
    AppointmentIdempotencyConflictError,
    AppointmentPractitionerError,
    AppointmentRescheduleInputError,
    AppointmentTerminalError,
    SlotConflict,
)
from apps.scheduling.appointment_rescheduling import reschedule_appointment
from apps.scheduling.appointment_values import AppointmentLocalRange
from apps.scheduling.appointment_view import (
    AppointmentTransitionView,
    view_appointment_for_transition,
)
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
from apps.scheduling.booking_queries import (
    BookingAvailabilityWindow,
    BookingPractitioner,
    BookingPreparation,
    prepare_booking,
)

__all__ = (
    "AGENDA_PAGE_SIZE",
    "AgendaInputError",
    "AgendaItem",
    "AgendaPage",
    "AppointmentAccessDeniedError",
    "AppointmentAvailabilityError",
    "AppointmentCancellationConflictError",
    "AppointmentCancellationInputError",
    "AppointmentCreateInputError",
    "AppointmentIdempotencyConflictError",
    "AppointmentLocalRange",
    "AppointmentPractitionerError",
    "AppointmentRescheduleInputError",
    "AppointmentTerminalError",
    "AppointmentTransitionView",
    "AvailabilityAccessDeniedError",
    "AvailabilityCreateInputError",
    "AvailabilityHasAppointmentsError",
    "AvailabilityIdempotencyConflictError",
    "AvailabilityOverlapError",
    "AvailabilityPractitionerError",
    "AvailabilityViewItem",
    "BookingAvailabilityWindow",
    "BookingPractitioner",
    "BookingPreparation",
    "SlotConflict",
    "cancel_appointment",
    "create_appointment",
    "create_availability",
    "prepare_booking",
    "reschedule_appointment",
    "retire_availability",
    "view_agenda",
    "view_appointment_for_transition",
    "view_availability",
)
