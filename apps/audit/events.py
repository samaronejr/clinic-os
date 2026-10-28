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
        **{
            f"retention.purge.{state}": Phase1AuditEventDefinition(
                "tenant", "clinic-os-worker", "retention.artifact_purge", state
            )
            for state in ("purge_due", "blocked_hold", "purging", "purged", "failure")
        },
        "prescription.issuance.rehearsed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "prescription.issuance", "rehearsed"
        ),
        "prescription.signature.signed_unverified": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "prescription.signature", "signed_unverified"
        ),
        "prescription.signature.verification_completed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "prescription.signature",
            "verification_completed",
        ),
        "prescription.signature.verification_failed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "prescription.signature", "verification_failed"
        ),
        "prescription.clinical_draft.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "prescription.clinical_draft", "created"
        ),
        "prescription.clinical_draft.saved": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "prescription.clinical_draft", "saved"
        ),
        "prescription.clinical_draft.discarded": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "prescription.clinical_draft", "discarded"
        ),
        "prescription.favorite.added": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "prescription.favorite", "added"
        ),
        "prescription.favorite.removed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "prescription.favorite", "removed"
        ),
        "prescription.draft.renewed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "prescription.draft", "renewed"
        ),
        "scheduling.queue.arrived": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.queue", "arrived"
        ),
        "scheduling.queue.called": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.queue", "called"
        ),
        "scheduling.queue.progressed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.queue", "progressed"
        ),
        "scheduling.room.updated": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.room", "updated"
        ),
        "scheduling.delay.updated": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.delay", "updated"
        ),
        "scheduling.display.issued": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.display", "issued"
        ),
        "scheduling.display.revoked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.display", "revoked"
        ),
        "ehr.observation.recorded": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.observation", "recorded"
        ),
        "ehr.observation.confirmed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.observation", "confirmed"
        ),
        "ehr.medication.recorded": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.medication_statement", "recorded"
        ),
        "ehr.medication.reconciled": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.medication_statement", "reconciled"
        ),
        "ehr.note.restricted": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.restricted_note", "restricted"
        ),
        "ehr.note.granted": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.note_access_grant", "granted"
        ),
        "ehr.note.revoked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.note_access_grant", "revoked"
        ),
        "ehr.prior_note.copied": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.copied_note_block", "copied"
        ),
        "ehr.prior_note.reviewed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.copied_note_block", "reviewed"
        ),
        "ehr.timeline.viewed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.timeline", "viewed"
        ),
        **{
            f"ai.action.{state}": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker",
                "ai.action_proposal",
                state,
            )
            for state in (
                "proposal",
                "validated",
                "rejected",
                "approved",
                "executing",
                "receipt",
                "unknown",
                "reconciled",
                "exception",
                "failure",
                "compensating",
                "compensated",
                "manual_required",
            )
        },
        **{
            f"ai.execution.{state}": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker",
                "ai.action_execution",
                state,
            )
            for state in (
                "executing",
                "sending",
                "receipt",
                "unknown",
                "reconciled",
                "exception",
                "failure",
                "rejected",
                "compensating",
                "compensated",
                "manual_required",
            )
        },
        **{
            f"ai.agent_run.{state}": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker",
                "ai.agent_run",
                state,
            )
            for state in (
                "created",
                "validated",
                "rejected",
                "approved",
                "executing",
                "receipt",
                "unknown",
                "reconciled",
                "exception",
                "failure",
                "compensating",
                "compensated",
                "manual_required",
            )
        },
        **{
            f"ai.compensation.{state}": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker",
                "ai.compensation",
                state,
            )
            for state in ("compensating", "compensated", "manual_required")
        },
        "ai.agent_policy.published": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-worker",
            "ai.agent_policy_version",
            "published",
        ),
        "ai.approval.recorded": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-worker",
            "ai.approval",
            "recorded",
        ),
        "ai.receipt.recorded": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-worker",
            "ai.action_receipt",
            "recorded",
        ),
        **{
            f"scribe.session.{state}": Phase1AuditEventDefinition(
                "tenant", "clinic-os-web", "scribe.recording_session", state
            )
            for state in (
                "created",
                "authorized",
                "recording",
                "paused",
                "stopped",
                "stopped_revoked",
            )
        },
        **{
            f"scribe.epoch.{state}": Phase1AuditEventDefinition(
                "tenant", "clinic-os-web", "scribe.capture_epoch", state
            )
            for state in ("created", "closed")
        },
        **{
            f"scribe.chunk.{state}": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker" if state == "accepted" else "clinic-os-web",
                "scribe.audio_chunk",
                state,
            )
            for state in ("received", "accepted", "quarantined", "rejected")
        },
        "scribe.transcription.queued": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "scribe.transcription", "queued"
        ),
        **{
            f"scribe.transcript.{mutation}": Phase1AuditEventDefinition(
                "tenant",
                "clinic-os-worker" if mutation == "recognized" else "clinic-os-web",
                "scribe.transcript",
                mutation,
            )
            for mutation in (
                "recognized",
                "finalized",
                "speaker_relabeled",
                "corrected",
            )
        },
        "ai.model.drafted": Phase1AuditEventDefinition(
            "system", "clinic-os-ops", "ai.model_configuration", "drafted"
        ),
        "ai.model.approved": Phase1AuditEventDefinition(
            "system", "clinic-os-ops", "ai.model_configuration", "approved"
        ),
        "ai.model.active": Phase1AuditEventDefinition(
            "system", "clinic-os-ops", "ai.model_configuration", "active"
        ),
        "ai.model.retired": Phase1AuditEventDefinition(
            "system", "clinic-os-ops", "ai.model_configuration", "retired"
        ),
        "ai.global_switch.changed": Phase1AuditEventDefinition(
            "system", "clinic-os-ops", "ai.gateway_switch", "changed"
        ),
        "ai.organization_policy.published": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.clinic_configuration", "published"
        ),
        "ai.artifact.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "ai.artifact", "created"
        ),
        **{
            f"ai.decision_support.{decision}": Phase1AuditEventDefinition(
                "tenant", "clinic-os-web", "ai.decision_support", decision
            )
            for decision in ("accepted", "modified", "rejected")
        },
        "ai.extraction.started": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ai.extraction_job", "started"
        ),
        "ai.extraction.proposed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "ai.extraction_candidate", "proposed"
        ),
        "ai.extraction.confirmed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ai.extraction_candidate", "confirmed"
        ),
        "ai.extraction.rejected": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ai.extraction_candidate", "rejected"
        ),
        "ai.invocation.reserved": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "ai.invocation", "reserved"
        ),
        "ai.invocation.succeeded": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "ai.invocation", "succeeded"
        ),
        "ai.invocation.failed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "ai.invocation", "failed"
        ),
        "ai.circuit.opened": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "ai.circuit", "opened"
        ),
        "ai.circuit.closed": Phase1AuditEventDefinition(
            "system", "clinic-os-ops", "ai.circuit", "closed"
        ),
        "identity.delegate.requested": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.patient_delegate_grant", "requested"
        ),
        "identity.delegate.verified": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.patient_delegate_grant", "verified"
        ),
        "identity.delegate.rejected": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.patient_delegate_grant", "rejected"
        ),
        "identity.delegate.activated": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.patient_delegate_grant", "activated"
        ),
        "identity.delegate.revoked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.patient_delegate_grant", "revoked"
        ),
        "identity.delegate.session_switched": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "intake.patient_session", "session_switched"
        ),
        "intake.duplicate.detected": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "intake.duplicate_candidate", "detected"
        ),
        "intake.duplicate.merged": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "intake.merge_decision", "merged"
        ),
        "intake.duplicate.dismissed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "intake.merge_decision", "dismissed"
        ),
        "intake.duplicate.unmerged": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "intake.patient_alias", "unmerged"
        ),
        "intake.duplicate.forward_transferred": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "intake.patient_merge_transfer", "transferred"
        ),
        "identity.passkey.registered": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.passkey", "created"
        ),
        "identity.passkey.verified": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.passkey", "verified"
        ),
        "identity.passkey.counter_regression": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.passkey", "denied"
        ),
        "identity.passkey.removed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.passkey", "revoked"
        ),
        "identity.passkey.policy_changed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.organization", "policy_changed"
        ),
        "workflows.task.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.task", "created"
        ),
        "workflows.task.assigned": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.task", "assigned"
        ),
        "workflows.task.started": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.task", "started"
        ),
        "workflows.task.completed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.task", "completed"
        ),
        "workflows.task.cancelled": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.task", "cancelled"
        ),
        "workflows.task.escalated": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "workflows.task", "escalated"
        ),
        "workflows.comment.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.task_comment", "created"
        ),
        "workflows.definition.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.definition_version", "created"
        ),
        "workflows.definition.revised": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.definition_version", "revised"
        ),
        "workflows.definition.validated": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.definition_version", "validated"
        ),
        "workflows.definition.published": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.definition_version", "published"
        ),
        "workflows.definition.retired": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.definition_version", "retired"
        ),
        "workflows.run.started": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.run", "started"
        ),
        "workflows.run.running": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "workflows.run", "running"
        ),
        "workflows.run.waiting": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "workflows.run", "waiting"
        ),
        "workflows.run.completed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "workflows.run", "completed"
        ),
        "workflows.run.failed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "workflows.run", "failed"
        ),
        "workflows.run.cancelled": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.run", "cancelled"
        ),
        "workflows.run.retried": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.run", "retried"
        ),
        "workflows.step.claimed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "workflows.step", "claimed"
        ),
        "workflows.step.waiting": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "workflows.step", "waiting"
        ),
        "workflows.step.completed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "workflows.step", "completed"
        ),
        "workflows.step.failed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-worker", "workflows.step", "failed"
        ),
        "workflows.step.cancelled": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.step", "cancelled"
        ),
        "workflows.step.retried": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.step", "retried"
        ),
        "realtime.subscription.authorized": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.clinic", "authorized"
        ),
        "identity.signup.requested": Phase1AuditEventDefinition(
            "system", "clinic-os-web", "identity.signup_registration", "requested"
        ),
        "identity.signup.inspected": Phase1AuditEventDefinition(
            "system", "clinic-os-ops", "identity.signup_registration", "inspected"
        ),
        "identity.principal.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-ops", "identity.service_principal", "created"
        ),
        "identity.principal.revoked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-ops", "identity.service_principal", "revoked"
        ),
        "identity.principal_grant.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-ops", "identity.service_principal_grant", "created"
        ),
        "identity.principal_grant.revoked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-ops", "identity.service_principal_grant", "revoked"
        ),
        "identity.saved_view.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.saved_view", "created"
        ),
        "identity.saved_view.archived": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.saved_view", "archived"
        ),
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
        "ehr.template.draft_created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.specialty_template", "draft_created"
        ),
        "ehr.template.retired": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.specialty_template", "retired"
        ),
        "intake.questionnaire_template.draft_created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "intake.questionnaire_template", "draft_created"
        ),
        "intake.questionnaire_template.published": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "intake.questionnaire_template", "published"
        ),
        "intake.questionnaire_template.retired": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "intake.questionnaire_template", "retired"
        ),
        "consent.text.published": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.text", "published"
        ),
        "consent.accepted": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.acceptance", "accepted"
        ),
        "consent.refused": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.refusal", "refused"
        ),
        "consent.notice.published": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.notice", "published"
        ),
        "consent.ai_disclosure.recorded": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.ai_disclosure", "recorded"
        ),
        "consent.participant.acknowledged": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "consent.participant_acknowledgment",
            "acknowledged",
        ),
        "consent.revoked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "consent.revocation", "revoked"
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
        "ehr.encounter.opened_unscheduled": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.encounter", "opened_unscheduled"
        ),
        "ehr.episode.opened": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.episode", "opened"
        ),
        "ehr.episode.closed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.episode", "closed"
        ),
        "ehr.episode.linked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.episode_encounter", "linked"
        ),
        "ehr.draft.handed_over": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.document_version", "handed_over"
        ),
        "ehr.addendum.opened": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.encounter_addendum", "opened"
        ),
        "ehr.addendum.saved": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.encounter_addendum", "saved"
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
        "identity.staff.invited": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.staff_invitation", "created"
        ),
        "identity.staff.invitation_accepted": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.staff_invitation", "accepted"
        ),
        "identity.staff.invitation_expired": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.staff_invitation", "expired"
        ),
        "identity.staff.invitation_revoked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.staff_invitation", "revoked"
        ),
        "identity.recovery.enrolled": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.recovery_batch", "created"
        ),
        "identity.recovery.consumed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.recovery_code", "consumed"
        ),
        "identity.authenticator.reset_requested": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.authenticator_reset", "requested"
        ),
        "identity.authenticator.reset_executed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.authenticator_reset", "executed"
        ),
        "identity.session.revoked": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.session", "revoked"
        ),
        "identity.staff.offboarded": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "identity.user", "offboarded"
        ),
        "identity.clinic_timezone.changed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-ops",
            "identity.clinic",
            "timezone_changed",
        ),
        "intake.demographics.viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_clinic_enrollment",
            "viewed",
        ),
        "intake.demographics.billing_viewed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_clinic_enrollment",
            "viewed",
        ),
        "intake.demographics.saved": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_demographics",
            "saved",
        ),
        "intake.identifier.saved": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_identifier",
            "saved",
        ),
        "intake.identifier.retired": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_identifier",
            "retired",
        ),
        "intake.address.saved": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_address",
            "saved",
        ),
        "intake.address.retired": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.patient_address",
            "retired",
        ),
        "intake.emergency_contact.saved": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.emergency_contact",
            "saved",
        ),
        "intake.emergency_contact.retired": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.emergency_contact",
            "retired",
        ),
        "intake.membership.saved": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.insurance_membership",
            "saved",
        ),
        "intake.membership.retired": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.insurance_membership",
            "retired",
        ),
        "intake.intake_policy.saved": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "intake.clinic_intake_policy",
            "saved",
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
        "scheduling.resource.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.resource", "created"
        ),
        "scheduling.service.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.service_type", "created"
        ),
        "scheduling.template.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.availability_template", "created"
        ),
        "scheduling.closure.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.closure", "created"
        ),
        "scheduling.definition.retired": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.definition", "retired"
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
        "scheduling.appointment.requested": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "requested",
        ),
        "scheduling.appointment.held": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "held",
        ),
        "scheduling.appointment.expired": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-worker",
            "scheduling.appointment",
            "expired",
        ),
        "scheduling.appointment.booked": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "booked",
        ),
        "scheduling.appointment.arrived": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "arrived",
        ),
        "scheduling.appointment.started": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "started",
        ),
        "scheduling.appointment.completed": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "completed",
        ),
        "scheduling.appointment.no_show": Phase1AuditEventDefinition(
            "tenant",
            "clinic-os-web",
            "scheduling.appointment",
            "no_show",
        ),
        "scheduling.series.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.appointment_series", "created"
        ),
        "scheduling.series.edited": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "scheduling.appointment_series", "edited"
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
        "teleconsult.participant.removed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "teleconsult.session", "removed"
        ),
        "teleconsult.audio_only.enabled": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "teleconsult.session", "audio_only"
        ),
        "teleconsult.audio_only.disabled": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "teleconsult.session", "video_restored"
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
        # Plan item 30: specialty bundle and retained clinical entry transitions.
        "ehr.pack.installed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.specialty_pack", "installed"
        ),
        "ehr.pack.uninstalled": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.specialty_pack", "uninstalled"
        ),
        "ehr.specialty.recorded": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.specialty_entry", "recorded"
        ),
        "ehr.pack.reordered": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "ehr.specialty_pack", "reordered"
        ),
        # Task 31: keep this additive registry block together for lane rebases.
        "orders.order.draft": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "orders.order", "draft"
        ),
        "orders.order.placed": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "orders.order", "placed"
        ),
        "orders.order.collected": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "orders.order", "collected"
        ),
        "orders.order.in_process": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "orders.order", "in_process"
        ),
        "orders.result.ingested": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "orders.result_version", "ingested"
        ),
        "orders.result.matched": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "orders.result_version", "matched"
        ),
        "orders.result.quarantined": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "orders.result_version", "quarantined"
        ),
        "orders.result.rejected": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "orders.result_version", "rejected"
        ),
        "orders.result.acknowledged": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "orders.result_version", "acknowledged"
        ),
        "orders.result.released": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "orders.result_version", "released"
        ),
        "orders.task.created": Phase1AuditEventDefinition(
            "tenant", "clinic-os-web", "workflows.task", "created"
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
