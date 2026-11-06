"""Administrative lead capture, bounded redaction and aggregate reporting."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.crm.access import CrmAccessDeniedError, CrmRuleError, Reason, manager
from apps.crm.models import (
    Lead,
    PublicBookingProfile,
    ReferralSource,
    SatisfactionSurvey,
)
from apps.crm.rules import MAX_DESCRIPTION, MAX_EMAIL, MAX_NAME
from apps.identity.models import Clinic
from apps.identity.overlay_content import validate_overlay_text
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from uuid import UUID

LEAD_RETENTION_DAYS = 90


def create_source(*, clinic_id: UUID, code: str, name: str) -> ReferralSource:
    """Capture an administrative referral label without a patient relationship."""
    manager(clinic_id)
    if (
        not re.fullmatch(r"[a-z0-9_-]{1,40}", code)
        or not 0 < len(name.strip()) <= MAX_NAME
    ):
        raise CrmRuleError(Reason.INVALID_SOURCE)
    validate_overlay_text(name)
    if ReferralSource.objects.filter(clinic_id=clinic_id, code=code).exists():
        raise CrmRuleError(Reason.INVALID_SOURCE)
    with transaction.atomic():
        result = ReferralSource.objects.create(
            organization_id=Clinic.objects.get(pk=clinic_id).organization_id,
            clinic_id=clinic_id,
            code=code,
            name=name.strip(),
        )
        record_phase1_event(
            "crm.source.created", clinic_id=clinic_id, affected_record_id=result.pk
        )
        return result


def create_lead(
    *, clinic_id: UUID, name: str, email: str, source_id: UUID | None = None
) -> Lead:
    """Capture a prospect; this grants no marketing permission and no care record."""
    manager(clinic_id)
    if not isinstance(name, str) or not 0 < len(name.strip()) <= MAX_NAME:
        raise CrmRuleError(Reason.INVALID_LEAD)
    try:
        validate_email(email)
        validate_overlay_text(name)
    except ValidationError as error:
        raise CrmRuleError(Reason.INVALID_LEAD) from error
    if len(email) > MAX_EMAIL:
        raise CrmRuleError(Reason.INVALID_LEAD)
    if (
        source_id
        and not ReferralSource.objects.filter(
            pk=source_id, clinic_id=clinic_id
        ).exists()
    ):
        raise CrmAccessDeniedError
    with transaction.atomic():
        result = Lead.objects.create(
            organization_id=Clinic.objects.get(pk=clinic_id).organization_id,
            clinic_id=clinic_id,
            name=name.strip(),
            email=email,
            source_id=source_id,
            expires_at=timezone.now() + timedelta(days=LEAD_RETENTION_DAYS),
        )
        record_phase1_event(
            "crm.lead.created", clinic_id=clinic_id, affected_record_id=result.pk
        )
        return result


def convert_lead(*, clinic_id: UUID, lead_id: UUID, enrollment_id: UUID) -> Lead:
    """Record the manager's explicit mapping, never infer identity from contact."""
    manager(clinic_id)
    with transaction.atomic():
        lead = (
            Lead.objects.select_for_update()
            .filter(pk=lead_id, clinic_id=clinic_id)
            .first()
        )
        enrollment = PatientClinicEnrollment.objects.filter(
            pk=enrollment_id, clinic_id=clinic_id
        ).first()
        if lead is None or enrollment is None:
            raise CrmAccessDeniedError
        if lead.state != "active" or lead.expires_at <= timezone.now():
            raise CrmRuleError(Reason.LEAD_UNAVAILABLE)
        lead.state = "converted"
        lead.enrollment = enrollment
        lead.save(update_fields=["state", "enrollment"])
        record_phase1_event(
            "crm.lead.converted", clinic_id=clinic_id, affected_record_id=lead.pk
        )
        return lead


def redact_expired_leads(*, clinic_id: UUID) -> int:
    """Remove contact and enrollment at the prospect's separate deadline."""
    manager(clinic_id)
    with transaction.atomic():
        rows = (
            Lead.objects.select_for_update()
            .filter(clinic_id=clinic_id, expires_at__lte=timezone.now())
            .exclude(state="redacted")
        )
        count = 0
        for lead in rows:
            lead.name = None
            lead.email = None
            lead.enrollment_id = None
            lead.state = "redacted"
            lead.save(update_fields=["name", "email", "enrollment", "state"])
            record_phase1_event(
                "crm.lead.redacted", clinic_id=clinic_id, affected_record_id=lead.pk
            )
            count += 1
        return count


def conversion_report(*, clinic_id: UUID) -> dict[str, int]:
    """Return counts, denominators and score counts; no clinical dimensions."""
    manager(clinic_id)
    leads = Lead.objects.filter(clinic_id=clinic_id).exclude(state="redacted")
    surveys = SatisfactionSurvey.objects.filter(clinic_id=clinic_id)
    return {
        "leads": leads.count(),
        "converted": leads.filter(state="converted").count(),
        "surveys": surveys.count(),
        "promoters": surveys.filter(score__gte=9).count(),
        "detractors": surveys.filter(score__lte=6).count(),
    }


def publish_booking_profile(
    *, clinic_id: UUID, code: str, description: str
) -> PublicBookingProfile:
    """Publish real clinic copy; a public code is a body-only selector."""
    manager(clinic_id)
    if (
        not re.fullmatch(r"[a-z0-9_-]{1,40}", code)
        or not 0 < len(description.strip()) <= MAX_DESCRIPTION
    ):
        raise CrmRuleError(Reason.INVALID_PROFILE)
    validate_overlay_text(description)
    with transaction.atomic():
        result, created = PublicBookingProfile.objects.get_or_create(
            clinic_id=clinic_id,
            defaults={
                "organization_id": Clinic.objects.get(pk=clinic_id).organization_id,
                "code": code,
                "description": description.strip(),
            },
        )
        if not created:
            result = PublicBookingProfile.objects.select_for_update().get(pk=result.pk)
            if result.code != code:
                raise CrmRuleError(Reason.INVALID_PROFILE)
            if result.description == description.strip() and result.active:
                return result
            result.description = description.strip()
            result.active = True
            result.save(update_fields=["description", "active"])
        record_phase1_event(
            "crm.profile.published", clinic_id=clinic_id, affected_record_id=result.pk
        )
        return result
