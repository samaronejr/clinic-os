"""Scoped form binding and typed definition catalog presentation."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from django import forms as django_forms
from django.utils.dates import WEEKDAYS
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from apps.scheduling.models import AvailabilityTemplate, Resource
from apps.scheduling.resource_booking import service_practitioners
from apps.scheduling.resource_forms import (
    ClosureForm,
    GenerateForm,
    ResourceForm,
    RetireForm,
    ServiceForm,
    TemplateForm,
)

if TYPE_CHECKING:
    from datetime import date, time
    from uuid import UUID

    from django.http import HttpRequest

    from apps.scheduling.resource_forms import DefinitionForm


class TemplateCatalogRow(TypedDict):
    """The complete presentation record of one immutable template."""

    pk: UUID
    active: bool
    subject: str
    weekdays: str
    label: str
    valid_from: date
    valid_to: date
    start_local: time
    end_local: time
    timezone: str


def _template_catalog(clinic_id: UUID) -> list[TemplateCatalogRow]:
    professionals: dict[UUID | None, str] = dict(service_practitioners(clinic_id))
    result: list[TemplateCatalogRow] = []
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


def _forms(clinic_id: UUID, request: HttpRequest) -> dict[str, DefinitionForm]:
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
    bound = (
        {request.POST.get("action", ""): request.POST}
        if request.method == "POST"
        else {}
    )
    result: dict[str, DefinitionForm] = {
        "resource": ResourceForm(data=bound.get("resource"), prefix="resource"),
        "service": ServiceForm(data=bound.get("service"), prefix="service"),
        "template": TemplateForm(
            data=bound.get("template"),
            prefix="template",
            practitioners=subjects["practitioners"],
            resources=subjects["resources"],
        ),
        "closure": ClosureForm(
            data=bound.get("closure"),
            prefix="closure",
            practitioners=subjects["practitioners"],
            resources=subjects["resources"],
        ),
        "generate": GenerateForm(data=bound.get("generate"), prefix="generate"),
        "retire": RetireForm(data=bound.get("retire"), prefix="retire"),
    }
    widget = result["generate"].fields["template_id"].widget
    if isinstance(widget, django_forms.Select):
        widget.choices = [
            (str(row["pk"]), str(row["label"]))
            for row in _template_catalog(clinic_id)
            if row["active"]
        ]
    return result
