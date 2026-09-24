"""Patient decisions and staff read-only receipts; identifiers stay in POST bodies."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.consent.forms import AcceptanceForm, TextForm
from apps.consent.services import (
    STAFF_ROLES,
    available_texts,
    patient_receipts,
    prepare_acceptance,
    publish_text,
    record_consent,
    revoke_consent,
    staff_receipts,
)
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
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
            elif action == "accept":
                form = AcceptanceForm(request.POST)
                if form.is_valid():
                    record_consent(**form.cleaned_data)
                    context["notice"] = (
                        "Consentimento registrado. Guarde seu comprovante abaixo."
                    )
                else:
                    context["error"] = (
                        "Nenhum consentimento registrado. Leia o texto e marque "
                        "a opção somente se desejar aceitar."
                    )
                    status = 400
            elif action == "revoke":
                revoke_consent(
                    acceptance_id=UUID(request.POST.get("acceptance_id", ""))
                )
                context["notice"] = (
                    "Consentimento revogado. O histórico clínico e o comprovante "
                    "foram preservados."
                )
            else:
                return _private(render(request, "consent/denied.html", status=403))
        context["texts"] = available_texts()
        context["receipts"] = patient_receipts()
    except ValidationError as error:
        context["error"] = " ".join(error.messages)
        context["texts"] = available_texts()
        context["receipts"] = patient_receipts()
        status = 409
    except (PatientAccessDeniedError, ValueError):
        return _private(render(request, "consent/denied.html", status=403))
    return _private(render(request, "consent/patient.html", context, status=status))


def _continuation(clinic_id: UUID) -> str:
    return reverse("consent:staff", kwargs={"clinic_id": clinic_id})


@privileged_totp_required(_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def staff_consent(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Publish authorized overlays and inspect receipts, without an accept action."""
    context: dict[str, object] = {"form": TextForm(), "clinic_id": clinic_id}
    status = 200
    try:
        require_current_actor_clinic_roles(clinic_id, STAFF_ROLES)
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "publish":
                form = TextForm(request.POST)
                context["form"] = form
                if form.is_valid():
                    published = publish_text(clinic_id=clinic_id, **form.cleaned_data)
                    context["notice"] = (
                        f"Versão {published.version} publicada. "
                        "Os textos anteriores foram preservados."
                    )
                    context["form"] = TextForm()
                else:
                    status = 400
            elif action == "receipts":
                context["receipts"] = staff_receipts(
                    clinic_id=clinic_id,
                    enrollment_id=UUID(request.POST.get("enrollment_id", "")),
                )
            else:
                return _private(render(request, "403.html", status=403))
    except ValidationError as error:
        context["error"] = " ".join(error.messages)
        status = 400
    except (PatientAccessDeniedError, CurrentActorError, ValueError):
        return _private(render(request, "403.html", status=403))
    return _private(render(request, "consent/staff.html", context, status=status))
