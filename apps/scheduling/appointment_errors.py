"""Stable non-identifying appointment creation errors."""


class AppointmentCreateInputError(ValueError):
    """Reject malformed appointment input without reflecting it."""

    def __init__(self) -> None:
        """Expose one stable non-identifying input message."""
        super().__init__("appointment create input is invalid")


class AppointmentPractitionerError(Exception):
    """Reject a target without an active exact physician assignment."""

    def __init__(self) -> None:
        """Expose one stable non-identifying practitioner message."""
        super().__init__("appointment practitioner unavailable")


class AppointmentAvailabilityError(Exception):
    """Reject a booking without one active containing availability block."""

    def __init__(self) -> None:
        """Expose one stable non-identifying availability message."""
        super().__init__("appointment availability unavailable")


class AppointmentIdempotencyConflictError(Exception):
    """Reject reuse of a booking key for different canonical input."""

    def __init__(self) -> None:
        """Expose one stable non-identifying idempotency message."""
        super().__init__("appointment idempotency conflict")


class SlotConflictError(Exception):
    """Hide whether a practitioner or organization patient occupied the slot."""

    def __init__(self) -> None:
        """Expose one stable non-identifying slot message."""
        super().__init__("appointment slot unavailable")


SlotConflict = SlotConflictError


class AppointmentCancellationInputError(ValueError):
    """Reject values outside the fixed cancellation-reason vocabulary."""

    def __init__(self) -> None:
        """Expose one stable non-identifying cancellation message."""
        super().__init__("appointment cancellation input is invalid")


class AppointmentCancellationConflictError(Exception):
    """Reject a different reason for an already-cancelled appointment."""

    def __init__(self) -> None:
        """Expose one stable non-identifying cancellation conflict."""
        super().__init__("appointment cancellation conflicts with terminal state")


class AppointmentTerminalError(Exception):
    """Reject attempts to reopen or reschedule a terminal appointment."""

    def __init__(self) -> None:
        """Expose one stable non-identifying terminal-state message."""
        super().__init__("appointment is terminal")


class AppointmentRescheduleInputError(ValueError):
    """Reject malformed or invalid clinic-local reschedule bounds."""

    def __init__(self) -> None:
        """Expose one stable non-identifying reschedule message."""
        super().__init__("appointment reschedule input is invalid")
