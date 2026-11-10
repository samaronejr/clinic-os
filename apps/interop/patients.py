"""Register one migrated patient through intake's own invariants.

The import authority (``access.import_authority``) replaces intake's
reception-manager role gate for this path only; every derived value is
produced by intake itself: the canonical create fingerprint, the tenant HMAC
name index, the first demographics version and its receipt, identifier
blind indexes and the deterministic duplicate hints that route possible
duplicates to human review (todo 18). Nothing is ever merged automatically.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.core.idempotency import create_fingerprint
from apps.identity.current_context import current_actor_username
from apps.intake.demographics import (
    _append_identifier,
    _apply_demographics,
    _DemographicsMutation,
)
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.intake.patient_name_index import name_indexes
from apps.interop.access import Code, ImportConflictError

if TYPE_CHECKING:
    from apps.identity.models import Clinic
    from apps.interop.validation import PatientRow

IMPORT_KEY_SCOPE: Final = "patients"


@dataclass(frozen=True, slots=True)
class ImportedPatient:
    """The intake identity one source row created, and its identifier count."""

    patient_id: UUID
    enrollment_id: UUID
    identifier_count: int


def import_key(job_id: UUID, row: int) -> UUID:
    """Derive the enrollment command key the database guard recomputes."""
    text = f"{job_id}:{IMPORT_KEY_SCOPE}:{row}"
    return UUID(hex=hashlib.sha256(text.encode("utf-8")).hexdigest()[:32])


def register(
    *, clinic: Clinic, actor_id: UUID, job_id: UUID, row: PatientRow
) -> ImportedPatient:
    """Create the registry identity, first demographics version and identifiers."""
    registry_name = row.legal_name or row.social_name or ""
    fingerprint = create_fingerprint(
        "patient",
        {
            "birth_date": row.birth_date.isoformat() if row.birth_date else "",
            "clinic_id": str(clinic.pk),
            "full_name": registry_name,
        },
    )
    key = import_key(job_id, row.row)
    if PatientClinicEnrollment.objects.filter(
        organization_id=clinic.organization_id, idempotency_key=key
    ).exists():
        # A committed batch is atomic; an existing key means a forged replay.
        raise ImportConflictError(Code.INTEROP_STALE)
    with transaction.atomic():
        patient = Patient.objects.create(
            organization_id=clinic.organization_id,
            full_name=registry_name,
            full_name_index=name_indexes(registry_name)[-1],
            birth_date=row.birth_date,
        )
        enrollment = PatientClinicEnrollment.objects.create(
            organization_id=clinic.organization_id,
            clinic=clinic,
            patient=patient,
            idempotency_key=key,
            create_fingerprint=fingerprint,
        )
        record_phase1_event(
            "intake.patient.created",
            clinic_id=clinic.pk,
            affected_record_id=patient.pk,
        )
        # Identifiers first: the first duplicate hint intake records for this
        # identity then carries the exact-identifier signal, not only a prefix.
        for kind, value in row.identifiers:
            _append_identifier(
                clinic=clinic,
                patient_id=patient.pk,
                kind=kind,
                normalized=value,
                issuer=None,
            )
        changes: dict[str, object] = {
            "legal_name": row.legal_name or "",
            "social_name": row.social_name or "",
            "birth_date": row.birth_date,
        }
        if row.sex_at_birth is not None:
            changes["sex_at_birth"] = row.sex_at_birth
        _apply_demographics(
            _DemographicsMutation(
                clinic=clinic,
                enrollment=enrollment,
                actor_id=actor_id,
                actor_label=current_actor_username()[:150],
                expected_version=0,
                changes=changes,
                unknown={},
                reason="",
                source="staff_recorded",
            )
        )
    return ImportedPatient(
        patient_id=patient.pk,
        enrollment_id=enrollment.pk,
        identifier_count=len(row.identifiers),
    )
