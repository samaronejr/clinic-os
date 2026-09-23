"""Clinic-scoped settings with canonical roles, TOTP, CSRF and native forms."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.consent.forms import TextForm
from apps.consent.models import ConsentText
from apps.consent.services import publish_text
from apps.ehr.attachments import AttachmentInput
from apps.ehr.models import SpecialtyTemplate
from apps.ehr.services import SOAP_FIELDS, publish_template
from apps.identity.clinic_configuration import (
    CONFIGURATION_ROLES,
    MAX_LOGO_BYTES,
    ConfigurationContent,
    latest_configuration,
    publish_configuration,
)
from apps.identity.clinic_settings_forms import (
    ClinicSettingsForm,
    QuestionnaireForm,
    SpecialtyOverlayForm,
)
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.identity.otp import privileged_totp_required
from apps.intake.models import QuestionnaireTemplate
from apps.intake.services import publish_template as publish_questionnaire

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest


def _continuation(clinic_id: UUID) -> str:
    return reverse("identity:clinic-settings", args=(clinic_id,))


def _private(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store, private"
    return response


def _publish_overlay(action: str, clinic_id: UUID, values: dict[str, Any]) -> None:
    """Publish the next version of one versioned overlay from valid form data."""
    if action == "specialty":
        prompts = {field: values.pop(field) for field in SOAP_FIELDS}
        publish_template(clinic_id=clinic_id, prompts=prompts, **values)
    elif action == "questionnaire":
        publish_questionnaire(
            clinic_id=clinic_id,
            key=values["key"],
            title=values["title"],
            questions=values["questions"],
        )
    else:
        publish_text(clinic_id=clinic_id, **values)


@privileged_totp_required(_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def clinic_settings(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Publish data-only future versions; no timezone or credential write path."""
    try:
        require_current_actor_clinic_roles(clinic_id, CONFIGURATION_ROLES)
    except CurrentActorError:
        return _private(render(request, "403.html", status=403))
    clinic = Clinic.objects.get(pk=clinic_id)
    current = latest_configuration(clinic_id)
    initial = {
        "expected_version": current.version if current else 0,
        "display_name": current.display_name if current else clinic.name,
        "contact_email": current.contact_email if current else "",
        "contact_phone": current.contact_phone if current else "",
        "brand_token": current.brand_token if current else "navy",
        "reminder_hours": current.reminder_hours if current else 24,
    }
    forms = {
        "settings": ClinicSettingsForm(initial=initial),
        "specialty": SpecialtyOverlayForm(),
        "consent": TextForm(),
        "questionnaire": QuestionnaireForm(),
    }
    status = 200
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action not in forms:
            return _private(render(request, "400.html", status=400))
        form = type(forms[action])(request.POST, request.FILES)
        forms[action] = form
        allowed = {*form.fields, "action", "csrfmiddlewaretoken"}
        allowed_files = {"logo"} if action == "settings" else set()
        if (
            (set(request.POST) | set(request.FILES)) - allowed
            or set(request.FILES) - allowed_files
            or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
            or any(len(request.FILES.getlist(key)) != 1 for key in request.FILES)
        ):
            return _private(render(request, "400.html", status=400))
        if form.is_valid():
            values = form.cleaned_data.copy()
            try:
                if action == "settings":
                    upload = values.pop("logo")
                    logo = (
                        AttachmentInput(
                            file_name=upload.name,
                            declared_type=upload.content_type,
                            data=upload.read(MAX_LOGO_BYTES + 1),
                        )
                        if upload
                        else None
                    )
                    expected_version = values.pop("expected_version")
                    remove_logo = values.pop("remove_logo")
                    publish_configuration(
                        clinic_id=clinic_id,
                        logo=logo,
                        expected_version=expected_version,
                        remove_logo=remove_logo,
                        content=ConfigurationContent(**values),
                    )
                else:
                    _publish_overlay(action, clinic_id, values)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                return _private(redirect(_continuation(clinic_id)))
        status = 400
    return _private(
        render(
            request,
            "identity/clinic_settings.html",
            {
                "clinic": clinic,
                "configuration": current,
                "settings_form": forms["settings"],
                "specialty_form": forms["specialty"],
                "consent_form": forms["consent"],
                "questionnaire_form": forms["questionnaire"],
                "questionnaires": QuestionnaireTemplate.objects.filter(
                    clinic_id=clinic_id
                )
                .order_by("key", "-version")
                .distinct("key"),
                "templates": SpecialtyTemplate.objects.filter(clinic_id=clinic_id)
                .order_by("key", "-version")
                .distinct("key"),
                "texts": ConsentText.objects.filter(clinic_id=clinic_id)
                .order_by("purpose", "-version")
                .distinct("purpose"),
            },
            status=status,
        )
    )


@privileged_totp_required(_continuation)
@require_http_methods(["GET"])
def clinic_logo(_request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Serve only a normalized raster to an authorized clinic staff member."""
    try:
        require_current_actor_clinic_roles(clinic_id, tuple(UserClinicRole.Role))
    except CurrentActorError:
        return _private(HttpResponse(status=403))
    configuration = latest_configuration(clinic_id)
    if configuration is None or not configuration.logo_png:
        return _private(HttpResponse(status=404))
    response = HttpResponse(bytes(configuration.logo_png), content_type="image/png")
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Disposition"] = 'inline; filename="clinic-logo.png"'
    return _private(response)
