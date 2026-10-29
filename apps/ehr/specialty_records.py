"""Record facts and procedure source links without prescribing a schedule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import TYPE_CHECKING, Final

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from apps.audit.services import record_phase1_event
from apps.ehr.longitudinal_access import patient_scope
from apps.ehr.longitudinal_values import ORIGINS
from apps.ehr.models import ClinicalAttachment, Encounter
from apps.ehr.pack_models import SpecialtyEntry, SpecialtyPack
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    _encounter_actor,
    decided,
)
from apps.identity.overlay_content import validate_overlay_text
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from uuid import UUID

MAX_VACCINE: Final = 240
MAX_DOSE: Final = 120
MAX_PLAN: Final = 4000
MAX_IMAGES: Final = 20


@dataclass(frozen=True, slots=True)
class ImmunizationInput:
    """Reported historical administration, not a due date or prescription."""

    vaccine: str
    administered_on: date
    dose: str
    provenance: str


@dataclass(frozen=True, slots=True)
class ProcedureInput:
    """Consent is a linked document, never inferred from submitting a plan."""

    plan: str
    consent_document_id: UUID
    image_ids: tuple[UUID, ...] = ()


def _authority(*, clinic_id: UUID, encounter_id: UUID) -> Encounter:
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    _encounter_actor(clinic_id, encounter)
    enrollment = PatientClinicEnrollment.objects.filter(
        clinic_id=clinic_id, patient_id=encounter.patient_id
    ).first()
    if enrollment is None:
        raise ClinicalAccessDeniedError
    patient_scope(clinic_id=clinic_id, enrollment_id=enrollment.pk, action="write")
    if encounter.state != Encounter.State.OPEN:
        reason = "encounter_closed"
        raise ClinicalConflictError(reason)
    return encounter


def _pack(*, clinic_id: UUID, key: str) -> SpecialtyPack:
    row = (
        SpecialtyPack.objects.filter(
            clinic_id=clinic_id, key=key, uninstalled_at__isnull=True
        )
        .order_by("-version")
        .first()
    )
    if row is None:
        reason = "pack_unavailable"
        raise ClinicalConflictError(reason)
    return row


def record_immunization(
    *, clinic_id: UUID, encounter_id: UUID, record: ImmunizationInput
) -> SpecialtyEntry:
    """Retain a sourced report without confirming it or predicting the next dose."""
    authority = partial(_authority, clinic_id=clinic_id, encounter_id=encounter_id)
    encounter = authority()
    if (
        not record.vaccine.strip()
        or len(record.vaccine) > MAX_VACCINE
        or not record.dose.strip()
        or len(record.dose) > MAX_DOSE
        or type(record.administered_on) is not date
        or record.provenance not in ORIGINS
    ):
        raise ValidationError(_("Check the immunization record and its source."))
    validate_overlay_text(record.vaccine)
    validate_overlay_text(record.dose)
    with decided(authority):
        pack = _pack(clinic_id=clinic_id, key="pediatrics")
        row = SpecialtyEntry.objects.create(
            organization_id=encounter.organization_id,
            clinic_id=clinic_id,
            pack=pack,
            encounter=encounter,
            kind="immunization",
            details={
                "vaccine": record.vaccine,
                "administered_on": record.administered_on.isoformat(),
                "dose": record.dose,
                "origin": record.provenance,
            },
            recorded_by_id=encounter.physician_id,
        )
        record_phase1_event(
            "ehr.specialty.recorded", clinic_id=clinic_id, affected_record_id=row.pk
        )
        return row


def record_procedure(
    *, clinic_id: UUID, encounter_id: UUID, record: ProcedureInput
) -> SpecialtyEntry:
    """Bind available consent and image artifacts to this exact encounter."""
    authority = partial(_authority, clinic_id=clinic_id, encounter_id=encounter_id)
    encounter = authority()
    if (
        not record.plan.strip()
        or len(record.plan) > MAX_PLAN
        or len(record.image_ids) > MAX_IMAGES
        or len(set(record.image_ids)) != len(record.image_ids)
    ):
        raise ValidationError(_("Check the procedure plan and source documents."))
    validate_overlay_text(record.plan)
    attachments = ClinicalAttachment.objects.filter(
        encounter=encounter, state="available"
    )
    if not attachments.filter(pk=record.consent_document_id).exists() or (
        attachments.filter(
            pk__in=record.image_ids, detected_type__in=("image/jpeg", "image/png")
        ).count()
        != len(record.image_ids)
    ):
        raise ClinicalAccessDeniedError
    with decided(authority):
        pack = _pack(clinic_id=clinic_id, key="procedures")
        row = SpecialtyEntry.objects.create(
            organization_id=encounter.organization_id,
            clinic_id=clinic_id,
            pack=pack,
            encounter=encounter,
            kind="procedure",
            details={"plan": record.plan},
            consent_document_id=record.consent_document_id,
            images=[str(identifier) for identifier in record.image_ids],
            recorded_by_id=encounter.physician_id,
        )
        record_phase1_event(
            "ehr.specialty.recorded", clinic_id=clinic_id, affected_record_id=row.pk
        )
        return row
