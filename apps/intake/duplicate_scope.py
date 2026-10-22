"""Ordered scope locks for one human-adjudicated patient pair."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import connection

from apps.intake.alias_access import (
    DuplicateConflictError,
    MergeCrossOrganizationError,
    PatientAliasedError,
    review_actor,
    verified_review_session,
)
from apps.intake.models import DuplicateCandidate, PatientAlias, PatientClinicEnrollment
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    patient_lock_key,
    resource_lock_keys,
    user_lock_keys,
)
from apps.scheduling.models import Appointment

if TYPE_CHECKING:
    from uuid import UUID

    from apps.identity.models import Clinic


@dataclass(frozen=True, slots=True)
class AdjudicationScope:
    """Stored enrollments, actual actor and every affected authorized clinic."""

    actor_id: UUID
    clinic: Clinic
    candidate: DuplicateCandidate
    original: PatientClinicEnrollment
    survivor: PatientClinicEnrollment
    clinic_ids: tuple[UUID, ...]


def lock_candidate(
    *, clinic_id: UUID, candidate_id: UUID, survivor_patient_id: UUID
) -> AdjudicationScope:
    """Acquire clinic, practitioner, resource and patient gates in native order."""
    actor_id, clinic = review_actor(clinic_id)
    verified_review_session(actor_id)
    discovered = (
        DuplicateCandidate.objects.select_related("enrollment_a", "enrollment_b")
        .filter(pk=candidate_id, clinic_id=clinic_id)
        .first()
    )
    if discovered is None:
        raise MergeCrossOrganizationError
    patient_ids = (
        discovered.enrollment_a.patient_id,
        discovered.enrollment_b.patient_id,
    )
    clinic_ids = tuple(
        sorted(
            set(
                PatientClinicEnrollment.objects.filter(
                    patient_id__in=patient_ids
                ).values_list("clinic_id", flat=True)
            )
        )
    )
    for affected_clinic in clinic_ids:
        review_actor(affected_clinic)
    acquire_advisory_locks(tuple(clinic_lock_key(cid) for cid in clinic_ids))
    forward_rows = tuple(
        Appointment.objects.filter(
            patient_id__in=patient_ids,
            status__in=("requested", "held", "scheduled", "arrived"),
        )
        .only("practitioner_id", "resource_ids")
        .order_by("id")
    )
    acquire_advisory_locks(
        user_lock_keys(tuple(sorted({row.practitioner_id for row in forward_rows})))
    )
    acquire_advisory_locks(
        resource_lock_keys(
            tuple(sorted({key for row in forward_rows for key in row.resource_ids}))
        )
    )
    candidate = (
        DuplicateCandidate.objects.select_for_update(of=("self",))
        .select_related("enrollment_a__patient", "enrollment_b__patient")
        .filter(pk=candidate_id, clinic_id=clinic_id)
        .first()
    )
    if candidate is None:
        raise MergeCrossOrganizationError
    if candidate.state != DuplicateCandidate.State.OPEN:
        code = "duplicate_stale"
        raise DuplicateConflictError(code)
    enrollments = (candidate.enrollment_a, candidate.enrollment_b)
    survivor = next(
        (row for row in enrollments if row.patient_id == survivor_patient_id), None
    )
    if survivor is None:
        raise MergeCrossOrganizationError
    original = next(row for row in enrollments if row.pk != survivor.pk)
    acquire_advisory_locks(
        tuple(
            patient_lock_key(clinic.organization_id, patient_id)
            for patient_id in sorted(patient_ids)
        )
    )
    with connection.cursor() as cursor:
        for patient_id in sorted(patient_ids):
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                [f"intake-alias-write-v1:{clinic.organization_id}:{patient_id}"],
            )
    if PatientAlias.objects.filter(
        active=True, merged_patient_id__in=patient_ids
    ).exists():
        raise PatientAliasedError
    return AdjudicationScope(
        actor_id, clinic, candidate, original, survivor, clinic_ids
    )
