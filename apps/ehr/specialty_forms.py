"""Native specialty entry fields and scoped choices; no clinical browser storage."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.ehr.models import ClinicalAttachment, Encounter
from apps.ehr.specialty_records import (
    ImmunizationInput,
    ProcedureInput,
    record_immunization,
    record_procedure,
)
from apps.ehr.workspace_forms import ORIGIN_CHOICES
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from django.http import QueryDict

    from apps.ehr.longitudinal_access import PatientScope


class ImmunizationForm(forms.Form):
    """An unconfirmed historical record, never a predicted immunization schedule."""

    vaccine = forms.CharField(label=_("Vaccine as documented"), max_length=240)
    administered_on = forms.DateField(
        label=_("Administration date"),
        input_formats=["%d/%m/%Y"],
        widget=forms.DateInput(format="%d/%m/%Y"),
        help_text=_("Use DD/MM/YYYY as recorded in the source."),
    )
    dose = forms.CharField(label=_("Dose as documented"), max_length=120)
    provenance = forms.ChoiceField(label=_("Origin"), choices=ORIGIN_CHOICES)


class ProcedureForm(forms.Form):
    """A source-linked plan does not imply execution or substitute for consent."""

    plan = forms.CharField(
        label=_("Procedure plan"), max_length=4000, widget=forms.Textarea
    )


def specialty_forms(
    *, scope: PatientScope, data: QueryDict | None = None
) -> dict[str, forms.Form]:
    """Show only this patient's open encounters and currently available sources."""
    clinic = Clinic.objects.get(pk=scope.clinic_id)
    if clinic.timezone is None:
        from apps.ehr.services import ClinicalAccessDeniedError  # noqa: PLC0415

        raise ClinicalAccessDeniedError
    encounters = Encounter.objects.filter(
        clinic_id=scope.clinic_id,
        patient_id=scope.patient_id,
        physician_id=scope.user_id,
        state="open",
    ).order_by("-created_at")
    choices = [
        (
            str(row.pk),
            timezone.localtime(row.created_at, ZoneInfo(clinic.timezone)).strftime(
                "%d/%m/%Y %H:%M"
            ),
        )
        for row in encounters
    ]
    result: dict[str, forms.Form] = {
        "immunization": ImmunizationForm(data, prefix="immunization"),
        "procedure": ProcedureForm(data, prefix="procedure"),
    }
    for form in result.values():
        form.fields["encounter_id"] = forms.ChoiceField(
            label=_("Open encounter"),
            choices=[("", _("Choose an option")), *choices],
        )
    attachments = list(
        ClinicalAttachment.objects.filter(
            encounter__in=encounters, state="available"
        ).order_by("-created_at")
    )
    result["procedure"].fields["consent_document_id"] = forms.ChoiceField(
        label=_("Consent document"),
        choices=[
            ("", _("Choose an option")),
            *[(str(row.pk), str(row.file_name)) for row in attachments],
        ],
    )
    result["procedure"].fields["image_ids"] = forms.MultipleChoiceField(
        label=_("Encounter images"),
        required=False,
        choices=[
            (str(row.pk), str(row.file_name))
            for row in attachments
            if row.detected_type in {"image/jpeg", "image/png"}
        ],
    )
    return result


def record_specialty_form(
    *, scope: PatientScope, data: QueryDict, forms_by_name: dict[str, forms.Form]
) -> bool:
    """Validate all bindings before the atomic service can append a record."""
    action = data.get("action", "")
    form = specialty_forms(scope=scope, data=data)[action]
    forms_by_name[action] = form
    if not form.is_valid():
        return False
    values = form.cleaned_data
    try:
        if action == "immunization":
            record_immunization(
                clinic_id=scope.clinic_id,
                encounter_id=UUID(values["encounter_id"]),
                record=ImmunizationInput(
                    values["vaccine"],
                    values["administered_on"],
                    values["dose"],
                    values["provenance"],
                ),
            )
        else:
            record_procedure(
                clinic_id=scope.clinic_id,
                encounter_id=UUID(values["encounter_id"]),
                record=ProcedureInput(
                    values["plan"],
                    UUID(values["consent_document_id"]),
                    tuple(UUID(identifier) for identifier in values["image_ids"]),
                ),
            )
    except ValidationError as error:
        form.add_error(None, error)
        return False
    return True
