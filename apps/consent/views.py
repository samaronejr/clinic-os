"""Patient decisions and staff read-only receipts; identifiers stay in POST bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.consent.forms import (
    AcceptanceForm,
    DisclosureForm,
    NoticeForm,
    ParticipantForm,
    TextForm,
)
from apps.consent.services import (
    acknowledge_participant,
    available_notices,
    available_texts,
    patient_disclosures,
    patient_receipts,
    patient_refusals,
    prepare_acceptance,
    publish_notice,
    publish_text,
    record_ai_disclosure,
    record_consent,
    record_refusal,
    revoke_consent,
    staff_receipts,
    staff_refusals,
)
from apps.identity.current_context import (
    CurrentActorError,
    require_permission,
)
from apps.identity.otp import privileged_totp_required
from apps.intake.access import PatientAccessDeniedError

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


def _private(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store, private"
    return response


@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def patient_consent(request: HttpRequest) -> HttpResponse:
    """Read, explicitly accept, inspect and revoke only this patient's versions."""
    context: dict[str, object] = {}
    status = 200
    try:
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "read":
                text, token = prepare_acceptance(
                    text_id=UUID(request.POST.get("text_id", ""))
                )
                context.update(
                    text=text,
                    form=AcceptanceForm(
                        initial={"offer": token, "purpose": text.purpose}
                    ),
                )
            elif action in {"accept", "refuse"}:
                form = AcceptanceForm(request.POST)
                if form.is_valid():
                    if (action == "accept") != form.cleaned_data["accepted"]:
                        form.add_error(
                            "accepted",
                            gettext(
                                "Select the checkbox to accept; "
                                "leave it clear to refuse."
                            ),
                        )
                        context["form"] = form
                        status = 400
                    elif action == "accept":
                        record_consent(**form.cleaned_data)
                        context["notice"] = gettext(
                            "Consent recorded. Your receipt remains available below."
                        )
                    else:
                        record_refusal(
                            offer=form.cleaned_data["offer"],
                            purpose=form.cleaned_data["purpose"],
                        )
                        context["notice"] = gettext(
                            "Refusal recorded. Your care remains available."
                        )
                else:
                    context["error"] = gettext(
                        "No consent recorded. Read the text and select the "
                        "checkbox only if you wish to accept."
                    )
                    status = 400
            elif action == "revoke":
                revoke_consent(
                    acceptance_id=UUID(request.POST.get("acceptance_id", ""))
                )
                context["notice"] = gettext(
                    "Consent revoked. Clinical history and the receipt were preserved."
                )
            else:
                return _private(render(request, "consent/denied.html", status=403))
        context["texts"] = available_texts()
        context["notices"] = available_notices()
        context["receipts"] = patient_receipts()
        context["refusals"] = patient_refusals()
        context["disclosures"] = patient_disclosures()
    except ValidationError as error:
        context["error"] = " ".join(error.messages)
        context["texts"] = available_texts()
        context["notices"] = available_notices()
        context["receipts"] = patient_receipts()
        context["refusals"] = patient_refusals()
        context["disclosures"] = patient_disclosures()
        status = 409
    except (PatientAccessDeniedError, ValueError):
        return _private(render(request, "consent/denied.html", status=403))
    return _private(render(request, "consent/patient.html", context, status=status))


def _continuation(clinic_id: UUID) -> str:
    return reverse("consent:staff", kwargs={"clinic_id": clinic_id})


def _staff_publish(
    request: HttpRequest, clinic_id: UUID, action: str, context: dict[str, object]
) -> int:
    """Publish a validated template, keeping every prior version immutable."""
    if action == "publish":
        form = TextForm(request.POST)
        context["form"] = form
        if not form.is_valid():
            return 400
        published = publish_text(clinic_id=clinic_id, **form.cleaned_data)
        context["notice"] = gettext(
            "Version %(version)s published. Previous texts were preserved."
        ) % {"version": published.version}
        context["form"] = TextForm()
        return 200
    notice_form = NoticeForm(request.POST, prefix="notice")
    context["notice_form"] = notice_form
    if not notice_form.is_valid():
        return 400
    publish_notice(clinic_id=clinic_id, **notice_form.cleaned_data)
    context["notice"] = gettext("Information notice published.")
    context["notice_form"] = NoticeForm(prefix="notice")
    return 200


def _staff_attestation(
    request: HttpRequest, clinic_id: UUID, action: str, context: dict[str, object]
) -> int:
    """Record an authorized clinician's encounter or non-patient disclosure."""
    if action == "disclosure":
        disclosure_form = DisclosureForm(request.POST, prefix="disclosure")
        context["disclosure_form"] = disclosure_form
        if not disclosure_form.is_valid():
            return 400
        disclosure = record_ai_disclosure(
            clinic_id=clinic_id, **disclosure_form.cleaned_data
        )
        context["disclosures"] = [disclosure]
        context["notice"] = gettext("AI-use disclosure recorded.")
        context["disclosure_form"] = DisclosureForm(prefix="disclosure")
        return 200
    participant_form = ParticipantForm(request.POST, prefix="ack")
    context["participant_form"] = participant_form
    if not participant_form.is_valid():
        return 400
    acknowledge_participant(clinic_id=clinic_id, **participant_form.cleaned_data)
    context["notice"] = gettext("Non-patient participant notice recorded.")
    context["participant_form"] = ParticipantForm(prefix="ack")
    return 200


@privileged_totp_required(_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def staff_consent(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Publish authorized overlays and inspect receipts, without an accept action."""
    context: dict[str, object] = {
        "form": TextForm(),
        "notice_form": NoticeForm(prefix="notice"),
        "disclosure_form": DisclosureForm(prefix="disclosure"),
        "participant_form": ParticipantForm(prefix="ack"),
        "clinic_id": clinic_id,
    }
    status = 200
    try:
        try:
            require_permission("demographics.read", clinic_id=clinic_id)
        except CurrentActorError:
            try:
                require_permission("configuration.clinic", clinic_id=clinic_id)
            except CurrentActorError:
                require_permission("configuration.organization", clinic_id=clinic_id)
        if request.method == "POST":
            action = request.POST.get("action", "")
            if action in {"publish", "publish_notice"}:
                status = _staff_publish(request, clinic_id, action, context)
            elif action == "receipts":
                enrollment_id = UUID(request.POST.get("enrollment_id", ""))
                context["receipts"] = staff_receipts(
                    clinic_id=clinic_id, enrollment_id=enrollment_id
                )
                context["refusals"] = staff_refusals(
                    clinic_id=clinic_id, enrollment_id=enrollment_id
                )
            elif action in {"disclosure", "acknowledge"}:
                status = _staff_attestation(request, clinic_id, action, context)
            else:
                return _private(render(request, "403.html", status=403))
    except ValidationError as error:
        context["error"] = " ".join(error.messages)
        status = 400
    except (PatientAccessDeniedError, CurrentActorError, ValueError):
        return _private(render(request, "403.html", status=403))
    return _private(render(request, "consent/staff.html", context, status=status))
