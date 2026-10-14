from __future__ import annotations

import importlib
from typing import Final
from uuid import UUID

from django.utils import timezone

type EventContract = tuple[str, str, str, str]

CLINIC_ID: Final = UUID("11111111-1111-4111-8111-111111111111")
AFFECTED_ID: Final = UUID("22222222-2222-4222-8222-222222222222")
FIXED_MATRIX: Final[dict[str, EventContract]] = {
    "identity.delegate.requested": (
        "tenant",
        "clinic-os-web",
        "identity.patient_delegate_grant",
        "requested",
    ),
    "identity.delegate.verified": (
        "tenant",
        "clinic-os-web",
        "identity.patient_delegate_grant",
        "verified",
    ),
    "identity.delegate.rejected": (
        "tenant",
        "clinic-os-web",
        "identity.patient_delegate_grant",
        "rejected",
    ),
    "identity.delegate.activated": (
        "tenant",
        "clinic-os-web",
        "identity.patient_delegate_grant",
        "activated",
    ),
    "identity.delegate.revoked": (
        "tenant",
        "clinic-os-web",
        "identity.patient_delegate_grant",
        "revoked",
    ),
    "identity.delegate.session_switched": (
        "tenant",
        "clinic-os-web",
        "intake.patient_session",
        "session_switched",
    ),
    "intake.duplicate.detected": (
        "tenant",
        "clinic-os-web",
        "intake.duplicate_candidate",
        "detected",
    ),
    "intake.duplicate.merged": (
        "tenant",
        "clinic-os-web",
        "intake.merge_decision",
        "merged",
    ),
    "intake.duplicate.dismissed": (
        "tenant",
        "clinic-os-web",
        "intake.merge_decision",
        "dismissed",
    ),
    "intake.duplicate.unmerged": (
        "tenant",
        "clinic-os-web",
        "intake.patient_alias",
        "unmerged",
    ),
    "intake.duplicate.forward_transferred": (
        "tenant",
        "clinic-os-web",
        "intake.patient_merge_transfer",
        "transferred",
    ),
    "workflows.task.created": (
        "tenant",
        "clinic-os-web",
        "workflows.task",
        "created",
    ),
    "workflows.task.assigned": (
        "tenant",
        "clinic-os-web",
        "workflows.task",
        "assigned",
    ),
    "workflows.task.started": (
        "tenant",
        "clinic-os-web",
        "workflows.task",
        "started",
    ),
    "workflows.task.completed": (
        "tenant",
        "clinic-os-web",
        "workflows.task",
        "completed",
    ),
    "workflows.task.cancelled": (
        "tenant",
        "clinic-os-web",
        "workflows.task",
        "cancelled",
    ),
    "workflows.task.escalated": (
        "tenant",
        "clinic-os-worker",
        "workflows.task",
        "escalated",
    ),
    "workflows.comment.created": (
        "tenant",
        "clinic-os-web",
        "workflows.task_comment",
        "created",
    ),
    "workflows.definition.created": (
        "tenant",
        "clinic-os-web",
        "workflows.definition_version",
        "created",
    ),
    "workflows.definition.revised": (
        "tenant",
        "clinic-os-web",
        "workflows.definition_version",
        "revised",
    ),
    "workflows.definition.validated": (
        "tenant",
        "clinic-os-web",
        "workflows.definition_version",
        "validated",
    ),
    "workflows.definition.published": (
        "tenant",
        "clinic-os-web",
        "workflows.definition_version",
        "published",
    ),
    "workflows.definition.retired": (
        "tenant",
        "clinic-os-web",
        "workflows.definition_version",
        "retired",
    ),
    "workflows.run.started": (
        "tenant",
        "clinic-os-web",
        "workflows.run",
        "started",
    ),
    "workflows.run.cancelled": (
        "tenant",
        "clinic-os-web",
        "workflows.run",
        "cancelled",
    ),
    "workflows.run.retried": (
        "tenant",
        "clinic-os-web",
        "workflows.run",
        "retried",
    ),
    "workflows.run.running": (
        "tenant",
        "clinic-os-worker",
        "workflows.run",
        "running",
    ),
    "workflows.run.waiting": (
        "tenant",
        "clinic-os-worker",
        "workflows.run",
        "waiting",
    ),
    "workflows.run.completed": (
        "tenant",
        "clinic-os-worker",
        "workflows.run",
        "completed",
    ),
    "workflows.run.failed": (
        "tenant",
        "clinic-os-worker",
        "workflows.run",
        "failed",
    ),
    "workflows.step.claimed": (
        "tenant",
        "clinic-os-worker",
        "workflows.step",
        "claimed",
    ),
    "workflows.step.waiting": (
        "tenant",
        "clinic-os-worker",
        "workflows.step",
        "waiting",
    ),
    "workflows.step.completed": (
        "tenant",
        "clinic-os-worker",
        "workflows.step",
        "completed",
    ),
    "workflows.step.failed": (
        "tenant",
        "clinic-os-worker",
        "workflows.step",
        "failed",
    ),
    "workflows.step.cancelled": (
        "tenant",
        "clinic-os-web",
        "workflows.step",
        "cancelled",
    ),
    "workflows.step.retried": (
        "tenant",
        "clinic-os-web",
        "workflows.step",
        "retried",
    ),
    "realtime.subscription.authorized": (
        "tenant",
        "clinic-os-web",
        "identity.clinic",
        "authorized",
    ),
    "identity.signup.requested": (
        "system",
        "clinic-os-web",
        "identity.signup_registration",
        "requested",
    ),
    "identity.signup.inspected": (
        "system",
        "clinic-os-ops",
        "identity.signup_registration",
        "inspected",
    ),
    "identity.principal.created": (
        "tenant",
        "clinic-os-ops",
        "identity.service_principal",
        "created",
    ),
    "identity.principal.revoked": (
        "tenant",
        "clinic-os-ops",
        "identity.service_principal",
        "revoked",
    ),
    "identity.principal_grant.created": (
        "tenant",
        "clinic-os-ops",
        "identity.service_principal_grant",
        "created",
    ),
    "identity.principal_grant.revoked": (
        "tenant",
        "clinic-os-ops",
        "identity.service_principal_grant",
        "revoked",
    ),
    "identity.saved_view.created": (
        "tenant",
        "clinic-os-web",
        "identity.saved_view",
        "created",
    ),
    "identity.saved_view.archived": (
        "tenant",
        "clinic-os-web",
        "identity.saved_view",
        "archived",
    ),
    "identity.role_grant.created": (
        "tenant",
        "clinic-os-ops",
        "identity.role_grant",
        "created",
    ),
    "identity.care_team.created": (
        "tenant",
        "clinic-os-ops",
        "identity.care_team_membership",
        "created",
    ),
    "identity.care_team.revoked": (
        "tenant",
        "clinic-os-ops",
        "identity.care_team_membership",
        "revoked",
    ),
    "identity.professional_registration.created": (
        "tenant",
        "clinic-os-ops",
        "identity.professional_registration",
        "created",
    ),
    "identity.professional_registration.revoked": (
        "tenant",
        "clinic-os-ops",
        "identity.professional_registration",
        "revoked",
    ),
    "identity.clinic_configuration.published": (
        "tenant",
        "clinic-os-web",
        "identity.clinic_configuration",
        "published",
    ),
    "ehr.template.published": (
        "tenant",
        "clinic-os-web",
        "ehr.specialty_template",
        "published",
    ),
    "consent.text.published": ("tenant", "clinic-os-web", "consent.text", "published"),
    "consent.accepted": ("tenant", "clinic-os-web", "consent.acceptance", "accepted"),
    "consent.refused": ("tenant", "clinic-os-web", "consent.refusal", "refused"),
    "consent.notice.published": (
        "tenant",
        "clinic-os-web",
        "consent.notice",
        "published",
    ),
    "consent.ai_disclosure.recorded": (
        "tenant",
        "clinic-os-web",
        "consent.ai_disclosure",
        "recorded",
    ),
    "consent.participant.acknowledged": (
        "tenant",
        "clinic-os-web",
        "consent.participant_acknowledgment",
        "acknowledged",
    ),
    "consent.revoked": ("tenant", "clinic-os-web", "consent.revocation", "revoked"),
    "consent.receipts.viewed": (
        "tenant",
        "clinic-os-web",
        "intake.patient_clinic_enrollment",
        "viewed",
    ),
    "ehr.history.saved": ("tenant", "clinic-os-web", "ehr.history_assessment", "saved"),
    "ehr.history.viewed": ("tenant", "clinic-os-web", "ehr.encounter", "viewed"),
    "ehr.encounter.opened": ("tenant", "clinic-os-web", "ehr.encounter", "opened"),
    "ehr.document.draft_created": (
        "tenant",
        "clinic-os-web",
        "ehr.document_version",
        "draft_created",
    ),
    "ehr.document.saved": ("tenant", "clinic-os-web", "ehr.document_version", "saved"),
    "ehr.record.viewed": ("tenant", "clinic-os-web", "ehr.document_version", "viewed"),
    "ehr.document.finalized": (
        "tenant",
        "clinic-os-web",
        "ehr.document_version",
        "finalized",
    ),
    "ehr.document.amended": (
        "tenant",
        "clinic-os-web",
        "ehr.document_version",
        "amended",
    ),
    "ehr.document.discarded": (
        "tenant",
        "clinic-os-web",
        "ehr.document_version",
        "discarded",
    ),
    "ehr.encounter.closed": ("tenant", "clinic-os-web", "ehr.encounter", "closed"),
    "ehr.access.denied": ("tenant", "clinic-os-web", "ehr.record", "denied"),
    "ehr.attachment.uploaded": (
        "tenant",
        "clinic-os-web",
        "ehr.clinical_attachment",
        "uploaded",
    ),
    "ehr.attachment.scanned": (
        "tenant",
        "clinic-os-web",
        "ehr.clinical_attachment",
        "scanned",
    ),
    "ehr.attachment.downloaded": (
        "tenant",
        "clinic-os-web",
        "ehr.clinical_attachment",
        "downloaded",
    ),
    "ops.clinic.bootstrapped": (
        "system",
        "clinic-os-ops",
        "identity.organization",
        "bootstrapped",
    ),
    "identity.staff.provisioned": (
        "tenant",
        "clinic-os-ops",
        "identity.user_clinic_role",
        "provisioned",
    ),
    "identity.staff_role.revoked": (
        "tenant",
        "clinic-os-ops",
        "identity.user_clinic_role",
        "revoked",
    ),
    "identity.staff.invited": (
        "tenant",
        "clinic-os-web",
        "identity.staff_invitation",
        "created",
    ),
    "identity.staff.invitation_accepted": (
        "tenant",
        "clinic-os-web",
        "identity.staff_invitation",
        "accepted",
    ),
    "identity.staff.invitation_expired": (
        "tenant",
        "clinic-os-web",
        "identity.staff_invitation",
        "expired",
    ),
    "identity.staff.invitation_revoked": (
        "tenant",
        "clinic-os-web",
        "identity.staff_invitation",
        "revoked",
    ),
    "identity.recovery.enrolled": (
        "tenant",
        "clinic-os-web",
        "identity.recovery_batch",
        "created",
    ),
    "identity.recovery.consumed": (
        "tenant",
        "clinic-os-web",
        "identity.recovery_code",
        "consumed",
    ),
    "identity.authenticator.reset_requested": (
        "tenant",
        "clinic-os-web",
        "identity.authenticator_reset",
        "requested",
    ),
    "identity.authenticator.reset_executed": (
        "tenant",
        "clinic-os-web",
        "identity.authenticator_reset",
        "executed",
    ),
    "identity.session.revoked": (
        "tenant",
        "clinic-os-web",
        "identity.session",
        "revoked",
    ),
    "identity.staff.offboarded": (
        "tenant",
        "clinic-os-web",
        "identity.user",
        "offboarded",
    ),
    "identity.clinic_timezone.changed": (
        "tenant",
        "clinic-os-ops",
        "identity.clinic",
        "timezone_changed",
    ),
    "intake.demographics.viewed": (
        "tenant",
        "clinic-os-web",
        "intake.patient_clinic_enrollment",
        "viewed",
    ),
    "intake.demographics.billing_viewed": (
        "tenant",
        "clinic-os-web",
        "intake.patient_clinic_enrollment",
        "viewed",
    ),
    "intake.demographics.saved": (
        "tenant",
        "clinic-os-web",
        "intake.patient_demographics",
        "saved",
    ),
    "intake.identifier.saved": (
        "tenant",
        "clinic-os-web",
        "intake.patient_identifier",
        "saved",
    ),
    "intake.identifier.retired": (
        "tenant",
        "clinic-os-web",
        "intake.patient_identifier",
        "retired",
    ),
    "intake.address.saved": (
        "tenant",
        "clinic-os-web",
        "intake.patient_address",
        "saved",
    ),
    "intake.address.retired": (
        "tenant",
        "clinic-os-web",
        "intake.patient_address",
        "retired",
    ),
    "intake.emergency_contact.saved": (
        "tenant",
        "clinic-os-web",
        "intake.emergency_contact",
        "saved",
    ),
    "intake.emergency_contact.retired": (
        "tenant",
        "clinic-os-web",
        "intake.emergency_contact",
        "retired",
    ),
    "intake.membership.saved": (
        "tenant",
        "clinic-os-web",
        "intake.insurance_membership",
        "saved",
    ),
    "intake.membership.retired": (
        "tenant",
        "clinic-os-web",
        "intake.insurance_membership",
        "retired",
    ),
    "intake.intake_policy.saved": (
        "tenant",
        "clinic-os-web",
        "intake.clinic_intake_policy",
        "saved",
    ),
    "intake.patient.created": (
        "tenant",
        "clinic-os-web",
        "intake.patient",
        "created",
    ),
    "intake.patient.searched": (
        "tenant",
        "clinic-os-web",
        "identity.clinic",
        "searched",
    ),
    "intake.contacts.viewed": (
        "tenant",
        "clinic-os-web",
        "intake.patient_clinic_enrollment",
        "viewed",
    ),
    "intake.contact.saved": (
        "tenant",
        "clinic-os-web",
        "intake.patient_contact",
        "saved",
    ),
    "intake.contact.verified": (
        "tenant",
        "clinic-os-web",
        "intake.patient_contact",
        "verified",
    ),
    "intake.preference.opted_in": (
        "tenant",
        "clinic-os-web",
        "intake.patient_channel_preference",
        "opted_in",
    ),
    "intake.preference.opted_out": (
        "tenant",
        "clinic-os-web",
        "intake.patient_channel_preference",
        "opted_out",
    ),
    "intake.patient_access.viewed": (
        "tenant",
        "clinic-os-web",
        "intake.patient_clinic_enrollment",
        "viewed",
    ),
    "intake.patient_access.issued": (
        "tenant",
        "clinic-os-web",
        "intake.patient_access_grant",
        "issued",
    ),
    "intake.patient_access.revoked": (
        "tenant",
        "clinic-os-web",
        "intake.patient_access_grant",
        "revoked",
    ),
    "scheduling.availability.created": (
        "tenant",
        "clinic-os-web",
        "scheduling.availability",
        "created",
    ),
    "scheduling.availability.retired": (
        "tenant",
        "clinic-os-web",
        "scheduling.availability",
        "retired",
    ),
    "scheduling.availability.viewed": (
        "tenant",
        "clinic-os-web",
        "identity.clinic",
        "viewed",
    ),
    "scheduling.booking.viewed": (
        "tenant",
        "clinic-os-web",
        "intake.patient_clinic_enrollment",
        "viewed",
    ),
    "scheduling.resource.created": (
        "tenant",
        "clinic-os-web",
        "scheduling.resource",
        "created",
    ),
    "scheduling.service.created": (
        "tenant",
        "clinic-os-web",
        "scheduling.service_type",
        "created",
    ),
    "scheduling.template.created": (
        "tenant",
        "clinic-os-web",
        "scheduling.availability_template",
        "created",
    ),
    "scheduling.closure.created": (
        "tenant",
        "clinic-os-web",
        "scheduling.closure",
        "created",
    ),
    "scheduling.definition.retired": (
        "tenant",
        "clinic-os-web",
        "scheduling.definition",
        "retired",
    ),
    "scheduling.appointment.created": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "created",
    ),
    "scheduling.appointment.rescheduled": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "rescheduled",
    ),
    "scheduling.appointment.cancelled": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "cancelled",
    ),
    "scheduling.appointment.requested": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "requested",
    ),
    "scheduling.appointment.held": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "held",
    ),
    "scheduling.appointment.expired": (
        "tenant",
        "clinic-os-worker",
        "scheduling.appointment",
        "expired",
    ),
    "scheduling.appointment.booked": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "booked",
    ),
    "scheduling.appointment.arrived": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "arrived",
    ),
    "scheduling.appointment.started": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "started",
    ),
    "scheduling.appointment.completed": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "completed",
    ),
    "scheduling.appointment.no_show": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "no_show",
    ),
    "scheduling.series.created": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment_series",
        "created",
    ),
    "scheduling.series.edited": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment_series",
        "edited",
    ),
    "scheduling.appointment.viewed": (
        "tenant",
        "clinic-os-web",
        "scheduling.appointment",
        "viewed",
    ),
    "scheduling.agenda.viewed": (
        "tenant",
        "clinic-os-web",
        "identity.clinic",
        "viewed",
    ),
    "teleconsult.session.created": (
        "tenant",
        "clinic-os-web",
        "teleconsult.session",
        "created",
    ),
    "teleconsult.session.started": (
        "tenant",
        "clinic-os-web",
        "teleconsult.session",
        "started",
    ),
    "teleconsult.session.ended": (
        "tenant",
        "clinic-os-web",
        "teleconsult.session",
        "ended",
    ),
    "teleconsult.access.denied": (
        "tenant",
        "clinic-os-web",
        "teleconsult.session",
        "denied",
    ),
    "providers.capability.proposed": (
        "system",
        "clinic-os-ops",
        "providers.capability_version",
        "proposed",
    ),
    "providers.capability.selected_in_plan": (
        "system",
        "clinic-os-ops",
        "providers.capability_version",
        "selected_in_plan",
    ),
    "providers.capability.approved": (
        "system",
        "clinic-os-ops",
        "providers.capability_version",
        "approved",
    ),
    "providers.capability.sandbox": (
        "system",
        "clinic-os-ops",
        "providers.capability_version",
        "sandbox",
    ),
    "providers.capability.production_authorized": (
        "system",
        "clinic-os-ops",
        "providers.capability_version",
        "production_authorized",
    ),
    "providers.capability.activated": (
        "system",
        "clinic-os-ops",
        "providers.capability_version",
        "activated",
    ),
    "providers.capability.degraded": (
        "system",
        "clinic-os-ops",
        "providers.capability_version",
        "degraded",
    ),
    "providers.capability.revoked": (
        "system",
        "clinic-os-ops",
        "providers.capability_version",
        "revoked",
    ),
}


def test_fixed_matrix_builds_only_clinic_metadata() -> None:
    audit_events = importlib.import_module("apps.audit.events")
    implemented = {
        event_type: (
            definition.chain,
            definition.component_id,
            definition.affected_record_type,
            definition.object_verb,
        )
        for event_type, definition in audit_events.PHASE1_AUDIT_EVENTS.items()
    }

    assert implemented == FIXED_MATRIX
    for event_type, expected in FIXED_MATRIX.items():
        earliest = timezone.now()
        append = audit_events.build_phase1_audit_event(
            event_type,
            clinic_id=CLINIC_ID,
            affected_record_id=AFFECTED_ID,
        )
        latest = timezone.now()

        assert append.chain == expected[0]
        assert append.event.event_type == event_type
        assert append.event.component_id == expected[1]
        assert append.event.component_ip is None
        assert append.event.affected_record_type == expected[2]
        assert append.event.affected_record_id == str(AFFECTED_ID)
        assert earliest <= append.event.occurred_at_utc <= latest
        assert append.payload == {
            "clinic_id": str(CLINIC_ID),
            "object_verb": expected[3],
        }
