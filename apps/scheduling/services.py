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
from apps.scheduling.appointment_creation import (
    ServiceBooking,
    create_appointment,
    create_hold,
    create_service_appointment,
)
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
from apps.scheduling.appointment_lifecycle import (
    AppointmentLifecycleError,
    AppointmentLifecycleInputError,
    HoldExpiry,
    arrive,
    book,
    cancel,
    complete,
    expire,
    expire_due_holds,
    hold,
    mark_no_show,
    start,
)
from apps.scheduling.appointment_rescheduling import reschedule_appointment
from apps.scheduling.appointment_series import (
    SeriesBooking,
    SeriesEdit,
    create_series,
    edit_series,
)
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
from apps.scheduling.resource_errors import SchedulingRuleError

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
    "AppointmentLifecycleError",
    "AppointmentLifecycleInputError",
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
    "HoldExpiry",
    "SchedulingRuleError",
    "SeriesBooking",
    "SeriesEdit",
    "ServiceBooking",
    "SlotConflict",
    "arrive",
    "book",
    "cancel",
    "cancel_appointment",
    "complete",
    "create_appointment",
    "create_availability",
    "create_hold",
    "create_series",
    "create_service_appointment",
    "edit_series",
    "expire",
    "expire_due_holds",
    "hold",
    "mark_no_show",
    "prepare_booking",
    "reschedule_appointment",
    "retire_availability",
    "start",
    "view_agenda",
    "view_appointment_for_transition",
    "view_availability",
)
