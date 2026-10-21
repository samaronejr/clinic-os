"""Native form adapter for the three longitudinal fact commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError

from apps.ehr.longitudinal import (
    MedicationInput,
    ObservationInput,
    record_medication_statement,
    record_observation,
    record_vitals,
)
from apps.ehr.longitudinal_values import CODES
from apps.ehr.services import ClinicalAccessDeniedError
from apps.ehr.workspace_forms import MedicationForm, ObservationForm, VitalsForm

if TYPE_CHECKING:
    from django.forms import Form
    from django.http import QueryDict

    from apps.ehr.longitudinal_access import PatientScope


def record_form(
    *, scope: PatientScope, data: QueryDict, forms: dict[str, Form]
) -> bool:
    """Retain invalid input and never infer clinician confirmation from saving."""
    action = data.get("action", "")
    form_type = {
        "observation": ObservationForm,
        "vitals": VitalsForm,
        "medication": MedicationForm,
    }.get(action)
    if form_type is None:
        raise ClinicalAccessDeniedError
    form = form_type(data, prefix=action)
    forms[action] = form
    if not form.is_valid():
        return False
    values = form.cleaned_data
    try:
        match action:
            case "observation":
                record_observation(
                    clinic_id=scope.clinic_id,
                    enrollment_id=scope.enrollment_id,
                    observation=ObservationInput(
                        kind=values["kind"],
                        value=values["value"],
                        unit=values["unit"],
                        provenance=values["provenance"],
                        reference_low=values["reference_low"] or None,
                        reference_high=values["reference_high"] or None,
                    ),
                )
            case "vitals":
                record_vitals(
                    clinic_id=scope.clinic_id,
                    enrollment_id=scope.enrollment_id,
                    measurements=tuple(
                        ObservationInput(
                            kind=kind,
                            value=values[kind],
                            unit=CODES[kind].unit,
                            provenance=values["provenance"],
                        )
                        for kind in CODES
                        if kind not in {"bmi", "pain_present"} and values[kind]
                    ),
                )
            case "medication":
                record_medication_statement(
                    clinic_id=scope.clinic_id,
                    enrollment_id=scope.enrollment_id,
                    medication=MedicationInput(**values),
                )
    except ValidationError as error:
        form.add_error(None, error)
        return False
    return True
