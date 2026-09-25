"""No-store, native Settings administration for scheduling definitions."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from django import forms as django_forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.dates import WEEKDAYS
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.identity.otp import privileged_totp_required
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.models import (
    Absence,
    AvailabilityTemplate,
    Holiday,
    Resource,
    ServiceType,
)
from apps.scheduling.resource_booking import service_practitioners
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.resource_forms import (
    ClosureForm,
    GenerateForm,
    ResourceForm,
    RetireForm,
    ServiceForm,
    SubjectForm,
    TemplateForm,
)
from apps.scheduling.resource_services import (
    ClosureInput,
    ResourceInput,
    ServiceInput,
    TemplateInput,
    configuration_clinic,
    create_closure,
    create_resource,
    create_service_type,
    create_template,
    generate_availability,
    retire_definition,
)
from apps.scheduling.timezones import LocalTimeValueError

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


def _continuation(clinic_id: UUID) -> str:
    return reverse("scheduling:resource-settings", args=(clinic_id,))


def _private(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store, private"
    return response


def _submit(action: str, clinic_id: UUID, values: dict[str, Any]) -> None:
    for field in ("practitioner_id", "resource_id"):
        if field in values:
            values[field] = UUID(values[field]) if values[field] else None
    if action == "resource":
        create_resource(clinic_id=clinic_id, content=ResourceInput(**values))
    elif action == "service":
        create_service_type(clinic_id=clinic_id, content=ServiceInput(**values))
    elif action == "template":
        create_template(clinic_id=clinic_id, content=TemplateInput(**values))
    elif action == "closure":
        create_closure(clinic_id=clinic_id, content=ClosureInput(**values))
    elif action == "generate":
        generate_availability(clinic_id=clinic_id, **values)
    else:
        retire_definition(
            clinic_id=clinic_id,
            kind=cast(
                "Literal['resource', 'service', 'template', 'holiday', 'absence']",
                values["kind"],
            ),
            record_id=values["record_id"],
        )


def _template_catalog(clinic_id: UUID) -> list[dict[str, object]]:
    professionals: dict[UUID | None, str] = dict(service_practitioners(clinic_id))
    result = []
    for row in (
        AvailabilityTemplate.objects.filter(clinic_id=clinic_id)
        .select_related("resource")
        .order_by("valid_from", "pk")
    ):
        subject = (
            row.resource.name
            if row.resource is not None
            else professionals.get(row.practitioner_id, _("Unavailable"))
        )
        weekdays = ", ".join(str(WEEKDAYS[day]) for day in row.weekdays)
        label = (
            f"{subject}: {weekdays} {row.start_local:%H:%M}-{row.end_local:%H:%M}; "
            f"{date_format(row.valid_from, 'SHORT_DATE_FORMAT')} - "
            f"{date_format(row.valid_to, 'SHORT_DATE_FORMAT')}"
        )
        result.append(
            {
                "pk": row.pk,
                "active": row.active,
                "subject": subject,
                "weekdays": weekdays,
                "label": label,
                "valid_from": row.valid_from,
                "valid_to": row.valid_to,
                "start_local": row.start_local,
                "end_local": row.end_local,
                "timezone": row.timezone,
            }
        )
    return result


def _forms(clinic_id: UUID, request: HttpRequest) -> dict[str, django_forms.Form]:
    subjects = {
        "practitioners": [
            (str(pk), label) for pk, label in service_practitioners(clinic_id)
        ],
        "resources": [
            (str(row.pk), row.name)
            for row in Resource.objects.filter(
                clinic_id=clinic_id, active=True
            ).order_by("name", "pk")
        ],
    }
    constructors: dict[str, type[django_forms.Form]] = {
        "resource": ResourceForm,
        "service": ServiceForm,
        "template": TemplateForm,
        "closure": ClosureForm,
        "generate": GenerateForm,
        "retire": RetireForm,
    }
    result: dict[str, django_forms.Form] = {}
    for action, form_class in constructors.items():
        data = (
            request.POST
            if request.method == "POST" and request.POST.get("action") == action
            else None
        )
        if issubclass(form_class, SubjectForm):
            result[action] = form_class(
                data=data,
                prefix=action,
                practitioners=subjects["practitioners"],
                resources=subjects["resources"],
            )
        else:
            result[action] = form_class(data=data, prefix=action)
    widget = result["generate"].fields["template_id"].widget
    if isinstance(widget, django_forms.Select):
        widget.choices = [
            (str(row["pk"]), str(row["label"]))
            for row in _template_catalog(clinic_id)
            if row["active"]
        ]
    return result


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
                _submit(action, clinic_id, form.cleaned_data.copy())
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
