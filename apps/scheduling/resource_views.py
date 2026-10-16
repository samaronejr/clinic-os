"""No-store native Settings HTTP transactions and refusal presentation."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from django import forms as django_forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.identity.otp import privileged_totp_required
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.booking_entry import service_booking_entry
from apps.scheduling.models import Absence, Holiday, Resource, ServiceType
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.resource_form_actions import publish_definition
from apps.scheduling.resource_services import configuration_clinic
from apps.scheduling.resource_settings_forms import _forms, _template_catalog
from apps.scheduling.timezones import LocalTimeValueError

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest, HttpResponse


def _continuation(clinic_id: UUID) -> str:
    return reverse("scheduling:resource-settings", args=(clinic_id,))


def _private(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store, private"
    return response


@privileged_totp_required(_continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def resource_settings(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Configure only authorized clinic data; every selector stays in POST."""
    try:
        clinic = configuration_clinic(clinic_id)
        forms = _forms(clinic_id, request)
    except AppointmentAccessDeniedError:
        return _private(render(request, "403.html", status=403))
    status = 200
    code = ""
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action not in forms:
            return _private(render(request, "400.html", status=400))
        form = forms[action]
        allowed = {form.add_prefix(name) for name in form.fields} | {
            "csrfmiddlewaretoken",
            "action",
        }
        repeated = {
            form.add_prefix(name)
            for name, field in form.fields.items()
            if isinstance(field, django_forms.MultipleChoiceField)
        }
        if (
            set(request.POST) - allowed
            or request.FILES
            or any(
                len(request.POST.getlist(key)) != 1
                for key in request.POST
                if key not in repeated
            )
        ):
            return _private(render(request, "400.html", status=400))
        if form.is_valid():
            try:
                publish_definition(clinic_id=clinic_id, form=form)
            except AppointmentAccessDeniedError:
                return _private(render(request, "403.html", status=403))
            except SchedulingRuleError as error:
                code = error.code
                form.add_error(None, str(error.message))
                status = HTTPStatus.CONFLICT
            except (ValidationError, LocalTimeValueError) as error:
                form.add_error(
                    None,
                    error
                    if isinstance(error, ValidationError)
                    else _("Choose an unambiguous clinic-local time."),
                )
            else:
                messages.success(request, _("Scheduling settings saved."))
                return _private(redirect(_continuation(clinic_id)))
        status = status if status == HTTPStatus.CONFLICT else HTTPStatus.BAD_REQUEST
    return _private(
        render(
            request,
            "scheduling/resource_settings.html",
            {
                "clinic": clinic,
                "booking_form": service_booking_entry(clinic_id),
                "book_url": reverse("scheduling:appointment-create", args=(clinic_id,)),
                "forms": forms,
                "error_code": code,
                "panels": [
                    (key, label, forms[key])
                    for key, label in (
                        ("resource", _("Resources")),
                        ("service", _("Service types")),
                        ("template", _("Availability templates")),
                        ("closure", _("Holidays and absences")),
                        ("generate", _("Generate availability")),
                    )
                ],
                "catalogs": [
                    (
                        "resource",
                        _("Resources"),
                        Resource.objects.filter(clinic_id=clinic_id).order_by(
                            "name", "pk"
                        ),
                    ),
                    (
                        "service",
                        _("Service types"),
                        ServiceType.objects.filter(clinic_id=clinic_id).order_by(
                            "name", "pk"
                        ),
                    ),
                    (
                        "template",
                        _("Availability templates"),
                        _template_catalog(clinic_id),
                    ),
                    (
                        "holiday",
                        _("Holidays"),
                        Holiday.objects.filter(clinic_id=clinic_id).order_by(
                            "start_at", "pk"
                        ),
                    ),
                    (
                        "absence",
                        _("Absences"),
                        Absence.objects.filter(clinic_id=clinic_id).order_by(
                            "start_at", "pk"
                        ),
                    ),
                ],
            },
            status=status,
        )
    )
