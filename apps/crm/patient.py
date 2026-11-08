"""Optional channel choices and post-visit feedback under patient authority."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection, transaction

from apps.audit.events import build_phase1_audit_event
from apps.audit.services import AuditTrustedContext, _content_hash
from apps.consent.services import patient_authority
from apps.core.integration import hold_subject_mutation_key
from apps.crm.access import CrmRuleError, Reason
from apps.crm.campaigns import marketing_lock
from apps.crm.models import MarketingPreference, SatisfactionSurvey
from apps.crm.rules import MAX_SCORE
from apps.scheduling.patient_authority import require_patient_booking_scope
from apps.scheduling.patient_booking import patient_appointment

if TYPE_CHECKING:
    from uuid import UUID


def _audit(
    event_name: str,
    record_id: UUID,
    organization_id: UUID,
    session_id: UUID,
    clinic_id: UUID,
) -> None:
    event = build_phase1_audit_event(
        event_name, clinic_id=clinic_id, affected_record_id=record_id
    )
    digest = _content_hash(
        event.event,
        AuditTrustedContext(organization_id=organization_id, actor_user_id=session_id),
        dict(event.payload),
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.crm_patient_audit(%s,%s,%s,%s)",
            [record_id, event_name, event.event.occurred_at_utc, digest],
        )


def set_marketing_preference(*, channel: str, opted_in: bool) -> MarketingPreference:
    """Record a patient's own channel choice without conditioning care on it."""
    scope = patient_authority()
    if channel not in {"email", "sms", "whatsapp"} or type(opted_in) is not bool:
        raise CrmRuleError(Reason.INVALID_PREFERENCE)
    with transaction.atomic():
        hold_subject_mutation_key(marketing_lock(scope.clinic_id))
        previous = (
            MarketingPreference.objects.filter(
                enrollment_id=scope.enrollment_id, channel=channel
            )
            .order_by("-revision")
            .first()
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT clinic_app.crm_contact_version(%s)", [channel])
            row = cursor.fetchone()
        version = row[0] if row and row[0] else 0
        if opted_in and not version:
            raise CrmRuleError(Reason.VERIFIED_CHANNEL)
        # A repeated opt-out or unchanged opt-in is one receipt.
        if (
            previous
            and previous.opted_in == opted_in
            and (not opted_in or previous.contact_version == version)
        ):
            return previous
        result = MarketingPreference.objects.create(
            organization_id=scope.organization_id,
            clinic_id=scope.clinic_id,
            enrollment_id=scope.enrollment_id,
            patient_session_id=scope.session_id,
            channel=channel,
            opted_in=opted_in,
            contact_version=version if opted_in else 0,
            revision=previous.revision + 1 if previous else 1,
        )
        _audit(
            "crm.preference.recorded",
            result.pk,
            scope.organization_id,
            scope.session_id,
            scope.clinic_id,
        )
        return result


def record_satisfaction(*, appointment_id: UUID, score: int) -> SatisfactionSurvey:
    """Record exactly one voluntary score for the patient's completed visit."""
    scope = require_patient_booking_scope()
    appointment = patient_appointment(appointment_id)
    if type(score) is not int or not 0 <= score <= MAX_SCORE:
        raise CrmRuleError(Reason.INVALID_SCORE)
    if appointment.status != "completed":
        raise CrmRuleError(Reason.COMPLETED_VISIT)
    with transaction.atomic():
        result, created = SatisfactionSurvey.objects.get_or_create(
            appointment=appointment,
            defaults={
                "organization_id": scope.organization_id,
                "clinic_id": scope.clinic_id,
                "patient_session_id": scope.session_id,
                "score": score,
            },
        )
        if not created and result.score != score:
            raise CrmRuleError(Reason.SCORE_RECORDED)
        if created:
            _audit(
                "crm.survey.recorded",
                result.pk,
                scope.organization_id,
                scope.session_id,
                scope.clinic_id,
            )
        return result
