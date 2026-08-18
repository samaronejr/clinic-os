"""Same-object reschedule and cancellation screens for one opaque appointment.

Neither screen can change the patient or the practitioner, and cancellation
offers only the closed reason vocabulary. Every unsafe challenge resumes at the
same object's own GET, so no submitted window or reason is ever replayed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.http import Http404, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.identity.otp import privileged_totp_required
from apps.scheduling.agenda_presenter import agenda_screen
from apps.scheduling.appointment_forms import (
    BOOKING_PRACTITIONER_MESSAGE,
    CANCELLATION_CONFLICT_MESSAGE,
    INVALID_CANCELLATION_MESSAGE,
    INVALID_RESCHEDULE_MESSAGE,
    SLOT_MESSAGE,
    TERMINAL_MESSAGE,
    WINDOW_MESSAGE,
    AppointmentCancelForm,
    AppointmentRescheduleForm,
)
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentAvailabilityError,
    AppointmentCancellationConflictError,
    AppointmentCancellationInputError,
    AppointmentPractitionerError,
    AppointmentRescheduleInputError,
    AppointmentTerminalError,
    AvailabilityAccessDeniedError,
    SlotConflict,
    cancel_appointment,
    reschedule_appointment,
    view_appointment_for_transition,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django import forms
    from django.http import HttpRequest

    from apps.scheduling.services import AppointmentTransitionView

SEE_OTHER: Final = 303
NO_CONTENT: Final = 204
TRANSITION_TEMPLATE: Final = "scheduling/appointment_transition.html"
TRANSITION_PARTIAL: Final = "scheduling/partials/transition_panel.html"
RESCHEDULE_KIND: Final = "reschedule"
CANCEL_KIND: Final = "cancel"


def reschedule_continuation(appointment_id: UUID) -> str:
    """Resume an unsafe reschedule challenge at this appointment's own GET."""
    return reverse("scheduling:appointment-reschedule", args=(appointment_id,))


def cancel_continuation(appointment_id: UUID) -> str:
    """Resume an unsafe cancellation challenge at this appointment's own GET."""
    return reverse("scheduling:appointment-cancel", args=(appointment_id,))


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _context(
    current: AppointmentTransitionView,
    form: forms.Form,
    kind: str,
) -> dict[str, object]:
    return {
        "action_url": (
            reschedule_continuation(current.appointment_id)
            if kind == RESCHEDULE_KIND
            else cancel_continuation(current.appointment_id)
        ),
        "agenda_url": reverse("scheduling:agenda", args=(current.clinic_id,)),
        "appointment": current,
        "form": form,
        "is_scheduled": current.status == Appointment.Status.SCHEDULED,
        "kind": kind,
        "timezone_key": agenda_screen(current.clinic_id).timezone_key,
    }


def _render(
    request: HttpRequest,
    current: AppointmentTransitionView,
    form: forms.Form,
    kind: str,
) -> HttpResponseBase:
    template = TRANSITION_PARTIAL if _is_htmx(request) else TRANSITION_TEMPLATE
    return render(request, template, _context(current, form, kind))


def _completed(request: HttpRequest, target: str) -> HttpResponseBase:
    if _is_htmx(request):
        response: HttpResponseBase = HttpResponse(status=NO_CONTENT)
        response.headers["HX-Redirect"] = target
        return response
    accepted = redirect(target, permanent=False)
    accepted.status_code = SEE_OTHER
    return accepted


def _rescheduled(form: AppointmentRescheduleForm, appointment_id: UUID) -> bool:
    try:
        reschedule_appointment(
            appointment_id=appointment_id,
            local_range=form.local_range(),
        )
    except AppointmentTerminalError:
        form.add_error(None, TERMINAL_MESSAGE)
    except SlotConflict:
        form.add_error(None, SLOT_MESSAGE)
    except AppointmentAvailabilityError:
        form.add_error(None, WINDOW_MESSAGE)
    except AppointmentPractitionerError:
        form.add_error(None, BOOKING_PRACTITIONER_MESSAGE)
    except AppointmentRescheduleInputError:
        form.add_error(None, INVALID_RESCHEDULE_MESSAGE)
    else:
        return True
    return False


def _cancelled(form: AppointmentCancelForm, appointment_id: UUID) -> bool:
    try:
        cancel_appointment(
            appointment_id=appointment_id,
            reason=form.selected_reason(),
        )
    except AppointmentCancellationConflictError:
        form.add_error(None, CANCELLATION_CONFLICT_MESSAGE)
    except AppointmentCancellationInputError:
        form.add_error(None, INVALID_CANCELLATION_MESSAGE)
    else:
        return True
    return False


@privileged_totp_required(reschedule_continuation)
@require_http_methods(["GET", "POST"])
def appointment_reschedule_view(
    request: HttpRequest,
    appointment_id: UUID,
) -> HttpResponseBase:
    """Show one audited appointment and move it inside its own window."""
    try:
        if request.method == "POST":
            form = AppointmentRescheduleForm(request.POST)
            if form.is_valid() and _rescheduled(form, appointment_id):
                return _completed(request, reschedule_continuation(appointment_id))
        else:
            form = AppointmentRescheduleForm()
        current = view_appointment_for_transition(appointment_id=appointment_id)
        return _render(request, current, form, RESCHEDULE_KIND)
    except (AppointmentAccessDeniedError, AvailabilityAccessDeniedError) as error:
        raise Http404 from error


@privileged_totp_required(cancel_continuation)
@require_http_methods(["GET", "POST"])
def appointment_cancel_view(
    request: HttpRequest,
    appointment_id: UUID,
) -> HttpResponseBase:
    """Show one audited appointment and cancel it with a closed reason."""
    try:
        if request.method == "POST":
            form = AppointmentCancelForm(request.POST)
            if form.is_valid() and _cancelled(form, appointment_id):
                return _completed(request, cancel_continuation(appointment_id))
        else:
            form = AppointmentCancelForm()
        current = view_appointment_for_transition(appointment_id=appointment_id)
        return _render(request, current, form, CANCEL_KIND)
    except (AppointmentAccessDeniedError, AvailabilityAccessDeniedError) as error:
        raise Http404 from error
