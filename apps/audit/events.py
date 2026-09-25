"""Fixed Phase 1A metadata-only audit event vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from django.utils import timezone

from apps.audit.canonical import AuditEventInput

if TYPE_CHECKING:
    from collections.abc import Mapping

type AuditChain = Literal["tenant", "system"]


@dataclass(frozen=True, slots=True)
class Phase1AuditEventDefinition:
    """Immutable event fields shared by every append of one event type."""

    chain: AuditChain
    component_id: str
    affected_record_type: str
    object_verb: str


@dataclass(frozen=True, slots=True)
class Phase1AuditAppend:
    """Complete metadata-only arguments for one tenant or system append."""

    chain: AuditChain
    event: AuditEventInput
    payload: Mapping[str, str]


class Phase1AuditEventRejectedError(ValueError):
    """Reject values outside the fixed Phase 1A event contract."""

    def __init__(self) -> None:
        """Expose one stable message without reflecting caller data."""
        super().__init__("Phase 1A audit event input is invalid")


PHASE1_AUDIT_EVENTS: Final[Mapping[str, Phase1AuditEventDefinition]] = MappingProxyType(
    {
        "identity.role_grant.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-ops", "identity.role_grant", "created"
        ),
        "identity.care_team.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-ops", "identity.care_team_membership", "created"
        ),
        "identity.care_team.revoked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-ops", "identity.care_team_membership", "revoked"
        ),
        "identity.professional_registration.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-ops", "identity.professional_registration", "created"
        ),
        "identity.professional_registration.revoked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-ops", "identity.professional_registration", "revoked"
        ),
        "identity.clinic_configuration.published": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.clinic_configuration", "published"
        ),
        "ehr.template.published": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.specialty_template", "published"
        ),
        "consent.text.published": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.text", "published"
        ),
        "consent.accepted": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.acceptance", "accepted"
        ),
        "consent.revoked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.revocation", "revoked"
        ),
        "consent.refused": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.refusal", "refused"
        ),
        "consent.notice.published": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.notice", "published"
        ),
        "consent.ai_disclosure.recorded": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.ai_use_disclosure", "recorded"
        ),
        "consent.participant.acknowledged": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "consent.participant_acknowledgment",
            "acknowledged",
        ),
        "consent.receipts.viewed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "intake.patient_clinic_enrollment", "viewed"
        ),
        "ehr.history.saved": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.history_assessment", "saved"
        ),
        "ehr.history.viewed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.encounter", "viewed"
        ),
        "ehr.encounter.opened": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.encounter", "opened"
        ),
        "ehr.document.draft_created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.document_version", "draft_created"
        ),
        "ehr.document.saved": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.document_version", "saved"
        ),
        "ehr.record.viewed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.document_version", "viewed"
        ),
        "ehr.document.finalized": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.document_version", "finalized"
        ),
        "ehr.document.amended": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.document_version", "amended"
        ),
        "ehr.document.discarded": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.document_version", "discarded"
        ),
        "ehr.encounter.closed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.encounter", "closed"
        ),
        "ehr.access.denied": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.record", "denied"
        ),
        "ehr.attachment.uploaded": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.clinical_attachment", "uploaded"
        ),
        "ehr.attachment.scanned": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.clinical_attachment", "scanned"
        ),
        "ehr.attachment.downloaded": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.clinical_attachment", "downloaded"
        ),
        "ops.clinic.bootstrapped": Phase1AuditEventDefinition(
            "system",
            "clinic-os-ops",
            "identity.organization",
            "bootstrapped",
        ),
        "identity.staff.provisioned": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-ops",
            "identity.user_clinic_role",
            "provisioned",
        ),
        "identity.staff_role.revoked": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-ops",
            "identity.user_clinic_role",
            "revoked",
        ),
        "identity.clinic_timezone.changed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-ops",
            "identity.clinic",
            "timezone_changed",
        ),
        "intake.patient.created": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient",
            "created",
        ),
        "intake.patient.searched": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "identity.clinic",
            "searched",
        ),
        "intake.contacts.viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_clinic_enrollment",
            "viewed",
        ),
        "intake.contact.saved": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_contact",
            "saved",
        ),
        "intake.contact.verified": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_contact",
            "verified",
        ),
        "intake.preference.opted_in": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_channel_preference",
            "opted_in",
        ),
        "intake.preference.opted_out": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_channel_preference",
            "opted_out",
        ),
        "intake.patient_access.viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_clinic_enrollment",
            "viewed",
        ),
        "intake.patient_access.issued": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_access_grant",
            "issued",
        ),
        "intake.patient_access.revoked": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_access_grant",
            "revoked",
        ),
        "scheduling.availability.created": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.availability",
            "created",
        ),
        "scheduling.availability.retired": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.availability",
            "retired",
        ),
        "scheduling.availability.viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "identity.clinic",
            "viewed",
        ),
        "scheduling.booking.viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_clinic_enrollment",
            "viewed",
        ),
        "scheduling.appointment.created": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "created",
        ),
        "scheduling.appointment.rescheduled": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "rescheduled",
        ),
        "scheduling.appointment.cancelled": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "cancelled",
        ),
        "scheduling.appointment.viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "viewed",
        ),
        "scheduling.agenda.viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "identity.clinic",
            "viewed",
        ),
        "teleconsult.session.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "teleconsult.session", "created"
        ),
        "teleconsult.session.started": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "teleconsult.session", "started"
        ),
        "teleconsult.session.ended": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "teleconsult.session", "ended"
        ),
        "teleconsult.access.denied": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "teleconsult.session", "denied"
        ),
        "providers.capability.proposed": Phase1AuditEventDefinition(
            "system",
            "clinic-os-ops",
            "providers.capability_version",
            "proposed",
        ),
        "providers.capability.selected_in_plan": Phase1AuditEventDefinition(
            "system",
            "clinic-os-ops",
            "providers.capability_version",
            "selected_in_plan",
        ),
        "providers.capability.approved": Phase1AuditEventDefinition(
            "system",
            "clinic-os-ops",
            "providers.capability_version",
            "approved",
        ),
        "providers.capability.sandbox": Phase1AuditEventDefinition(
            "system",
            "clinic-os-ops",
            "providers.capability_version",
            "sandbox",
        ),
        "providers.capability.production_authorized": Phase1AuditEventDefinition(
            "system",
            "clinic-os-ops",
            "providers.capability_version",
            "production_authorized",
        ),
        "providers.capability.activated": Phase1AuditEventDefinition(
            "system",
            "clinic-os-ops",
            "providers.capability_version",
            "activated",
        ),
        "providers.capability.degraded": Phase1AuditEventDefinition(
            "system",
            "clinic-os-ops",
            "providers.capability_version",
            "degraded",
        ),
        "providers.capability.revoked": Phase1AuditEventDefinition(
            "system",
            "clinic-os-ops",
            "providers.capability_version",
            "revoked",
        ),
    }
)


def build_phase1_audit_event(
    event_type: str,
    *,
    clinic_id: UUID,
    affected_record_id: UUID,
) -> Phase1AuditAppend:
    """Build exact fields for one fixed event without domain or patient data."""
    definition = PHASE1_AUDIT_EVENTS.get(event_type)
    if (
        definition is None
        or type(clinic_id) is not UUID
        or type(affected_record_id) is not UUID
    ):
        raise Phase1AuditEventRejectedError
    return Phase1AuditAppend(
        chain=definition.chain,
        event=AuditEventInput(
            event_type=event_type,
            component_id=definition.component_id,
            component_ip=None,
            affected_record_type=definition.affected_record_type,
            affected_record_id=str(affected_record_id),
            occurred_at_utc=timezone.now(),
        ),
        payload={
            "clinic_id": str(clinic_id),
            "object_verb": definition.object_verb,
        },
    )


INTEGRATION_AUDIT_EVENTS: Final[Mapping[str, Phase1AuditEventDefinition]] = (
    MappingProxyType(
        {
            "comms.operation.enqueued": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-web",
                "comms.integration_operation",
                "enqueued",
            ),
            "comms.operation.succeeded": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker",
                "comms.integration_operation",
                "succeeded",
            ),
            "comms.operation.delivered": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker",
                "comms.integration_operation",
                "delivered",
            ),
            "comms.operation.failed": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker",
                "comms.integration_operation",
                "failed",
            ),
            "comms.operation.cancelled": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker",
                "comms.integration_operation",
                "cancelled",
            ),
            "billing.payment.settled": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker",
                "billing.payment_event",
                "settled",
            ),
            "billing.payment.recorded": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker",
                "billing.payment_event",
                "recorded",
            ),
            "billing.payment.flagged": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker",
                "billing.payment_event",
                "flagged",
            ),
        }
    )
)


def build_integration_audit_event(
    event_type: str,
    *,
    clinic_id: UUID,
    affected_record_id: UUID,
    operation_id: UUID,
    reason_code: str | None = None,
) -> Phase1AuditAppend:
    """Build one fixed integration event carrying only operation metadata."""
    definition = INTEGRATION_AUDIT_EVENTS.get(event_type)
    if (
        definition is None
        or type(clinic_id) is not UUID
        or type(affected_record_id) is not UUID
        or type(operation_id) is not UUID
        or (reason_code is not None and type(reason_code) is not str)
    ):
        raise Phase1AuditEventRejectedError
    payload = {
        "clinic_id": str(clinic_id),
        "object_verb": definition.object_verb,
        "request_id": str(operation_id),
    }
    if reason_code is not None:
        payload["reason_code"] = reason_code
    return Phase1AuditAppend(
        chain=definition.chain,
        event=AuditEventInput(
            event_type=event_type,
            component_id=definition.component_id,
            component_ip=None,
            affected_record_type=definition.affected_record_type,
            affected_record_id=str(affected_record_id),
            occurred_at_utc=timezone.now(),
        ),
        payload=payload,
    )
