"""Emergency read grants; professional, restricted-note and write limits remain."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import connection, transaction
from django.db.models.functions import Now
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.ehr.models import ClinicalDocumentVersion, Observation
from apps.identity import enterprise_authority as clock
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.enterprise_authority import GovernanceDeniedError, factor_scope
from apps.identity.enterprise_support_access import (
    approve_support_access,
    support_diagnostics,
)
from apps.identity.models import BreakGlassGrant, GovernanceNotice
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

__all__ = (
    "EmergencyScope",
    "GovernanceDeniedError",
    "approve_support_access",
    "grant_break_glass",
    "read_break_glass",
    "review_break_glass",
    "revoke_break_glass",
    "support_diagnostics",
)
MAX_EMERGENCY_REASON = 2000


@dataclass(frozen=True, slots=True)
class EmergencyScope:
    """Represent either a patient enrollment or one encounter inside it."""

    enrollment_id: UUID
    encounter_id: UUID | None = None


def utc_now() -> datetime:
    """One clock seam for expiry tests; the database independently enforces expiry."""
    return clock.utc_now()


@dataclass(frozen=True, slots=True)
class BreakGlassSnapshot:
    """Authorized transient output, never persisted in a browser or audit event."""

    enrollment_id: UUID
    notes: tuple[ClinicalDocumentVersion, ...]
    observations: tuple[Observation, ...]


def grant_break_glass(
    *,
    clinic_id: UUID,
    scope: EmergencyScope,
    reason: str,
    expires_at: datetime,
    session_key: str,
) -> BreakGlassGrant:
    """Require an actual factor, current profession, reason, scope and <=4 hours."""
    now = utc_now()
    if (
        not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= MAX_EMERGENCY_REASON
        or timezone.is_naive(expires_at)
        or not now < expires_at <= now + timedelta(hours=4)
    ):
        raise GovernanceDeniedError
    with transaction.atomic(), factor_scope(session_key=session_key) as actor:
        try:
            require_permission("break_glass.request", clinic_id=clinic_id)
        except CurrentActorError:
            try:
                require_permission("break_glass.request_scoped", clinic_id=clinic_id)
            except CurrentActorError as error:
                raise GovernanceDeniedError from error
        enrollment = PatientClinicEnrollment.objects.filter(
            pk=scope.enrollment_id, clinic_id=clinic_id
        ).first()
        if enrollment is None:
            raise GovernanceDeniedError
        grant = BreakGlassGrant.objects.create(
            organization_id=enrollment.organization_id,
            clinic_id=clinic_id,
            clinician_id=actor,
            enrollment=enrollment,
            encounter_id=scope.encounter_id,
            reason=reason.strip(),
            expires_at=expires_at,
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT clinic_app.enterprise_create_review(%s)", [grant.pk])
            row = cursor.fetchone()
            if row is None or row[0] is None:
                raise GovernanceDeniedError
            grant.review_task_id = row[0]
        record_phase1_event(
            "workflows.task.created",
            clinic_id=clinic_id,
            affected_record_id=grant.review_task_id,
        )
        for audience in ("manager", "patient"):
            notice = GovernanceNotice.objects.create(
                organization_id=enrollment.organization_id,
                clinic_id=clinic_id,
                break_glass=grant,
                audience=audience,
            )
            record_phase1_event(
                "identity.break_glass.notified",
                clinic_id=clinic_id,
                affected_record_id=notice.pk,
            )
        record_phase1_event(
            "identity.break_glass.granted",
            clinic_id=clinic_id,
            affected_record_id=grant.pk,
        )
        return grant


def read_break_glass(*, clinic_id: UUID, grant_id: UUID) -> BreakGlassSnapshot:
    """Recheck the exact live grant without writes or restricted notes."""
    with transaction.atomic():
        grant = BreakGlassGrant.objects.filter(pk=grant_id, clinic_id=clinic_id).first()
        if (
            grant is None
            or grant.revoked_at is not None
            or grant.reviewed_at is not None
            or not grant.created_at <= utc_now() < grant.expires_at
        ):
            raise GovernanceDeniedError
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.enterprise_break_glass_usable(%s,%s)",
                [grant_id, clinic_id],
            )
            if cursor.fetchone() != (True,):
                raise GovernanceDeniedError
        notes = tuple(
            ClinicalDocumentVersion.objects.raw(
                "SELECT id,organization_id,document_id,template_id,author_id,"
                "amendment_of_id,amendment_reason,version,revision,state,content,"
                "content_sha256,content_digest,finalized_at,created_at,updated_at "
                "FROM clinic_app.enterprise_break_glass_versions(%s)",
                [grant.pk],
            )
        )
        observations = tuple(
            Observation.objects.raw(
                "SELECT * FROM clinic_app.enterprise_break_glass_observations(%s)",
                [grant.pk],
            )
        )
        record_phase1_event(
            "identity.break_glass.read",
            clinic_id=clinic_id,
            affected_record_id=grant.pk,
        )
        return BreakGlassSnapshot(grant.enrollment_id, notes, observations)


def revoke_break_glass(*, clinic_id: UUID, grant_id: UUID, session_key: str) -> None:
    """Immediately end an own grant or a manager-visible grant."""
    with transaction.atomic(), factor_scope(session_key=session_key):
        grant = (
            BreakGlassGrant.objects.select_for_update()
            .filter(pk=grant_id, clinic_id=clinic_id)
            .first()
        )
        if grant is None:
            raise GovernanceDeniedError
        if grant.revoked_at is None:
            BreakGlassGrant.objects.filter(pk=grant.pk).update(revoked_at=Now())
            record_phase1_event(
                "identity.break_glass.revoked",
                clinic_id=clinic_id,
                affected_record_id=grant.pk,
            )


def review_break_glass(*, clinic_id: UUID, grant_id: UUID, session_key: str) -> None:
    """Complete the mandatory task only after the event ended, under manager MFA."""
    with (
        transaction.atomic(),
        factor_scope(session_key=session_key),
        connection.cursor() as cursor,
    ):
        grant = BreakGlassGrant.objects.filter(pk=grant_id, clinic_id=clinic_id).first()
        if grant is None or grant.review_task_id is None:
            raise GovernanceDeniedError
        cursor.execute("SELECT clinic_app.enterprise_manager(%s)", [clinic_id])
        if cursor.fetchone() != (True,):
            raise GovernanceDeniedError
        if grant.revoked_at is None and utc_now() < grant.expires_at:
            raise GovernanceDeniedError
        cursor.execute("SELECT clinic_app.enterprise_finish_review(%s)", [grant.pk])
        if cursor.fetchone() != (True,):
            if grant.reviewed_at is not None:
                return
            raise GovernanceDeniedError
        for verb in ("assigned", "started", "completed"):
            record_phase1_event(
                f"workflows.task.{verb}",
                clinic_id=clinic_id,
                affected_record_id=grant.review_task_id,
            )
        record_phase1_event(
            "identity.break_glass.reviewed",
            clinic_id=clinic_id,
            affected_record_id=grant.pk,
        )
