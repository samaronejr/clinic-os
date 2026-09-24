"""Native pt-BR staff queue and enrollment-only patient offer screens."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import UUID

from django import forms
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.identity.current_context import list_active_clinic_physicians
from apps.identity.otp import privileged_totp_required
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.access import (
    AppointmentAccessDeniedError,
    authorized_appointment_manager_clinic,
)
from apps.scheduling.patient_authority import require_patient_booking_scope
from apps.scheduling.services import AppointmentPractitionerError
from apps.scheduling.waitlist import (
    WaitlistInputError,
    add_waitlist_entry,
    issue_waitlist_offer,
    patient_waitlist_offers,
    respond_to_offer,
    staff_waitlist,
    waitlist_notice_channels,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, HttpResponseBase

    from apps.scheduling.models import WaitlistOffer


class OpeningForm(forms.Form):
    """Clinic catalog choices, never a caller-selected practitioner scope."""

    practitioner = forms.ChoiceField(label="Profissional")
    start_local = forms.CharField(
        label="Início", widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    end_local = forms.CharField(
        label="Fim", widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )


class RequestForm(OpeningForm):
    """A requested practitioner and window attached to an existing enrollment."""

    enrollment = forms.ChoiceField(label="Paciente")


def waitlist_continuation(clinic_id: UUID) -> str:
    """Resume privileged staff work without putting patient state in a URL."""
    return reverse("scheduling:waitlist", args=(clinic_id,))


@privileged_totp_required(waitlist_continuation)
@require_http_methods(["GET", "POST"])
def waitlist_view(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Manage the FIFO queue; never prioritize using clinical information."""
    try:
        return _staff_screen(request, clinic_id)
    except AppointmentAccessDeniedError as error:
        raise Http404 from error


def _staff_screen(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    clinic = authorized_appointment_manager_clinic(clinic_id)
    action = request.POST.get("action", "")
    entry_form = RequestForm(request.POST if action == "add" else None, prefix="entry")
    offer_form = OpeningForm(
        request.POST if action == "offer" else None, prefix="offer"
    )
    choices = [
        (str(row.user_id), row.display_label)
        for row in list_active_clinic_physicians(clinic_id)
    ]
    cast("forms.ChoiceField", entry_form.fields["practitioner"]).choices = choices
    cast("forms.ChoiceField", offer_form.fields["practitioner"]).choices = choices
    # Names are tenant envelopes: ordering happens after decryption.
    enrollments = sorted(
        PatientClinicEnrollment.objects.filter(clinic_id=clinic_id).select_related(
            "patient"
        ),
        key=lambda row: (row.patient.full_name.lower(), str(row.pk)),
    )
    cast("forms.ChoiceField", entry_form.fields["enrollment"]).choices = [
        (str(row.pk), row.patient.full_name) for row in enrollments
    ]
    error = ""
    if request.method == "POST":
        form = entry_form if action == "add" else offer_form
        if action not in {"add", "offer"} or not form.is_valid():
            error = "Confira os campos e tente novamente."
        else:
            try:
                matched = _staff_submit(clinic_id, action, form.cleaned_data)
            except (WaitlistInputError, AppointmentPractitionerError):
                error = (
                    "Confira o período solicitado. Para oferecer, "
                    "escolha um horário futuro e disponível."
                )
            else:
                messages.success(
                    request,
                    "Fila atualizada. Confira as solicitações e ofertas abaixo."
                    if matched
                    else "Nenhuma solicitação elegível para este horário.",
                )
                response = redirect("scheduling:waitlist", clinic_id=clinic_id)
                response.status_code = 303
                return response
    entries = staff_waitlist(clinic_id)
    rows = [
        {
            "entry": entry,
            "offers": [
                {"offer": offer, "channels": waitlist_notice_channels(offer.pk)}
                for offer in entry.offers.all()
            ],
        }
        for entry in entries
    ]
    return render(
        request,
        "scheduling/waitlist.html",
        {
            "clinic": clinic,
            "rows": rows,
            "entry_form": entry_form,
            "offer_form": offer_form,
            "error": error,
        },
        status=409 if error else 200,
    )


def _staff_submit(clinic_id: UUID, action: str, data: dict[str, object]) -> bool:
    practitioner = UUID(str(data["practitioner"]))
    start, end = str(data["start_local"]), str(data["end_local"])
    if action == "add":
        add_waitlist_entry(
            clinic_id=clinic_id,
            enrollment_id=UUID(str(data["enrollment"])),
            practitioner_id=practitioner,
            start_local=start,
            end_local=end,
        )
        return True
    return (
        issue_waitlist_offer(
            clinic_id=clinic_id,
            practitioner_id=practitioner,
            start_local=start,
            end_local=end,
        )
        is not None
    )


def _patient_submit(request: HttpRequest) -> WaitlistOffer:
    if set(request.POST) - {"csrfmiddlewaretoken", "action", "offer_id"}:
        raise AppointmentAccessDeniedError
    action = request.POST.get("action")
    if action not in {"accept", "decline"}:
        raise WaitlistInputError
    return respond_to_offer(
        UUID(request.POST.get("offer_id", "")), accept=action == "accept"
    )


@require_http_methods(["GET", "POST"])
def patient_offers_view(request: HttpRequest) -> HttpResponse:
    """Only the invited enrollment can inspect, accept or decline its offers."""
    try:
        scope = require_patient_booking_scope()
        error = ""
        if request.method == "POST":
            action = request.POST.get("action")
            try:
                offer = _patient_submit(request)
            except ValueError:
                error = "Confira a oferta e tente novamente."
            else:
                if offer.state in {"expired", "unavailable"} or (
                    action == "accept" and offer.state == "declined"
                ):
                    error = (
                        "Esta oferta não está mais disponível. "
                        "Consulte outros horários ou fale com a clínica."
                    )
                else:
                    response = redirect("scheduling:patient-offers")
                    response.status_code = 303
                    return response
        return render(
            request,
            "scheduling/patient_offers.html",
            {
                "scope": scope,
                "offers": patient_waitlist_offers(),
                "error": error,
            },
            status=409 if error else 200,
        )
    except AppointmentAccessDeniedError:
        return render(
            request, "intake/patient_gate.html", {"state": "required"}, status=403
        )
