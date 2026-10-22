"""Native POST command adapter; record selectors never enter a URL."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from apps.ehr.longitudinal import (
    MedicationReconciliation,
    confirm_observation,
    reconcile_medications,
)
from apps.ehr.models import (
    ClinicalDocumentVersion,
    Encounter,
    EncounterAddendum,
    MedicationStatement,
    Observation,
)
from apps.ehr.note_models import CopiedNoteBlock
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.ehr.workspace_note_commands import note_command
from apps.ehr.workspace_recording import record_form

if TYPE_CHECKING:
    from django.forms import Form
    from django.http import QueryDict

    from apps.ehr.longitudinal_access import PatientScope

POST_ACTIONS = frozenset(
    {
        "open",
        "tab",
        "filter",
        "observation",
        "vitals",
        "medication",
        "immunization",
        "procedure",
        "reconcile",
        "confirm_observation",
        "restrict",
        "copy",
        "review_copy",
        "open_addendum",
        "save_addendum",
    }
)


def _binding(*, scope: PatientScope, data: QueryDict) -> None:
    """Keep every command bound to the displayed patient."""
    action = data.get("action")
    visible = True
    match action:
        case "immunization" | "procedure":
            prefix = f"{action}-encounter_id"
            visible = Encounter.objects.filter(
                pk=UUID(data.get(prefix, "")),
                clinic_id=scope.clinic_id,
                patient_id=scope.patient_id,
            ).exists()
        case "reconcile":
            visible = MedicationStatement.objects.filter(
                pk=UUID(data.get("statement_id", "")),
                clinic_id=scope.clinic_id,
                patient_id=scope.patient_id,
            ).exists()
        case "confirm_observation":
            visible = Observation.objects.filter(
                pk=UUID(data.get("observation_id", "")),
                clinic_id=scope.clinic_id,
                patient_id=scope.patient_id,
            ).exists()
        case "restrict" | "copy":
            identifier = data.get(
                "target_version_id" if action == "copy" else "version_id", ""
            )
            visible = ClinicalDocumentVersion.objects.filter(
                pk=UUID(identifier),
                document__encounter__clinic_id=scope.clinic_id,
                document__encounter__patient_id=scope.patient_id,
            ).exists()
        case "review_copy":
            visible = CopiedNoteBlock.objects.filter(
                pk=UUID(data.get("block_id", "")),
                clinic_id=scope.clinic_id,
                target__document__encounter__patient_id=scope.patient_id,
            ).exists()
        case "open_addendum":
            visible = Encounter.objects.filter(
                pk=UUID(data.get("encounter_id", "")),
                clinic_id=scope.clinic_id,
                patient_id=scope.patient_id,
            ).exists()
        case "save_addendum":
            visible = EncounterAddendum.objects.filter(
                pk=UUID(data.get("addendum_id", "")),
                clinic_id=scope.clinic_id,
                patient_id=scope.patient_id,
            ).exists()
    if not visible:
        raise ClinicalAccessDeniedError


def apply_action(
    *, scope: PatientScope, data: QueryDict, forms: dict[str, Form]
) -> str:
    """Validate one native command before delegating its atomic domain change."""
    action = data.get("action", "")
    if action not in POST_ACTIONS:
        raise ClinicalAccessDeniedError
    _binding(scope=scope, data=data)
    match action:
        case "open" | "tab" | "filter":
            return ""
        case "observation" | "vitals" | "medication" | "immunization" | "procedure":
            if not record_form(scope=scope, data=data, forms=forms):
                return ""
        case "reconcile":
            if data.get("reviewed") != "yes":
                raise ValidationError(
                    _("Confirm that you reviewed the selected medication.")
                )
            reconcile_medications(
                clinic_id=scope.clinic_id,
                enrollment_id=scope.enrollment_id,
                statements=(
                    MedicationReconciliation(
                        statement_id=UUID(data.get("statement_id", "")),
                        expected_revision=int(data.get("revision", "")),
                        status=data.get("status", ""),
                    ),
                ),
            )
        case "confirm_observation":
            if data.get("reviewed") != "yes":
                raise ValidationError(_("Confirm that you reviewed this observation."))
            confirm_observation(
                clinic_id=scope.clinic_id,
                observation_id=UUID(data.get("observation_id", "")),
                expected_revision=int(data.get("revision", "")),
            )
        case _:
            note_command(clinic_id=scope.clinic_id, data=data)
    return _("Saved to the clinical record. The origin is preserved.")


def action_result(
    *, scope: PatientScope, data: QueryDict, forms: dict[str, Form]
) -> tuple[str, str, HTTPStatus]:
    """Keep rejected commands atomic and return only fixed, translated feedback."""
    try:
        with transaction.atomic():
            outcome = apply_action(scope=scope, data=data, forms=forms)
    except ValidationError as error:
        return "", " ".join(error.messages), HTTPStatus.BAD_REQUEST
    except ClinicalConflictError as error:
        message = {
            "copied_blocks_unreviewed": _(
                "Review every copied block at the current revision before finalizing."
            ),
            "restricted_source": _(
                "Restrict the destination note before copying a restricted source."
            ),
        }.get(
            error.reason_code,
            _("The record changed. Reload and review before saving."),
        )
        return "", message, HTTPStatus.CONFLICT
    if any(form.errors for form in forms.values()):
        return "", "", HTTPStatus.BAD_REQUEST
    return outcome, "", HTTPStatus.OK
