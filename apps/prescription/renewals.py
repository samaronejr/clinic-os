"""Explicit renewal into a new encounter, with the class deliberately unset."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.ehr.models import Encounter
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.prescription.class_values import CLASSES_CONTRACT
from apps.prescription.directions import OPTIONAL_ITEM_FIELDS
from apps.prescription.models import (
    DocumentKind,
    PrescriptionDocument,
    PrescriptionDraft,
)
from apps.prescription.policy import ITEM_LIMITS
from apps.prescription.services import (
    DocumentStorageError,
    authorize_encounter,
    save_draft,
    verify_document_integrity,
)

if TYPE_CHECKING:
    from uuid import UUID


def renew_draft(
    *, clinic_id: UUID, encounter_id: UUID, source_document_id: UUID
) -> PrescriptionDraft:
    """Copy frozen directions for fresh review, never their class or authority."""
    encounter = authorize_encounter(clinic_id=clinic_id, encounter_id=encounter_id)
    source = PrescriptionDocument.objects.filter(
        pk=source_document_id,
        clinic_id=clinic_id,
        draft__kind=DocumentKind.PRESCRIPTION,
        patient_id=encounter.patient_id,
        signature_operations__state__in=("issued", "rehearsal_complete"),
        prescriptiondocumentrevocation__isnull=True,
    ).first()
    if source is None:
        raise ClinicalAccessDeniedError
    if not verify_document_integrity(source).ok:
        raise DocumentStorageError
    raw_items = source.frozen_input.get("items")
    if not isinstance(raw_items, list) or any(
        not isinstance(row, dict) for row in raw_items
    ):
        raise DocumentStorageError
    items = [
        {
            key: value
            for key, value in row.items()
            if key in (*ITEM_LIMITS, *OPTIONAL_ITEM_FIELDS)
        }
        for row in raw_items
    ]
    with transaction.atomic():
        locked = Encounter.objects.select_for_update().get(pk=encounter.pk)
        if (
            locked.state != "open"
            or PrescriptionDraft.objects.filter(
                encounter=locked, kind=DocumentKind.PRESCRIPTION
            ).exists()
        ):
            reason = "precondition_failed"
            raise ClinicalConflictError(reason)
        draft = PrescriptionDraft.objects.create(
            organization_id=locked.organization_id,
            clinic_id=clinic_id,
            encounter=locked,
            patient_id=locked.patient_id,
            issuer_id=locked.physician_id,
            category=None,
            contract_version=CLASSES_CONTRACT,
            renewal_source=source,
        )
        saved = save_draft(
            clinic_id=clinic_id,
            draft_id=draft.pk,
            encounter_id=locked.pk,
            patient_id=locked.patient_id,
            issuer_id=locked.physician_id,
            category=None,
            expected_version=1,
            items=items,
        )
        record_phase1_event(
            "prescription.draft.renewed",
            clinic_id=clinic_id,
            affected_record_id=draft.pk,
        )
        return saved
