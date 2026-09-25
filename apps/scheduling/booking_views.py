"""Body-only legacy manager and RP-authorized service booking screen.

Both booking modes are POST. Neither the enrollment nor any demographic value
ever reaches a URL, so an unverified privileged challenge can only resume at the
blank clinic patient list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast
from uuid import UUID, uuid4

from django import forms
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.identity.otp import privileged_totp_required
from apps.scheduling.agenda_presenter import (
    DAY_VIEW,
    agenda_screen,
    agenda_url,
    booking_window_rows,
)
from apps.scheduling.appointment_creation import (
    ServiceBooking,
    create_service_appointment,
)
from apps.scheduling.appointment_forms import (
    BOOKED_MESSAGE,
    BOOKING_PRACTITIONER_MESSAGE,
    CONFLICTING_BOOKING_KEY_MESSAGE,
    INVALID_BOOKING_MESSAGE,
    SLOT_MESSAGE,
    WINDOW_MESSAGE,
    AppointmentCreateForm,
    AppointmentPrepareForm,
)
from apps.scheduling.availability_presenter import manager_choices
from apps.scheduling.forms import BLANK_CHOICE
from apps.scheduling.models import Resource, ServiceType
from apps.scheduling.resource_booking import (
    authorized_service_clinic,
    service_practitioners,
)
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentAvailabilityError,
    AppointmentCreateInputError,
    AppointmentIdempotencyConflictError,
    AppointmentPractitionerError,
    AvailabilityAccessDeniedError,
    SlotConflict,
    create_appointment,
    prepare_booking,
)

if TYPE_CHECKING:
    from django.contrib.messages.storage.base import BaseStorage
    from django.http import HttpRequest

    from apps.scheduling.services import BookingPreparation

SEE_OTHER: Final = 303
NO_CONTENT: Final = 204
BOOK_TEMPLATE: Final = "scheduling/appointment_book.html"
BOOK_PARTIAL: Final = "scheduling/partials/booking_panel.html"
PREPARE_MODE: Final = "prepare"
CREATE_MODE: Final = "create"
BOOKED_TAG: Final = "scheduling.appointment.booked"


def appointment_create_continuation(clinic_id: UUID) -> str:
    """Resume an unsafe booking challenge at the blank clinic patient list."""
    return reverse("intake:patient-list", args=(clinic_id,))


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _choices(preparation: BookingPreparation) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(entry.practitioner_id), entry.display_identifier)
        for entry in preparation.practitioners
    )


def _booking_context(
    clinic_id: UUID,
    preparation: BookingPreparation,
    form: AppointmentCreateForm,
) -> dict[str, object]:
    return {
        "book_url": reverse("scheduling:appointment-create", args=(clinic_id,)),
        "clinic_id": clinic_id,
        "form": form,
        "list_url": appointment_create_continuation(clinic_id),
        "patient_display_name": preparation.patient_display_name,
        "practitioners": preparation.practitioners,
        "timezone_key": agenda_screen(clinic_id).timezone_key,
        "window_count": sum(len(entry.windows) for entry in preparation.practitioners),
        "windows": booking_window_rows(preparation.practitioners),
    }


def _render_booking(
    request: HttpRequest,
    context: dict[str, object],
) -> HttpResponseBase:
    template = BOOK_PARTIAL if _is_htmx(request) else BOOK_TEMPLATE
    return render(request, template, context)


def _authorize_service_practitioner(request: HttpRequest, clinic_id: UUID) -> None:
    try:
        practitioner_id = forms.UUIDField(required=False).clean(
            request.POST.get("practitioner")
        )
    except forms.ValidationError as error:
        raise Http404 from error
    authorized_service_clinic(clinic_id, practitioner_id)


def _configure_resources(form: AppointmentCreateForm, clinic_id: UUID) -> None:
    form.configure_resources(
        [
            (str(row.pk), f"{row.name} ({row.duration_min} min)")
            for row in ServiceType.objects.filter(
                clinic_id=clinic_id, active=True
            ).order_by("name", "pk")
        ],
        [
            (str(row.pk), row.name)
            for row in Resource.objects.filter(
                clinic_id=clinic_id, active=True
            ).order_by("name", "pk")
        ],
    )

    practitioner = form.fields["practitioner"]
    if form.has_service_types and isinstance(practitioner, forms.ChoiceField):
        practitioner.label = _("Professional")
        practitioner.choices = [
            BLANK_CHOICE,
            *((str(pk), label) for pk, label in service_practitioners(clinic_id)),
        ]


def _preparation(request: HttpRequest, clinic_id: UUID) -> BookingPreparation:
    form = AppointmentPrepareForm(data=request.POST)
    if not form.is_valid():
        raise Http404
    service_id = form.cleaned_data["service_type_id"]
    resource_ids: tuple[UUID, ...] = ()
    if service_id is not None:
        _authorize_service_practitioner(request, clinic_id)
        try:
            resource_ids = tuple(
                UUID(pk) for pk in request.POST.getlist("resource_ids")
            )
        except ValueError as error:
            raise Http404 from error
    return prepare_booking(
        clinic_id=clinic_id,
        enrollment_id=form.selected_enrollment(),
        service_type_id=service_id,
        resource_ids=resource_ids,
    )


def _prepared(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    preparation = _preparation(request, clinic_id)
    blank = AppointmentCreateForm(_choices(preparation))
    _configure_resources(blank, clinic_id)
    blank.initial["enrollment_id"] = str(preparation.enrollment_id)
    if preparation.service_type_id is not None:
        blank.initial["service_type_id"] = str(preparation.service_type_id)
        blank.initial["resource_ids"] = [str(pk) for pk in preparation.resource_ids]
    blank.initial["idempotency_key"] = str(uuid4())
    return _render_booking(request, _booking_context(clinic_id, preparation, blank))


def _booked(form: AppointmentCreateForm, clinic_id: UUID) -> bool:
    service_id = form.cleaned_data["service_type_id"]
    if service_id is None and form.cleaned_data["resource_ids"]:
        form.add_error(None, INVALID_BOOKING_MESSAGE)
        return False
    try:
        if service_id is not None:
            create_service_appointment(
                clinic_id=clinic_id,
                enrollment_id=form.selected_enrollment(),
                practitioner_id=form.selected_practitioner(),
                booking=ServiceBooking(
                    form.local_range(),
                    service_id,
                    tuple(UUID(pk) for pk in form.cleaned_data["resource_ids"]),
                ),
                idempotency_key=form.cleaned_data["idempotency_key"],
            )
        else:
            create_appointment(
                clinic_id=clinic_id,
                enrollment_id=form.selected_enrollment(),
                practitioner_id=form.selected_practitioner(),
                local_range=form.local_range(),
                idempotency_key=form.cleaned_data["idempotency_key"],
            )
    except SchedulingRuleError as error:
        form.rule_code = error.code
        form.add_error(None, str(error.message))
    except AppointmentIdempotencyConflictError:
        form.add_error(None, CONFLICTING_BOOKING_KEY_MESSAGE)
    except SlotConflict:
        form.add_error(None, SLOT_MESSAGE)
    except AppointmentAvailabilityError:
        form.add_error(None, WINDOW_MESSAGE)
    except AppointmentPractitionerError:
        form.add_error(None, BOOKING_PRACTITIONER_MESSAGE)
    except AppointmentCreateInputError:
        form.add_error(None, INVALID_BOOKING_MESSAGE)
    else:
        return True
    return False


def _booked_response(
    request: HttpRequest,
    clinic_id: UUID,
    local_date: str,
) -> HttpResponseBase:
    target = agenda_url(clinic_id, DAY_VIEW, local_date, 1)
    # Completion feedback names the next step and carries no identifier. A
    # retry whose first reply was lost resolves to the same booking, so it
    # must not queue a second notice the agenda would show as two bookings.
    # MessageMiddleware is installed, so the request carries real storage.
    pending = cast("BaseStorage", messages.get_messages(request))
    already_noticed = any(BOOKED_TAG in message.tags for message in pending)
    pending.used = False
    if not already_noticed:
        messages.success(request, str(BOOKED_MESSAGE), extra_tags=BOOKED_TAG)
    if _is_htmx(request):
        response: HttpResponseBase = HttpResponse(status=NO_CONTENT)
        response.headers["HX-Redirect"] = target
        return response
    accepted = redirect(target, permanent=False)
    accepted.status_code = SEE_OTHER
    return accepted


def _created(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    if request.POST.get("service_type_id"):
        _authorize_service_practitioner(request, clinic_id)
        choices = tuple(
            (str(pk), label) for pk, label in service_practitioners(clinic_id)
        )
    else:
        choices = manager_choices(clinic_id)
    form = AppointmentCreateForm(choices, request.POST)
    _configure_resources(form, clinic_id)
    if form.is_valid() and _booked(form, clinic_id):
        return _booked_response(request, clinic_id, form.local_range().start_local[:10])
    preparation = _preparation(request, clinic_id)
    return _render_booking(request, _booking_context(clinic_id, preparation, form))


@privileged_totp_required(appointment_create_continuation)
@require_http_methods(["POST"])
def appointment_create_view(
    request: HttpRequest,
    clinic_id: UUID,
) -> HttpResponseBase:
    """Prepare one booking window set or create one explicit appointment."""
    mode = request.POST.get("mode", "")
    if mode not in {PREPARE_MODE, CREATE_MODE}:
        raise Http404
    try:
        if mode == PREPARE_MODE:
            return _prepared(request, clinic_id)
        return _created(request, clinic_id)
    except (AppointmentAccessDeniedError, AvailabilityAccessDeniedError) as error:
        raise Http404 from error
    except SchedulingRuleError as error:
        return HttpResponse(str(error.message), status=409)
