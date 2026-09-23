"""Native, body-only pt-BR patient scheduling screens."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.scheduling.patient_authority import require_patient_booking_scope
from apps.scheduling.patient_booking import (
    book_patient_slot,
    cancel_patient_appointment,
    patient_appointments,
    patient_slots,
    reschedule_patient_appointment,
)
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentAvailabilityError,
    AppointmentCancellationConflictError,
    AppointmentCreateInputError,
    AppointmentIdempotencyConflictError,
    AppointmentPractitionerError,
    AppointmentRescheduleInputError,
    AppointmentTerminalError,
    SlotConflict,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


@require_http_methods(["GET", "POST"])
def patient_booking_view(request: HttpRequest) -> HttpResponse:
    """Choose, book, inspect and transition only the invited clinic enrollment."""
    try:
        return _screen(request)
    except AppointmentAccessDeniedError:
        return render(
            request, "intake/patient_gate.html", {"state": "required"}, status=403
        )


def _submit(request: HttpRequest, key: str) -> UUID | None:
    """Reject scope fields; execute one closed operation or select a transition."""
    if set(request.POST) - {
        "csrfmiddlewaretoken",
        "action",
        "day",
        "slot",
        "idempotency_key",
        "appointment_id",
    }:
        raise AppointmentAccessDeniedError
    action = request.POST.get("action", "")
    if action == "book":
        book_patient_slot(token=request.POST.get("slot", ""), idempotency_key=UUID(key))
        return None
    if action not in {"cancel", "choose", "reschedule"}:
        raise AppointmentCreateInputError
    appointment_id = UUID(request.POST.get("appointment_id", ""))
    if action == "choose":
        return appointment_id
    if action == "cancel":
        cancel_patient_appointment(appointment_id)
    else:
        reschedule_patient_appointment(appointment_id, request.POST.get("slot", ""))
    return None


def _submission_result(request: HttpRequest, key: str) -> tuple[UUID | None, str]:
    try:
        return _submit(request, key), ""
    except (ValueError, AppointmentCreateInputError, AppointmentRescheduleInputError):
        return None, "Confira o horário selecionado e tente novamente."
    except (AppointmentAvailabilityError, AppointmentPractitionerError, SlotConflict):
        return None, "Este horário não está mais disponível. Escolha outro horário."
    except (
        AppointmentIdempotencyConflictError,
        AppointmentCancellationConflictError,
        AppointmentTerminalError,
    ):
        return (
            None,
            "A consulta mudou. Confira suas consultas antes de tentar novamente.",
        )


def _screen(request: HttpRequest) -> HttpResponse:
    scope = require_patient_booking_scope()
    zone = ZoneInfo(scope.timezone)
    error = ""
    appointment_id = None
    key = request.POST.get("idempotency_key", str(uuid4()))
    raw_day = (
        request.POST.get("day") if request.method == "POST" else request.GET.get("day")
    )
    try:
        day = (
            date.fromisoformat(raw_day)
            if raw_day
            else timezone.now().astimezone(zone).date()
        )
    except ValueError:
        day = timezone.now().astimezone(zone).date()
        error = "Confira a data e tente novamente."
    if request.method == "POST":
        appointment_id, error = _submission_result(request, key)
        if not error and appointment_id is None:
            response = redirect("scheduling:patient-booking")
            response.status_code = 303
            return response
    return render(
        request,
        "scheduling/patient_booking.html",
        {
            "scope": scope,
            "day": day.isoformat(),
            "error": error,
            "slots": patient_slots(day, appointment_id),
            "appointments": patient_appointments(),
            "appointment_id": appointment_id,
            "idempotency_key": key,
        },
        status=409 if error else 200,
    )
