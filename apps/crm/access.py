"""Payload-free exact-clinic manager authority."""

from enum import StrEnum
from uuid import UUID

from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
    require_permission,
)
from apps.identity.models import UserClinicRole


class CrmAccessDeniedError(CurrentActorError):
    """Unknown, foreign and unauthorized records have the same response."""

    def __init__(self) -> None:
        """Never include a record selector or caller content."""
        super().__init__("current actor unauthorized")


class Reason(StrEnum):
    """Closed content-free refusal vocabulary."""

    NEUTRAL_TEMPLATE = "neutral_approved_template_required"
    CLINICAL_TARGETING = "clinical_targeting_blocked"
    INVALID_CAMPAIGN = "invalid_campaign"
    STALE = "stale_revision"
    AUDIENCE_LIMIT = "audience_limit"
    ILLEGAL_TRANSITION = "illegal_transition"
    NO_AUDIENCE = "no_consented_audience"
    INVALID_SOURCE = "invalid_source"
    INVALID_LEAD = "invalid_lead"
    LEAD_UNAVAILABLE = "lead_unavailable"
    INVALID_PROFILE = "invalid_profile"
    INVALID_PREFERENCE = "invalid_preference"
    VERIFIED_CHANNEL = "verified_channel_required"
    INVALID_SCORE = "invalid_score"
    COMPLETED_VISIT = "completed_visit_required"
    SCORE_RECORDED = "score_already_recorded"
    INVALID_ACTION = "invalid_action"


class CrmRuleError(Exception):
    """One fixed refusal code; never reflect content."""

    def __init__(self, code: Reason) -> None:
        """Store a domain-owned code."""
        self.code = code
        super().__init__(code)


def manager(clinic_id: UUID) -> UUID:
    """Recheck the canonical marketing bundle and its current subtraction."""
    try:
        actor = require_current_actor_clinic_roles(
            clinic_id, (UserClinicRole.Role.CLINIC_MANAGER,)
        )
        require_permission("demographics.read", clinic_id=clinic_id)
    except CurrentActorError as error:
        raise CrmAccessDeniedError from error
    return actor
