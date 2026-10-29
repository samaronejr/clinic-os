"""Atomic issuance commands; real issuance remains BLOCKED-ON-EG."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID

from config.settings.contracts import SYNTHETIC_DATA_MODE, resolved_data_mode
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.audit.services import record_phase1_event
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.prescription.class_values import PrescriptionClass
from apps.prescription.models import (
    DocumentKind,
    PrescriptionClassPolicy,
    PrescriptionDocument,
    PrescriptionDocumentRevocation,
    PrescriptionIssuance,
    SignatureOperation,
)
from apps.prescription.services import (
    DocumentStorageError,
    authorize_encounter,
    require_current_document,
)
from apps.prescription.sncr import SncrUnavailableError
from apps.providers.models import CapabilityVersion
from apps.providers.services import current_version, is_live

if TYPE_CHECKING:
    from apps.prescription.models import PrescriptionDraft


def _require_class(document: PrescriptionDocument) -> None:
    category = document.frozen_input.get("category")
    if category is None:
        raise ValidationError(code="class_required", message="class_required")
    policy = PrescriptionClassPolicy.objects.filter(pk=category).first()
    if policy is None:
        raise SncrUnavailableError
    if policy.requires_sncr:
        version = current_version("sncr", clinic_id=document.clinic_id)
        if version is None or version.state != CapabilityVersion.State.ACTIVATED:
            reason = "sncr_required"
            raise SncrUnavailableError(reason)
        if not is_live("sncr", clinic_id=document.clinic_id):
            raise SncrUnavailableError
    if category != PrescriptionClass.SYNTHETIC:
        # Class/model approval, qualified trust and registration remain closed.
        raise SncrUnavailableError
    if resolved_data_mode() != SYNTHETIC_DATA_MODE or not getattr(
        settings, "PRESCRIPTION_SYNTHETIC_ISSUANCE", False
    ):
        raise SncrUnavailableError


def issue(
    *, clinic_id: UUID, document_id: UUID, command_id: UUID
) -> PrescriptionIssuance:
    """Authorize before reading receipts and roll back every refused command."""
    if type(command_id) is not UUID or command_id.int == 0:
        raise ValidationError(code="invalid_command", message="invalid_command")
    try:
        with transaction.atomic():
            return _issue(clinic_id, document_id, command_id)
    except IntegrityError as error:
        reason = "issuance_command_conflict"
        raise ClinicalConflictError(reason) from error


def _issue(
    clinic_id: UUID, document_id: UUID, command_id: UUID
) -> PrescriptionIssuance:
    document = PrescriptionDocument.objects.filter(
        pk=document_id, clinic_id=clinic_id
    ).first()
    if document is None:
        raise ClinicalAccessDeniedError
    encounter = authorize_encounter(
        clinic_id=clinic_id, encounter_id=document.encounter_id
    )
    if document.issuer_id != encounter.physician_id:
        raise ClinicalAccessDeniedError
    draft: PrescriptionDraft = document.draft
    if draft.kind != DocumentKind.PRESCRIPTION:
        raise SncrUnavailableError
    _require_class(document)
    # Match the signing/draft mutation lock order and serialize same-document
    # commands without granting UPDATE on immutable rendered artifacts.
    type(draft).objects.select_for_update().get(pk=draft.pk)
    existing = PrescriptionIssuance.objects.filter(
        clinic_id=clinic_id, command_id=command_id
    ).first()
    if existing is not None:
        if existing.document_id != document_id:
            reason = "issuance_command_conflict"
            raise ClinicalConflictError(reason)
        return existing
    if PrescriptionIssuance.objects.filter(document=document).exists():
        reason = "already_completed"
        raise ClinicalConflictError(reason)
    require_current_document(document)
    if PrescriptionDocumentRevocation.objects.filter(document=document).exists():
        reason = "document_revoked"
        raise ClinicalConflictError(reason)
    signature = SignatureOperation.objects.filter(
        document=document,
        state=SignatureOperation.State.REHEARSAL_COMPLETE,
        category=PrescriptionClass.SYNTHETIC,
    ).first()
    if signature is None:
        reason = "verified_signature_required"
        raise ClinicalConflictError(reason)
    if (
        signature.content_digest != document.pdf_digest
        or sha256(bytes(signature.signed_bytes)).hexdigest() != signature.signed_digest
    ):
        raise DocumentStorageError
    receipt = PrescriptionIssuance.objects.create(
        organization_id=document.organization_id,
        clinic_id=clinic_id,
        issuer_id=document.issuer_id,
        document=document,
        signature=signature,
        command_id=command_id,
    )
    record_phase1_event(
        "prescription.issuance.rehearsed",
        clinic_id=clinic_id,
        affected_record_id=receipt.pk,
    )
    return receipt
