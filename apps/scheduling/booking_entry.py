"""Body-only staff booking entry without opening legacy patient administration."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_forms import AppointmentCreateForm
from apps.scheduling.forms import BLANK_CHOICE
from apps.scheduling.models import Resource, ServiceType
from apps.scheduling.resource_booking import (
    authorized_service_clinic,
    service_practitioners,
)

if TYPE_CHECKING:
    from uuid import UUID


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


def service_booking_entry(clinic_id: UUID) -> AppointmentCreateForm | None:
    """Offer only the current RP actor's scoped, active service choices."""
    try:
        clinic = authorized_service_clinic(clinic_id)
    except AppointmentAccessDeniedError:
        return None
    services = list(
        ServiceType.objects.filter(clinic=clinic, active=True).order_by("name", "pk")
    )
    if not services:
        return None
    practitioners = tuple(
        (str(pk), label) for pk, label in service_practitioners(clinic_id)
    )
    form = AppointmentCreateForm(practitioners)
    form.initial["idempotency_key"] = uuid4()
    enrollment = form.fields["enrollment_id"]
    enrollment.label = _("Patient")
    enrollment.widget = forms.Select(
        choices=[
            ("", _("Not selected")),
            *(
                (str(row.pk), row.patient.full_name)
                for row in PatientClinicEnrollment.objects.filter(
                    organization_id=clinic.organization_id, clinic_id=clinic_id
                )
                .select_related("patient")
                .order_by("pk")
            ),
        ]
    )
    form.fields["practitioner"].label = _("Professional")
    practitioner = form.fields["practitioner"]
    if isinstance(practitioner, forms.ChoiceField):
        practitioner.choices = [("", _("Not selected")), *practitioners]
    form.fields["service_type_id"].required = True
    form.fields["service_type_id"].label = _("Service type")
    form.configure_resources(
        [(str(row.pk), f"{row.name} ({row.duration_min} min)") for row in services],
        [
            (str(row.pk), row.name)
            for row in Resource.objects.filter(clinic=clinic, active=True).order_by(
                "kind", "name", "pk"
            )
        ],
    )
    service_widget = form.fields["service_type_id"].widget
    if isinstance(service_widget, forms.Select):
        service_widget.choices = [
            ("", _("Not selected")),
            *(
                (str(row.pk), f"{row.name} ({row.duration_min} min)")
                for row in services
            ),
        ]
    return form
