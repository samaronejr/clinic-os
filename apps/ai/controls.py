"""Organization opt-in changes only policy, never approved provider terms."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection, transaction

from apps.ai.contracts import GatewayAccessDeniedError, GatewayInputError
from apps.ai.policy import OrganizationPolicy, organization_policy
from apps.audit.services import record_phase1_event
from apps.core.integration import hold_subject_mutation_key
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.current_context import require_permission
from apps.identity.models import Clinic, ClinicConfiguration

if TYPE_CHECKING:
    from uuid import UUID


@transaction.atomic
def set_organization_policy(
    *, clinic_id: UUID, expected_version: int, policy: OrganizationPolicy
) -> ClinicConfiguration:
    """Publish an AI-only snapshot with organization-wide, remove-only authority."""
    user_id = require_permission("configuration.organization", clinic_id=clinic_id)
    clinic = Clinic.objects.get(pk=clinic_id)
    for other in Clinic.objects.filter(organization_id=clinic.organization_id):
        require_permission("configuration.organization", clinic_id=other.pk)
    hold_subject_mutation_key(f"ai-organization:{clinic.organization_id}")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0)), "
            "pg_advisory_xact_lock(hashtextextended(%s,0))",
            [
                f"ai-budget:{clinic.organization_id}",
                f"clinic-configuration:{clinic_id}",
            ],
        )
    current = latest_configuration(clinic_id)
    if current is None:
        raise GatewayAccessDeniedError
    if type(expected_version) is not int or current.version != expected_version:
        raise GatewayInputError
    snapshot = ClinicConfiguration.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic_id,
        version=current.version + 1,
        display_name=current.display_name,
        contact_email=current.contact_email,
        contact_phone=current.contact_phone,
        brand_token=current.brand_token,
        reminder_hours=current.reminder_hours,
        queue_quotas=current.queue_quotas,
        self_booking_requires_approval=current.self_booking_requires_approval,
        hold_ttl_minutes=current.hold_ttl_minutes,
        logo_png=current.logo_png,
        ai_policy=policy.as_json(),
        published_by_id=user_id,
    )
    record_phase1_event(
        "ai.organization_policy.published",
        clinic_id=clinic_id,
        affected_record_id=snapshot.pk,
    )
    return snapshot


def current_policy(*, clinic_id: UUID) -> OrganizationPolicy:
    """Read org controls without revealing another user's artifacts."""
    require_permission("configuration.organization", clinic_id=clinic_id)
    return organization_policy()
