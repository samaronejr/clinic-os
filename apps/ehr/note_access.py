"""Explicit EHR note grants augment, but never replace, current clinical scope."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from django.db import connection
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.ehr.longitudinal_access import patient_scope
from apps.ehr.models import ClinicalDocumentVersion
from apps.ehr.note_models import NoteAccessGrant, RestrictedNote
from apps.ehr.services import ClinicalAccessDeniedError, author_scope, decided
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_permission,
)
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from uuid import UUID


def _author(*, clinic_id: UUID, version_id: UUID) -> ClinicalDocumentVersion:
    author_scope(clinic_id=clinic_id, version_id=version_id)
    version = (
        ClinicalDocumentVersion.objects.filter(
            pk=version_id, document__encounter__clinic_id=clinic_id
        )
        .select_related("document__encounter")
        .only(
            "id",
            "organization_id",
            "author_id",
            "document_id",
            "document__encounter_id",
            "document__encounter__patient_id",
            "document__encounter__clinic_id",
        )
        .first()
    )
    if version is None:
        raise ClinicalAccessDeniedError
    enrollment = PatientClinicEnrollment.objects.filter(
        clinic_id=clinic_id, patient_id=version.document.encounter.patient_id
    ).first()
    if enrollment is None:
        raise ClinicalAccessDeniedError
    patient_scope(clinic_id=clinic_id, enrollment_id=enrollment.pk, action="write")
    try:
        require_permission(
            "clinical.read", clinic_id=clinic_id, patient_enrollment_id=enrollment.pk
        )
    except CurrentActorError as error:
        raise ClinicalAccessDeniedError from error
    return version


def _lock(document_id: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"ehr-note-access:{document_id}"],
        )


def restrict_note(*, clinic_id: UUID, version_id: UUID) -> RestrictedNote:
    """Restrict this document and all its versions; no grants are invented."""
    authority = partial(_author, clinic_id=clinic_id, version_id=version_id)
    version = authority()
    with decided(authority):
        _lock(version.document_id)
        row, created = RestrictedNote.objects.get_or_create(
            document_id=version.document_id,
            defaults={
                "organization_id": version.organization_id,
                "clinic_id": clinic_id,
                "restricted_by_id": version.author_id,
            },
        )
        if created:
            record_phase1_event(
                "ehr.note.restricted", clinic_id=clinic_id, affected_record_id=row.pk
            )
        return row


def grant_note_access(
    *, clinic_id: UUID, version_id: UUID, grantee_id: UUID
) -> NoteAccessGrant:
    """Grant one current clinic physician additional read scope, revocably."""
    authority = partial(_author, clinic_id=clinic_id, version_id=version_id)
    version = authority()
    note = RestrictedNote.objects.filter(document_id=version.document_id).first()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ehr_note_grantee(%s,%s)", [clinic_id, grantee_id]
        )
        eligible = cursor.fetchone() == (True,)
    if note is None or not eligible:
        raise ClinicalAccessDeniedError
    with decided(authority):
        _lock(version.document_id)
        row, created = NoteAccessGrant.objects.get_or_create(
            note=note,
            grantee_id=grantee_id,
            revoked_at__isnull=True,
            defaults={
                "organization_id": note.organization_id,
                "clinic_id": clinic_id,
                "granted_by_id": current_actor_id(),
            },
        )
        if created:
            record_phase1_event(
                "ehr.note.granted", clinic_id=clinic_id, affected_record_id=row.pk
            )
        return row


def revoke_note_access(*, clinic_id: UUID, grant_id: UUID) -> NoteAccessGrant:
    """Retain a terminal revocation; a retry appends no second event."""
    grant = (
        NoteAccessGrant.objects.filter(pk=grant_id, clinic_id=clinic_id)
        .select_related("note")
        .first()
    )
    if grant is None or grant.granted_by_id != current_actor_id():
        raise ClinicalAccessDeniedError
    version = ClinicalDocumentVersion.objects.filter(
        document_id=grant.note.document_id, author_id=current_actor_id()
    ).first()
    if version is None:
        raise ClinicalAccessDeniedError
    authority = partial(_author, clinic_id=clinic_id, version_id=version.pk)
    authority()
    with decided(authority):
        _lock(grant.note.document_id)
        row = NoteAccessGrant.objects.filter(pk=grant_id).first()
        if row is None:
            raise ClinicalAccessDeniedError
        if row.revoked_at is None:
            row.revoked_at = timezone.now()
            row.save(update_fields=("revoked_at",))
        return row
