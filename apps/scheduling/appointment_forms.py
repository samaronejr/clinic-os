"""Body-only appointment booking and transition forms.

Every field here is submitted in a request body. The clinic-local start and end
are explicit minute values rather than a generated slot, so the staff choose
any window the physician actually promised and Todo 10 stays the sole authority
on same-date, containment, and conflict rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from django import forms

from apps.scheduling.appointment_values import AppointmentLocalRange
from apps.scheduling.forms import (
    BLANK_CHOICE,
    bind_error_descriptions,
    describe_fields,
)
from apps.scheduling.models import Appointment
from apps.scheduling.timezones import LOCAL_MINUTE_PATTERN

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.http import QueryDict

LOCAL_MINUTE_MESSAGE: Final = "Enter a clinic-local minute as YYYY-MM-DDTHH:MM."
INVALID_BOOKING_MESSAGE: Final = (
    "Enter a future window that starts and ends on the same clinic-local date."
)
CONFLICTING_BOOKING_KEY_MESSAGE: Final = (
    "This booking was already submitted with different details."
)
SLOT_MESSAGE: Final = "That window is no longer free for this physician or patient."
WINDOW_MESSAGE: Final = (
    "Choose a window inside one of the physician's promised availability blocks."
)
BOOKING_PRACTITIONER_MESSAGE: Final = "Select an active physician for this clinic."
TERMINAL_MESSAGE: Final = "This appointment is cancelled and can no longer be moved."
CANCELLATION_CONFLICT_MESSAGE: Final = (
    "This appointment was already cancelled for a different reason."
)
INVALID_RESCHEDULE_MESSAGE: Final = (
    "Enter a future window that stays on one clinic-local date."
)
INVALID_CANCELLATION_MESSAGE: Final = "Choose one of the listed cancellation reasons."
AGENDA_INPUT_MESSAGE: Final = "Choose a day or week agenda with a valid calendar date."
BOOKING_DESCRIPTIONS: Final = {
    "practitioner": "booking-practitioner-help",
    "start_local": "booking-start-help",
    "end_local": "booking-end-help",
}
RESCHEDULE_DESCRIPTIONS: Final = {
    "start_local": "reschedule-start-help",
    "end_local": "reschedule-end-help",
}
CANCEL_DESCRIPTIONS: Final = {"reason": "cancel-reason-help"}


class LocalMinuteField(forms.CharField):
    """Accept exactly one clinic-local `YYYY-MM-DDTHH:MM` minute value."""

    widget = forms.DateTimeInput(attrs={"type": "datetime-local", "step": 60})

    def clean(self, value: object) -> str:
        """Return the submitted minute verbatim or reject its exact shape."""
        cleaned = str(super().clean(value))
        if LOCAL_MINUTE_PATTERN.fullmatch(cleaned) is None:
            raise forms.ValidationError(LOCAL_MINUTE_MESSAGE, code="invalid")
        return cleaned


class AppointmentPrepareForm(forms.Form):
    """Carry one body-only enrollment selection into booking preparation."""

    enrollment_id = forms.UUIDField(widget=forms.HiddenInput())

    def selected_enrollment(self) -> UUID:
        """Return the enrollment the request body selected."""
        return UUID(str(self.cleaned_data["enrollment_id"]))


class AppointmentCreateForm(forms.Form):
    """Collect one idempotent explicit clinic-local booking from the body."""

    enrollment_id = forms.UUIDField(widget=forms.HiddenInput())
    practitioner = forms.ChoiceField(label="Physician")
    start_local = LocalMinuteField(label="Starts (clinic local)")
    end_local = LocalMinuteField(label="Ends (clinic local)")
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput())

    def __init__(
        self,
        choices: Sequence[tuple[str, str]],
        data: QueryDict | None = None,
    ) -> None:
        """Bind the resolver-approved physician choices this clinic exposes."""
        super().__init__(data=data)
        field = self.fields["practitioner"]
        if isinstance(field, forms.ChoiceField):
            field.choices = [BLANK_CHOICE, *choices]
        describe_fields(self, BOOKING_DESCRIPTIONS)

    def full_clean(self) -> None:
        """Connect bound field errors to their rendered descriptions."""
        super().full_clean()
        bind_error_descriptions(self, BOOKING_DESCRIPTIONS)

    def selected_practitioner(self) -> UUID:
        """Return the resolver-approved practitioner the body selected."""
        return UUID(str(self.cleaned_data["practitioner"]))

    def selected_enrollment(self) -> UUID:
        """Return the enrollment this booking body carried forward."""
        return UUID(str(self.cleaned_data["enrollment_id"]))

    def local_range(self) -> AppointmentLocalRange:
        """Return the explicit clinic-local minute bounds as submitted."""
        return AppointmentLocalRange(
            start_local=str(self.cleaned_data["start_local"]),
            end_local=str(self.cleaned_data["end_local"]),
        )


class AppointmentRescheduleForm(forms.Form):
    """Collect one replacement clinic-local window and nothing else."""

    start_local = LocalMinuteField(label="New start (clinic local)")
    end_local = LocalMinuteField(label="New end (clinic local)")

    def __init__(self, data: QueryDict | None = None) -> None:
        """Describe both minute fields for assistive technology."""
        super().__init__(data=data)
        describe_fields(self, RESCHEDULE_DESCRIPTIONS)

    def full_clean(self) -> None:
        """Connect bound field errors to their rendered descriptions."""
        super().full_clean()
        bind_error_descriptions(self, RESCHEDULE_DESCRIPTIONS)

    def local_range(self) -> AppointmentLocalRange:
        """Return the explicit clinic-local minute bounds as submitted."""
        return AppointmentLocalRange(
            start_local=str(self.cleaned_data["start_local"]),
            end_local=str(self.cleaned_data["end_local"]),
        )


class AppointmentCancelForm(forms.Form):
    """Collect one closed cancellation reason with no free-text field."""

    reason = forms.ChoiceField(
        label="Cancellation reason",
        choices=Appointment.CancellationReason.choices,
    )

    def __init__(self, data: QueryDict | None = None) -> None:
        """Describe the closed reason vocabulary for assistive technology."""
        super().__init__(data=data)
        describe_fields(self, CANCEL_DESCRIPTIONS)

    def full_clean(self) -> None:
        """Connect bound field errors to their rendered descriptions."""
        super().full_clean()
        bind_error_descriptions(self, CANCEL_DESCRIPTIONS)

    def selected_reason(self) -> str:
        """Return the closed cancellation reason the body selected."""
        return str(self.cleaned_data["reason"])
