"""Representative synthetic record graphs for every domain added after the base.

``migration_upgrade_stages`` loads each entry right after the migration that
first brings its model into the migration state, through the *historical*
model of that state, inside the witness tenant whose base fixture already
holds a patient, encounter, finalized SOAP, prescription, signature,
attachment, invoice, consent and teleconsult graph. An entry lists only the
values that make one coherent, representative row graph (state-machine
states, provenance and unknown values, encrypted content, cross-domain
links); every other column is filled generically from the historical field.
Every model the candidate adds needs an explicit entry here: a model without
one is reported unseeded and fails the gate.

All values are synthetic: "Synthetic ..." names, ``*.invalid`` mail, TEST-NET
addresses. Nothing here is real patient, clinic or provider data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Final
from uuid import uuid4, uuid5

from django.db.models.expressions import RawSQL

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from uuid import UUID

Row = dict[str, object]
DIGEST: Final = "a" * 64
BYTES32: Final = bytes(range(32))


@dataclass(frozen=True, slots=True)
class SeedContext:
    """Identities and already-seeded rows visible to one stage."""

    organization: UUID
    clinic: UUID
    owner: UUID
    physician: UUID
    patient: UUID
    enrollment: UUID
    companion_patient: UUID
    companion_enrollment: UUID
    now: datetime
    seeded: Mapping[str, Sequence[object]]
    legacy: Callable[[str, Mapping[str, object]], object]
    fetch: Callable[[str, str, Mapping[str, object]], object]

    def value(self, label: str, name: str, **filters: object) -> object:
        """One stored column of the base row ``ref(label, **filters)`` names."""
        return self.fetch(label, name, filters)

    def key(self, name: str) -> UUID:
        """Return a stable synthetic id, so one row can name another first."""
        return uuid5(self.organization, name)

    @property
    def future(self) -> datetime:
        """A month after the stage time."""
        return self.now + timedelta(days=30)

    def ref(self, label: str, index: int = 0, **filters: object) -> object:
        """Primary key of a seeded new-domain row, else of a base row."""
        rows = self.seeded.get(label)
        if rows and not filters:
            return rows[index]
        return self.legacy(label, filters)


def _one(_context: SeedContext) -> Sequence[Row]:
    return ({},)


@dataclass(frozen=True, slots=True)
class Seed:
    """How one model's representative rows are loaded.

    ``rows`` returns per-row overrides; ``actor`` selects the
    ``app.current_user_id`` identity; ``adopt`` names why the model's first
    rows come from its own creating migration's backfill, which the stage
    records instead of inserting a conflicting row; ``support`` loads rows of
    already-existing models the graph needs first (an extra staff role, a
    recovery device) through the same historical state; ``after`` names
    same-stage models whose rows a value refers to without a foreign key, so
    they load first; ``updates`` then moves an earlier row through a product
    transition (``TRANSITION_GUARDS`` lists any session guard suspended for
    it) and re-records the row.

    To seed a model a later migration adds, add one entry under its domain;
    if its insert guard authorizes against a live session, name that one
    BEFORE ROW INSERT trigger in ``GUARDED_TRIGGERS`` and, when the guard
    encodes a cross-row rule, add the rule to ``LEAF_INVARIANTS``.
    """

    rows: Callable[[SeedContext], Sequence[Row]] = field(default=_one)
    actor: str = "owner"
    adopt: str = ""
    support: tuple[tuple[str, Seed], ...] = ()
    after: tuple[str, ...] = ()
    updates: tuple[tuple[str, Callable[[SeedContext], tuple[object, Row]]], ...] = ()


def _rows(*rows: Row) -> Callable[[SeedContext], Sequence[Row]]:
    return lambda _context: rows


def _owner_role(role: str) -> Callable[[SeedContext], Sequence[Row]]:
    return lambda c: ({"user_id": c.owner, "role": role},)


def _reviewer(name: str) -> Callable[[SeedContext], Sequence[Row]]:
    return lambda c: (
        {
            "id": c.key(name),
            "username": f"upgrade-{name}-{c.key(name).hex[:8]}",
            "password": "!",
            "is_active": True,
        },
    )


def _reviewer_role(name: str) -> Callable[[SeedContext], Sequence[Row]]:
    return lambda c: ({"user_id": c.key(name), "role": "physician"},)


TRANSACTION_TIME: Final = RawSQL("transaction_timestamp()", ())


def _finalized(c: SeedContext, name: str) -> object:
    """Return a column of the witness's finalized SOAP version."""
    return c.value("ehr.ClinicalDocumentVersion", name, state="finalized")


# The first reviewed neutral marketing body (apps/crm/rules.py).
MARKETING_BODY: Final = (
    "A clinica tem horarios disponiveis. Consulte a agenda no portal. "
    "Voce pode desativar mensagens de divulgacao nas preferencias do portal."
)


# The named BEFORE ROW INSERT guards suspended for each model's fixture load.
# Each one authorizes the insert against live request state (a verified
# authenticator factor, a patient or delegate session, a provider callback,
# a reserved AI call, a service command) that an owner load cannot present.
# Nothing else is suspended: AFTER triggers, deferred constraint triggers,
# UPDATE-only guards, CHECK/UNIQUE/FK/NOT NULL and RLS all stay active, the
# stage refuses to commit if any other trigger is disabled, and the leaf
# verifier runs LEAF_INVARIANTS over the seeded graph. Every other model
# loads through all of its live triggers.
GUARDED_TRIGGERS: Final[dict[str, tuple[str, ...]]] = {
    "ai.AIArtifact": ("ai_artifact_guard",),
    "ai.AICircuit": ("ai_circuit_guard",),
    "ai.AIInvocation": ("ai_invocation_guard",),
    "ai.ActionExecution": ("agent_record_guard",),
    "ai.ActionProposal": ("agent_record_guard",),
    "ai.AgentRun": ("agent_record_guard",),
    "ai.Approval": ("agent_record_guard",),
    "ai.CareProposal": ("care_guard",),
    "ai.Compensation": ("agent_record_guard",),
    "ai.EvaluationProtocol": ("evaluation_guard",),
    "ai.EvaluationRating": ("evaluation_guard",),
    "ai.EvaluationRun": ("evaluation_guard",),
    "ai.ExtractionCandidateRecord": ("extraction_guard",),
    "ai.ExtractionJob": ("extraction_guard",),
    "ai.FrontDeskThread": ("front_desk_guard",),
    "ai.FrontDeskTurn": ("front_desk_guard",),
    "ai.ModelChangeRequest": ("evaluation_guard",),
    "analytics.AnalystReview": ("analytics_insert",),
    "billing.Account": ("billing_journal_guard",),
    "billing.CashSession": ("billing_journal_guard",),
    "billing.FiscalConfiguration": ("fiscal_guard",),
    "billing.FiscalDocument": ("fiscal_guard",),
    "billing.JournalSettlement": ("billing_journal_guard",),
    "billing.Payout": ("billing_journal_guard",),
    "billing.PspEvidence": ("billing_psp_guard",),
    "billing.PspIntent": ("billing_psp_guard",),
    "billing.PspReceipt": ("billing_psp_guard",),
    "billing.Refund": ("billing_journal_guard",),
    "billing.SettlementAllocation": ("billing_journal_guard",),
    "comms.Message": ("comms_message_guard",),
    "consent.RefusalRecord": ("consent_binding",),
    "crm.Campaign": ("crm_binding",),
    "crm.CampaignSend": ("crm_binding",),
    "crm.Lead": ("crm_binding",),
    "crm.MarketingPreference": ("crm_binding",),
    "crm.PublicBookingProfile": ("crm_binding",),
    "crm.ReferralSource": ("crm_binding",),
    "crm.SatisfactionSurvey": ("crm_binding",),
    "ehr.AddendumSaveReceipt": ("ehr_addendum_binding",),
    "ehr.DraftEditState": ("ehr_autosave_binding",),
    "ehr.DraftSaveReceipt": ("ehr_autosave_binding",),
    "ehr.EncounterAddendum": ("ehr_addendum_binding",),
    "ehr.MedicationStatement": ("ehr_longitudinal_binding",),
    "ehr.NoteAccessGrant": ("ehr_note_binding",),
    "ehr.SpecialtyEntry": ("specialty_binding",),
    "ehr.SpecialtyPack": ("specialty_binding",),
    "identity.AuthenticatorRetirement": ("identity_retirement_boundary",),
    "identity.BreakGlassGrant": ("enterprise_guard",),
    "identity.RecoveryBatch": ("identity_recoverybatch_boundary",),
    "identity.ScimMembership": ("enterprise_guard",),
    "identity.ScimResource": ("enterprise_guard",),
    "identity.ScimRoleBinding": ("enterprise_guard",),
    "identity.ScimRoleMapping": ("enterprise_guard",),
    "identity.ScimToken": ("enterprise_guard",),
    "identity.SsoConnection": ("enterprise_guard",),
    "identity.SupportAccessGrant": ("enterprise_guard",),
    "identity.UnitGovernanceChange": ("enterprise_guard",),
    "insurance.Batch": ("insurance_guard",),
    "insurance.BatchGuide": ("insurance_guard",),
    "insurance.CommandReceipt": ("insurance_guard",),
    "insurance.Glosa": ("insurance_guard",),
    "insurance.Guide": ("insurance_guard",),
    "insurance.PayerRequest": ("insurance_guard",),
    "insurance.PaymentMatch": ("insurance_guard",),
    "insurance.Return": ("insurance_guard",),
    "insurance.Submission": ("insurance_guard",),
    "insurance.TussTerm": ("insurance_guard",),
    "intake.DataSubjectRequest": ("data_request_binding",),
    "intake.DuplicateCandidate": ("intake_duplicatecandidate_binding",),
    "intake.MergeDecision": ("intake_merge_decision_binding",),
    "intake.PatientAlias": ("intake_patient_alias_binding",),
    "interop.HiddenEnrollment": ("interop_guard",),
    "interop.ImportBatch": ("interop_guard",),
    "interop.ImportedRecord": ("interop_guard",),
    "interop.OrganizationExportPart": ("interop_partner_guard",),
    "interop.WebhookAttempt": ("interop_partner_guard",),
    "inventory.Calibration": ("inventory_guard",),
    "inventory.CommandReceipt": ("inventory_guard",),
    "inventory.Maintenance": ("inventory_guard",),
    "orders.LabAccount": ("orders_binding",),
    "orders.Order": ("orders_binding",),
    "orders.OrderEvent": ("orders_binding",),
    "orders.OrderItem": ("orders_binding",),
    "orders.Result": ("orders_binding",),
    "orders.ResultAcknowledgment": ("orders_binding",),
    "orders.ResultObservation": ("orders_binding",),
    "orders.ResultRejection": ("orders_binding",),
    "orders.ResultRelease": ("orders_binding",),
    "orders.ResultVersion": ("orders_binding",),
    "prescription.DocumentDraftContent": ("prescription_content_binding",),
    "prescription.PrescriptionIssuance": ("prescription_prescriptionissuance_binding",),
    "providers.CapabilityVersion": ("providers_version_insert",),
    "retention.ArtifactPurge": ("retention_purge_guard",),
    "scheduling.AvailabilityTemplate": ("scheduling_definition_guard",),
    "scheduling.ClinicianDelay": ("reception_guard",),
    "scheduling.DisplayDevice": ("reception_guard",),
    "scheduling.QueueEntry": ("reception_guard",),
    "scheduling.RoomStatus": ("reception_guard",),
    "scribe.AIDraft": ("draft_binding",),
    "scribe.AudioChunk": ("scribe_binding",),
    "scribe.CaptureEpoch": ("scribe_binding",),
    "scribe.ChunkTranscription": ("transcript_binding",),
    "scribe.DraftActionProposal": ("draft_binding",),
    "scribe.DraftSectionSuggestion": ("draft_binding",),
    "scribe.RecordingCommand": ("scribe_binding",),
    "scribe.RecordingSession": ("scribe_binding",),
    "scribe.ReviewDecision": ("draft_binding",),
    "scribe.TranscriptSegment": ("transcript_binding",),
    "scribe.TranscriptVersion": ("transcript_binding",),
    "teleconsult.TeleconsultDeviceCheck": ("teleconsult_binding",),
    "workflows.CareAction": ("care_guard",),
    "workflows.CarePlan": ("care_guard",),
    "workflows.PROAssignment": ("care_guard",),
    "workflows.PROOccurrence": ("care_guard",),
    "workflows.Recall": ("care_guard",),
    "workflows.Referral": ("care_guard",),
    "workflows.ReleaseDelivery": ("coordination_guard",),
    "workflows.ReleaseNoticeTemplate": ("coordination_guard",),
    "workflows.Task": ("workflow_guard",),
    "workflows.TaskComment": ("workflow_guard",),
    "workflows.UrgentConcern": ("coordination_guard",),
    "workflows.UrgentCoveragePolicy": ("coordination_guard",),
    "workflows.WorkflowDefinitionVersion": ("workflow_guard",),
    "workflows.WorkflowRun": ("workflow_guard",),
    "workflows.WorkflowStep": ("workflow_guard",),
}

# The named BEFORE ROW UPDATE guards suspended while a seed moves an earlier
# row through a product transition (``Seed.updates``), for the same reason:
# they authorize the update against a live verified session.
TRANSITION_GUARDS: Final[dict[str, tuple[str, ...]]] = {
    "intake.DuplicateCandidate": ("intake_duplicatecandidate_transition",),
}

# Columns holding secret material: the record keeps only their SHA-256.
SECRET_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "identity.TotpSecret.secret",
        "identity.User.password",
        "scribe.AudioChunk.wrapped_key",
    }
)

# The database role a model loads and reads back as when its table admits no
# owner-side write: ``clinic_app`` where the runtime policy and grants are
# the write path, ``clinic_resolver`` where only SECURITY DEFINER resolvers
# insert. Every other model loads as the migration owner under FORCE RLS.
LOAD_ROLES: Final[dict[str, str]] = {
    "analytics.AnalystReview": "clinic_app",
    "analytics.MetricDefinitionVersion": "clinic_resolver",
    "analytics.ReadModelRefresh": "clinic_resolver",
    "analytics.SavedReport": "clinic_app",
    "billing.Account": "clinic_resolver",
    "billing.CashSession": "clinic_app",
    "billing.CashSessionEvent": "clinic_resolver",
    "billing.FiscalDocument": "clinic_app",
    "billing.FiscalTransition": "clinic_resolver",
    "billing.InvoiceTerms": "clinic_resolver",
    "billing.JournalBook": "clinic_app",
    "billing.JournalEntry": "clinic_resolver",
    "billing.JournalLine": "clinic_resolver",
    "billing.JournalSettlement": "clinic_resolver",
    "billing.Payout": "clinic_app",
    "billing.PractitionerPayoutRule": "clinic_app",
    "billing.PspBinding": "clinic_app",
    "billing.PspEvidence": "clinic_app",
    "billing.PspIntent": "clinic_app",
    "billing.PspPolicy": "clinic_app",
    "billing.PspReceipt": "clinic_app",
    "billing.Refund": "clinic_app",
    "billing.SettlementAllocation": "clinic_resolver",
    "identity.SavedView": "clinic_app",
    "identity.TotpSecret": "clinic_resolver",
    "identity.UserPreference": "clinic_app",
    "insurance.Appeal": "clinic_app",
    "insurance.Batch": "clinic_app",
    "insurance.BatchGuide": "clinic_app",
    "insurance.ClinicalApproval": "clinic_app",
    "insurance.CommandReceipt": "clinic_app",
    "insurance.Glosa": "clinic_app",
    "insurance.Guide": "clinic_app",
    "insurance.InsuranceMembership": "clinic_app",
    "insurance.Payer": "clinic_app",
    "insurance.PayerContract": "clinic_app",
    "insurance.PayerRequest": "clinic_app",
    "insurance.PaymentMatch": "clinic_app",
    "insurance.PriceTable": "clinic_app",
    "insurance.Return": "clinic_app",
    "insurance.Submission": "clinic_app",
    "insurance.TussTerm": "clinic_app",
    "interop.ApiAccessToken": "clinic_resolver",
    "interop.ApiClient": "clinic_resolver",
    "interop.ApiClientSecret": "clinic_resolver",
    "interop.ApiWriteReceipt": "clinic_resolver",
    "interop.OrganizationExport": "clinic_resolver",
    "interop.OrganizationExportPart": "clinic_app",
    "interop.RndsSubmission": "clinic_resolver",
    "interop.WebhookAttempt": "clinic_resolver",
    "interop.WebhookDelivery": "clinic_resolver",
    "interop.WebhookSubscription": "clinic_resolver",
}

# Cross-row invariants the product's guards and deferred constraints enforce,
# re-checked over the seeded tenant at the leaf (clinic_super, read only).
# Each query counts violating rows; any non-zero count fails the gate.
LEAF_INVARIANTS: Final[dict[str, str]] = {
    "break_glass_review_and_notices": """
        SELECT count(*) FROM clinic_app.identity_breakglassgrant g
        WHERE g.organization_id=%(organization)s AND (NOT EXISTS (
            SELECT 1 FROM clinic_app.workflows_task t
            WHERE t.id=g.review_task_id AND t.kind='review'
            AND t.idempotency_key=g.id AND t.priority='high'
            AND t.due_at=g.expires_at+interval '24 hours'
            AND t.subject_ref=jsonb_build_object(
                'kind','clinic','id',g.clinic_id::text))
        OR (SELECT count(*) FROM clinic_app.identity_governancenotice n
            WHERE n.break_glass_id=g.id)<>2)""",
    "note_copy_from_other_finalized_version": """
        SELECT count(*) FROM clinic_app.ehr_copiednoteblock b
        JOIN clinic_app.ehr_clinicaldocumentversion s ON s.id=b.source_id
        JOIN clinic_app.ehr_clinicaldocumentversion t ON t.id=b.target_id
        JOIN clinic_app.ehr_clinicaldocument sd ON sd.id=s.document_id
        JOIN clinic_app.ehr_clinicaldocument td ON td.id=t.document_id
        JOIN clinic_app.ehr_encounter se ON se.id=sd.encounter_id
        JOIN clinic_app.ehr_encounter te ON te.id=td.encounter_id
        WHERE b.organization_id=%(organization)s AND (b.source_id=b.target_id
            OR s.state NOT IN ('finalized','superseded')
            OR se.patient_id<>te.patient_id OR b.inserted_revision>t.revision)""",
    "campaign_send_from_sending_marketing_thread": """
        SELECT count(*) FROM clinic_app.crm_campaignsend cs
        JOIN clinic_app.crm_campaign c ON c.id=cs.campaign_id
        JOIN clinic_app.comms_templateversion tv ON tv.id=c.template_id
        LEFT JOIN clinic_app.comms_message m ON m.id=cs.message_id
        LEFT JOIN clinic_app.comms_conversation cv ON cv.id=m.conversation_id
        WHERE cs.organization_id=%(organization)s AND (c.state NOT IN ('sending','sent')
            OR tv.purpose<>'marketing' OR tv.category<>'marketing'
            OR tv.approval_status<>'approved' OR m.direction IS DISTINCT FROM 'out'
            OR m.template_id IS DISTINCT FROM c.template_id
            OR cv.purpose IS DISTINCT FROM 'marketing'
            OR cv.enrollment_id IS DISTINCT FROM cs.enrollment_id)""",
    "evaluation_reviewers_distinct_physicians": """
        SELECT count(*) FROM clinic_app.ai_evaluationrun r
        WHERE r.organization_id=%(organization)s AND (r.rater_one_id=r.rater_two_id
            OR r.rater_one_id=r.adjudicator_id OR r.rater_two_id=r.adjudicator_id
            OR r.created_by_id IN (r.rater_one_id,r.rater_two_id,r.adjudicator_id)
            OR 3<>(SELECT count(DISTINCT role.user_id)
                FROM clinic_app.identity_userclinicrole role
                WHERE role.user_id IN (r.rater_one_id,r.rater_two_id,r.adjudicator_id)
                AND role.clinic_id=r.clinic_id AND role.role='physician'
                AND role.revoked_at IS NULL))""",
    "merge_decision_receipt": """
        SELECT count(*) FROM clinic_app.intake_mergedecision d
        LEFT JOIN clinic_app.intake_duplicatecandidate c ON c.id=d.candidate_id
        WHERE d.organization_id=%(organization)s AND (c.state IS DISTINCT FROM
            CASE d.decision WHEN 'merge' THEN 'merged' ELSE 'dismissed' END
            OR (d.decision='merge' AND NOT EXISTS (SELECT 1
                FROM clinic_app.intake_patientalias a
                WHERE a.merge_decision_id=d.id)))""",
    "duplicate_candidate_ordered_pair": """
        SELECT count(*) FROM clinic_app.intake_duplicatecandidate c
        WHERE c.organization_id=%(organization)s
        AND c.enrollment_a_id>=c.enrollment_b_id""",
    "demographics_version_has_receipt": """
        SELECT count(*) FROM clinic_app.intake_patientdemographics v
        WHERE v.organization_id=%(organization)s AND NOT EXISTS (SELECT 1
            FROM clinic_app.intake_demographicscorrection r
            WHERE r.demographics_id=v.id)""",
    "delegate_grant_two_enrollments": """
        SELECT count(*) FROM clinic_app.identity_patientdelegategrant g
        WHERE g.organization_id=%(organization)s
        AND g.represented_enrollment_id=g.delegate_enrollment_id""",
}

# Columns a later candidate migration rewrites on purpose, with the reason.
PRESERVATION_EXCEPTIONS: Final[dict[str, str]] = {}

PROVIDERS: Final[dict[str, Seed]] = {
    "ProviderCapability": Seed(
        _rows(
            {
                "key": "upgrade_synthetic_capability",
                "record_ref": "synthetic-upgrade-record",
                "description": "Synthetic upgrade capability; no real provider.",
            }
        )
    ),
    "CapabilityApproval": Seed(
        _rows(
            {
                "approver_name": "Synthetic Approver",
                "approver_role": "synthetic-review",
                "evidence_uri": "https://example.invalid/upgrade-evidence",
            }
        )
    ),
    # Researched (no approval) and sandbox (approved) sides of the lifecycle.
    "CapabilityVersion": Seed(
        lambda c: (
            {
                "provider": "synthetic-upgrade",
                "account": "synthetic",
                "environment": "sandbox",
                "api_version": "v1",
                "region": "br-synthetic",
                "state": "researched",
            },
            {
                "provider": "synthetic-upgrade",
                "account": "synthetic",
                "environment": "sandbox",
                "api_version": "v2",
                "region": "br-synthetic",
                "state": "sandbox",
                "approval_id": c.ref("providers.CapabilityApproval"),
            },
        )
    ),
    "ActivationRecord": Seed(
        lambda c: ({"version_id": c.ref("providers.CapabilityVersion", 1)},)
    ),
    "HealthEvent": Seed(_rows({"kind": "note", "detail": "Synthetic health note."})),
}

IDENTITY: Final[dict[str, Seed]] = {
    "UserPreference": Seed(_rows({"theme": "dark"})),
    "CareTeamMembership": Seed(
        lambda c: ({"role": "physician", "user_id": c.physician},)
    ),
    "ProfessionalRegistration": Seed(
        lambda c: (
            {
                "role": "physician",
                "council": "CRM",
                "jurisdiction": "SP",
                "number": "SYNTHETIC-CRM-0001",
                "specialty": "Synthetic general practice",
                "status": "regular",
                "user_id": c.physician,
                "valid_to": c.now + timedelta(days=365),
            },
        )
    ),
    "RoleGrant": Seed(
        _rows({"role": "receptionist", "permission": "appointment.book"})
    ),
    "ServicePrincipal": Seed(
        _rows(
            {
                "name": "upgrade-agent",
                "db_identity": "clinic_agent",
                "purpose": "appointment-read",
            }
        )
    ),
    "ServicePrincipalGrant": Seed(_rows({"permission": "appointment.read"})),
    "SavedView": Seed(
        _rows({"destination": "patients", "params": {"q": "synthetic"}}),
    ),
    "SignupRegistration": Seed(),
    "AuthenticatorReset": Seed(),
    "AuthenticatorRetirement": Seed(),
    "OnboardingProgress": Seed(_rows({"step_key": "clinic-profile"})),
    "RecoveryBatch": Seed(
        support=(
            (
                "otp_static.StaticDevice",
                Seed(lambda c: ({"user_id": c.owner, "name": "synthetic-recovery"},)),
            ),
        )
    ),
    "RecoveryCode": Seed(),
    # The physician's session: the owner's later authenticator lifecycle rows
    # legitimately revoke the owner's own sessions.
    "SessionRecord": Seed(
        lambda c: (
            {
                "ip_network": "192.0.2.0/24",
                "user_agent_family": "firefox",
                "user_id": c.physician,
            },
        )
    ),
    "StaffInvitation": Seed(
        lambda c: ({"bundle": "receptionist", "pending_user_id": c.physician},)
    ),
    "PatientDelegateGrant": Seed(
        lambda c: (
            {
                "authority_kind": "patient_authorized_adult",
                "purposes": ["care_processing"],
                "operations": ["records"],
                "represented_enrollment_id": c.enrollment,
                "delegate_enrollment_id": c.companion_enrollment,
                "requested_by_id": c.owner,
            },
        )
    ),
    "Passkey": Seed(_rows({"credential_id": BYTES32, "public_key": BYTES32})),
    "TotpSecret": Seed(
        adopt="0050 moves every existing device secret here",
    ),
    # A grant with its mandatory high-priority review task and both notices
    # (the deferred enterprise_grant_complete constraint checks all three).
    "BreakGlassGrant": Seed(
        lambda c: (
            {
                "id": c.key("break-glass"),
                "clinician_id": c.physician,
                "reason": "Synthetic review.",
                "created_at": c.now,
                "expires_at": c.now + timedelta(hours=2),
                "review_task_id": c.key("break-glass-review"),
            },
        ),
        support=(
            (
                "workflows.Task",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("break-glass-review"),
                            "kind": "review",
                            "subject_ref": {"kind": "clinic", "id": str(c.clinic)},
                            "due_at": c.now + timedelta(hours=26),
                            "priority": "high",
                            "idempotency_key": c.key("break-glass"),
                            "fingerprint": DIGEST,
                        },
                    )
                ),
            ),
        ),
    ),
    "GovernanceNotice": Seed(
        _rows({"audience": "manager"}, {"audience": "patient"}), actor="physician"
    ),
    "SsoConnection": Seed(_rows({"provider_key": "synthetic-oidc"})),
    "ScimResource": Seed(
        lambda c: (
            {"kind": "Users", "user_id": c.physician, "search_hash": BYTES32},
            {"kind": "Groups", "search_hash": bytes(32)},
        )
    ),
    "ScimMembership": Seed(
        lambda c: (
            {
                "group_id": c.ref("identity.ScimResource", 1),
                "member_id": c.ref("identity.ScimResource", 0),
            },
        )
    ),
    "ScimRoleBinding": Seed(),
    "ScimRoleMapping": Seed(
        lambda c: (
            {
                "group_id": c.ref("identity.ScimResource", 1),
                "unit_id": c.clinic,
                "bundle": "receptionist",
            },
        )
    ),
    "ScimToken": Seed(_rows({"secret_hash": BYTES32})),
    "SupportAccessGrant": Seed(
        lambda c: ({"support_user_id": c.physician, "purpose": "Synthetic."},)
    ),
    "UnitGovernanceChange": Seed(
        lambda c: (
            {
                "kind": "unit_membership",
                "access_denied": False,
                "policy_version": 1,
                "target_user_id": c.physician,
            },
        )
    ),
    "DelegateAccessEvent": Seed(_rows({"record_type": "result"})),
}

TELECONSULT: Final[dict[str, Seed]] = {
    "TeleconsultDeviceCheck": Seed(
        lambda c: (
            {
                "role": "physician",
                "participant_id": c.physician,
                "camera": "ok",
                "microphone": "ok",
                "speaker": "ok",
                "network": "good",
            },
        ),
        actor="physician",
    ),
}

SCHEDULING: Final[dict[str, Seed]] = {
    "Holiday": Seed(),
    "Resource": Seed(
        _rows(
            {"kind": "room", "name": "Synthetic room", "capacity": 2},
            {"kind": "location", "name": "Synthetic front desk"},
        )
    ),
    "AppointmentResource": Seed(),
    "Absence": Seed(lambda c: ({"practitioner_id": c.physician},)),
    "AvailabilityTemplate": Seed(
        lambda c: (
            {
                "weekdays": [1, 3],
                "start_local": c.now.replace(hour=8, minute=0).time(),
                "end_local": c.now.replace(hour=12, minute=0).time(),
                "valid_from": c.now.date(),
                "valid_to": (c.now + timedelta(days=90)).date(),
                "timezone": "America/Sao_Paulo",
                "practitioner_id": c.physician,
            },
        )
    ),
    "ServiceType": Seed(
        _rows(
            {
                "name": "Synthetic consultation",
                "duration_min": 30,
                "required_professional_roles": ["physician"],
                "required_resource_kinds": ["room"],
            }
        )
    ),
    "AppointmentSeries": Seed(
        lambda c: (
            {
                "frequency": "weekly",
                "weekday": 2,
                "first_date": (c.now + timedelta(days=7)).date(),
                "start_local": c.now.replace(hour=9, minute=0).time(),
                "end_local": c.now.replace(hour=9, minute=30).time(),
                "timezone": "America/Sao_Paulo",
                "count": 4,
                "create_fingerprint": BYTES32,
            },
        )
    ),
    "AppointmentTransition": Seed(
        _rows(
            {
                "from_status": "scheduled",
                "to_status": "confirmed",
                "revision": 2,
                "actor_kind": "staff",
            }
        )
    ),
    "SeriesException": Seed(
        _rows({"occurrence_index": 2, "kind": "cancelled", "affected_count": 1})
    ),
    "ClinicianDelay": Seed(
        lambda c: ({"practitioner_id": c.physician, "minutes": 15},)
    ),
    "DisplayDevice": Seed(_rows({"token_hash": DIGEST})),
    "QueueEntry": Seed(
        lambda c: (
            {
                "location_id": c.ref("scheduling.Resource", 1),
                "ticket_code": "A1",
                "position": 1,
            },
        )
    ),
    "RoomStatus": Seed(
        lambda c: ({"room_id": c.ref("scheduling.Resource"), "unit": 1},)
    ),
}

INTAKE: Final[dict[str, Seed]] = {
    "ClinicIntakePolicy": Seed(
        _rows({"version": 1, "required_fields": ["legal_name"]})
    ),
    "EmergencyContact": Seed(
        _rows({"relationship": "Synthetic sibling", "phone": "+55 11 0000-0000"})
    ),
    "InsuranceMembership": Seed(),
    "PatientAddress": Seed(
        _rows({"kind": "home", "city": "Synthetic City", "state_code": "SP"})
    ),
    "PatientIdentifier": Seed(
        _rows({"kind": "cns", "value": "SYNTHETIC-CNS-0001", "blind_index": BYTES32})
    ),
    # Patient-reported provenance with explicitly unknown fields, then the
    # staff correction receipt for the same version.
    "PatientDemographics": Seed(
        _rows(
            {
                "version": 1,
                "legal_name": "Synthetic Upgrade Persona",
                "social_name": "Synthetic Persona",
                "pronouns": "",
                "occupation": None,
                "source": "patient_reported",
                "unknown_fields": {"occupation": "unknown", "pronouns": "declined"},
            }
        )
    ),
    "DemographicsCorrection": Seed(
        _rows(
            {
                "actor_label": "Synthetic reception",
                "reason": "Synthetic patient-reported update.",
                "changed_fields": ["legal_name", "social_name"],
            }
        )
    ),
    "DuplicateCandidate": Seed(
        lambda c: (
            {
                "signals": ["name_birth"],
                "enrollment_a_id": min(str(c.enrollment), str(c.companion_enrollment)),
                "enrollment_b_id": max(str(c.enrollment), str(c.companion_enrollment)),
            },
        )
    ),
    "PatientDuplicateIndex": Seed(
        lambda c: (
            {
                "patient_id": c.companion_patient,
                "version": 2,
                "index_key_version": 1,
                "name_birth_index": BYTES32,
            },
        )
    ),
    "MergeDecision": Seed(
        lambda c: (
            {
                "decision": "merge",
                "merged_version": 1,
                "survivor_version": 1,
                "result_version": 2,
                "merged_patient_id": c.companion_patient,
                "survivor_patient_id": c.patient,
                "field_choices": {"legal_name": "survivor"},
                "reason": "Synthetic duplicate review.",
                "adjudicator_id": c.owner,
                # One adjudication instant: the candidate transition and the
                # receipt both bind to the transaction's timestamp.
                "created_at": TRANSACTION_TIME,
            },
        ),
        # The merge's receipts: the survivor's resulting demographics version
        # and its correction by the adjudicator, written in the same commit.
        support=(
            (
                "intake.PatientDemographics",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("merge-result-demographics"),
                            "version": 2,
                            "legal_name": "Synthetic Upgrade Persona",
                            "source": "staff_recorded",
                            "created_at": TRANSACTION_TIME,
                        },
                    )
                ),
            ),
            (
                "intake.DemographicsCorrection",
                Seed(
                    lambda c: (
                        {
                            "actor_id": c.owner,
                            "actor_label": "Synthetic adjudicator",
                            "reason": "Synthetic merge result.",
                            "changed_fields": ["legal_name"],
                            "demographics_id": c.key("merge-result-demographics"),
                            "previous_id": c.ref("intake.PatientDemographics"),
                        },
                    )
                ),
            ),
        ),
        updates=(
            (
                "intake.DuplicateCandidate",
                lambda c: (c.ref("intake.DuplicateCandidate"), {"state": "merged"}),
            ),
        ),
    ),
    "PatientAlias": Seed(
        lambda c: (
            {
                "merged_patient_id": c.companion_patient,
                "survivor_patient_id": c.patient,
                "active": True,
                "deactivation_reason": None,
            },
        )
    ),
    "PatientMergeTransfer": Seed(
        lambda c: (
            {
                "kind": "appointment",
                "original_id": str(c.companion_enrollment),
                "replacement_id": str(c.enrollment),
                "action": "recreated",
            },
        )
    ),
    "DataSubjectRequest": Seed(
        lambda c: (
            {
                "kind": "correction",
                "details": "Synthetic correction request.",
                "requester_enrollment_id": c.enrollment,
                # The request's own AFTER trigger opens this review task.
                "task_id": c.key("data-request-task"),
            },
        )
    ),
}

CONSENT: Final[dict[str, Seed]] = {
    "NoticeVersion": Seed(_rows({"topic": "ai_use", "version": 1})),
    "AIUseDisclosure": Seed(
        lambda c: (
            {"informed": True, "refused": False, "recorded_by_id": c.physician},
        ),
        actor="physician",
    ),
    "ParticipantAcknowledgment": Seed(
        lambda c: (
            {
                "participant_kind": "interpreter",
                "acknowledged_by_clinician_id": c.physician,
            },
        ),
        actor="physician",
    ),
    "RefusalRecord": Seed(),
}

EHR: Final[dict[str, Seed]] = {
    "Episode": Seed(_rows({"title": "Synthetic follow-up episode", "state": "open"})),
    "EpisodeEncounter": Seed(),
    # An amendment draft of the witness's finalized SOAP (the only draft the
    # product allows once a version is final), edited by its author; later
    # copies, receipts and AI drafts target it.
    "DraftEditState": Seed(
        lambda c: ({"author_id": c.physician},),
        actor="physician",
        support=(
            (
                "ehr.ClinicalDocumentVersion",
                Seed(
                    lambda c: (
                        {
                            "document_id": _finalized(c, "document_id"),
                            "template_id": _finalized(c, "template_id"),
                            "author_id": c.physician,
                            "version": 3,
                            "state": "draft",
                            "revision": 1,
                            "content_digest": "",
                            "content": _finalized(c, "content"),
                            "content_sha256": _finalized(c, "content_sha256"),
                            "amendment_of_id": _finalized(c, "id"),
                            "amendment_reason": "Synthetic amendment draft.",
                        },
                    ),
                    actor="physician",
                ),
            ),
        ),
    ),
    "DraftSaveReceipt": Seed(
        _rows({"request_sha256": DIGEST, "base_revision": 1, "revision": 2}),
        actor="physician",
    ),
    "EncounterAddendum": Seed(
        lambda c: ({"author_id": c.owner, "text": "Synthetic addendum draft."},)
    ),
    "AddendumSaveReceipt": Seed(
        _rows({"request_sha256": DIGEST, "base_revision": 1, "revision": 2})
    ),
    "MedicationStatement": Seed(
        lambda c: (
            {
                "origin": "reported_by_patient",
                "provenance": "reported_by_patient",
                "status": "unknown",
                "performer_id": c.physician,
                "details": {"name": "Synthetic medication", "dose": "unknown"},
            },
            {
                "origin": "observed",
                "provenance": "clinician_confirmed",
                "status": "active",
                "performer_id": c.physician,
                "reconciled_by_id": c.physician,
                "reconciled_at": c.now,
                "details": {"name": "Synthetic medication", "dose": "1 unit"},
            },
        )
    ),
    "Observation": Seed(
        lambda c: (
            {
                "origin": "observed",
                "provenance": "observed",
                "kind": "heart_rate",
                "unit": "/min",
                "value": {"value": 72},
                "performer_id": c.physician,
            },
        )
    ),
    "RestrictedNote": Seed(lambda c: ({"restricted_by_id": c.physician},)),
    "NoteAccessGrant": Seed(
        lambda c: ({"granted_by_id": c.physician, "grantee_id": c.owner},)
    ),
    "CopiedNoteBlock": Seed(
        lambda c: (
            {
                "section": "plan",
                "inserted_revision": 1,
                "source_id": c.ref("ehr.ClinicalDocumentVersion", state="finalized"),
                "target_id": c.ref("ehr.ClinicalDocumentVersion"),
            },
        )
    ),
    "TimelineProjection": Seed(),
    "TimelineRefresh": Seed(adopt="0014 backfills one refresh row per patient"),
    "SpecialtyPack": Seed(
        lambda c: (
            {
                "key": "general",
                "version": 1,
                "license_ref": "synthetic-license",
                "clinical_owner_id": c.physician,
            },
        )
    ),
    "SpecialtyEntry": Seed(
        lambda c: (
            {
                "kind": "procedure",
                "details": {"note": "Synthetic procedure entry."},
                "recorded_by_id": c.physician,
            },
        )
    ),
}

PRESCRIPTION: Final[dict[str, Seed]] = {
    "DocumentDraftContent": Seed(
        _rows({"version": 2, "content": {"body": "Synthetic"}})
    ),
    "MedicationCatalogImport": Seed(
        _rows({"source_url": "https://example.invalid/catalog.csv", "checksum": DIGEST})
    ),
    "MedicationCatalogEntry": Seed(
        _rows(
            {
                "code": "SYN0001",
                "description": "Synthetic medication",
                "strength_form": "10 mg tablet",
            }
        )
    ),
    "MedicationFavorite": Seed(lambda c: ({"issuer_id": c.physician},)),
    "PrescriptionClassPolicy": Seed(adopt="0013 loads the closed class catalog"),
    "PrescriptionIssuance": Seed(lambda c: ({"issuer_id": c.physician},)),
    "SncrNumberAllocation": Seed(
        lambda c: (
            {
                "category": "notificacao_b",
                "request_digest": DIGEST,
                "recording": {"number": "SYNTHETIC-0001"},
                "issuer_id": c.physician,
            },
        )
    ),
}

RETENTION: Final[dict[str, Seed]] = {
    "ArtifactPurge": Seed(
        lambda c: (
            {
                "record_class": "ai_artifact",
                "record_id": c.ref("ai.AIArtifact"),
                "principal_id": c.owner,
            },
        )
    ),
}

WORKFLOWS: Final[dict[str, Seed]] = {
    "Task": Seed(
        lambda c: (
            {
                "kind": "follow_up",
                "subject_ref": {"kind": "enrollment", "id": str(c.enrollment)},
                "due_at": c.future,
                "fingerprint": DIGEST,
            },
            {
                "kind": "follow_up",
                "subject_ref": {"kind": "enrollment", "id": str(c.enrollment)},
                "due_at": c.future,
                "state": "assigned",
                "owner_user_id": c.physician,
                "fingerprint": DIGEST,
            },
        )
    ),
    "TaskComment": Seed(_rows({"body": "Synthetic task comment."})),
    "WorkflowDefinitionVersion": Seed(
        _rows(
            {
                "key": "synthetic-follow-up",
                "version": 1,
                "steps": [{"kind": "task", "task_kind": "follow_up"}],
                "fingerprint": DIGEST,
                "creation_fingerprint": DIGEST,
            }
        )
    ),
    "WorkflowRun": Seed(
        lambda c: (
            {"context_refs": {"enrollment": str(c.enrollment)}, "fingerprint": DIGEST},
        )
    ),
    "WorkflowStep": Seed(_rows({"position": 0})),
    "CarePlan": Seed(
        lambda c: (
            {
                "clinician_id": c.physician,
                "source_version_id": c.ref(
                    "ehr.ClinicalDocumentVersion", state="finalized"
                ),
                "fingerprint": DIGEST,
            },
        )
    ),
    "CareAction": Seed(
        lambda c: (
            {
                "kind": "order",
                "instruction": None,
                "window_start": c.now,
                "window_end": c.future,
                "origin_version_id": c.ref(
                    "ehr.ClinicalDocumentVersion", state="finalized"
                ),
                "created_by_id": c.physician,
                "task_id": c.ref("workflows.Task"),
                "fingerprint": DIGEST,
            },
        )
    ),
    "CareRefusal": Seed(
        lambda c: (
            {
                "kind": "recall",
                "record_id": c.ref("workflows.Task"),
                "refused_at": c.now,
                "retry_at": c.future,
            },
        )
    ),
    "PROAssignment": Seed(
        _rows(
            {
                "instrument_key": "phq9",
                "instrument_version": 1,
                "interval_days": 30,
                "occurrences": 2,
                "response_role": "nurse",
                "staffing_policy_ref": "synthetic-policy",
                "fingerprint": DIGEST,
            }
        )
    ),
    "PROOccurrence": Seed(_rows({"sequence": 1, "answers": None})),
    "Recall": Seed(
        _rows(
            {
                "reason": "routine_return",
                "interval_days": 365,
                "owner_role": "receptionist",
                "fingerprint": DIGEST,
            }
        )
    ),
    "Referral": Seed(
        _rows({"target_kind": "specialty", "target_label": "Synthetic specialist"})
    ),
    "ReleaseNoticeTemplate": Seed(
        _rows({"version": 1, "body": "Seu resultado sintetico esta disponivel."})
    ),
    "UrgentCoveragePolicy": Seed(
        lambda c: (
            {
                "version": 1,
                "weekdays": [1, 2, 3, 4, 5],
                "opens_at": c.now.replace(hour=8, minute=0).time(),
                "closes_at": c.now.replace(hour=18, minute=0).time(),
                "response_hours": 4,
                "responsible_roles": ["physician"],
            },
        )
    ),
    "UrgentConcern": Seed(),
    "ReleaseDelivery": Seed(_rows({"audience": "patient"})),
}

COMMS: Final[dict[str, Seed]] = {
    "ProviderAccount": Seed(
        _rows(
            {
                "channel": "whatsapp",
                "provider": "whatsapp-cloud-v1",
                "external_account_id": "synthetic-account",
                "signing_key_digest": DIGEST,
            }
        )
    ),
    # A portal care thread and a separate WhatsApp marketing thread.
    "Conversation": Seed(
        lambda c: (
            {
                "channel": "portal",
                "purpose": "care",
                "enrollment_id": c.enrollment,
                "contact_fingerprint": DIGEST,
                "contact_address": "synthetic.invalid",
                "last_activity_at": c.now,
            },
            {
                "channel": "whatsapp",
                "purpose": "marketing",
                "enrollment_id": c.enrollment,
                "provider_account_id": c.ref("comms.ProviderAccount"),
                "contact_fingerprint": DIGEST,
                "contact_address": "+55 11 0000-0000",
                "last_activity_at": c.now,
            },
        ),
        after=("comms.ProviderAccount",),
    ),
    "Participant": Seed(
        lambda c: ({"role": "patient", "enrollment_id": c.enrollment},)
    ),
    "Message": Seed(
        lambda c: (
            {
                "direction": "in",
                "body": "Synthetic inbound.",
                "event_id": "evt-1",
                "template_parameters": None,
            },
            {
                "direction": "out",
                "body": "Synthetic reply.",
                "command_id": uuid4(),
                "sent_by_id": c.owner,
                "template_parameters": None,
            },
            {
                "direction": "out",
                "conversation_id": c.ref("comms.Conversation", 1),
                "body": MARKETING_BODY,
                "command_id": uuid4(),
                "sent_by_id": c.owner,
                "template_id": c.ref("comms.TemplateVersion", 1),
                "template_parameters": None,
            },
        )
    ),
    "InboundEvent": Seed(_rows({"event_id": "evt-1", "kind": "message"})),
    "InternalNote": Seed(_rows({"body": "Synthetic internal note."})),
    "MessageMeter": Seed(_rows({"source": "provider", "category": "service"})),
    "QuickReply": Seed(
        _rows({"title": "Synthetic", "body": "Synthetic reply.", "purpose": "care"})
    ),
    "SuggestedReply": Seed(_rows({"body": "Synthetic suggestion.", "drafter": "rule"})),
    "TemplateVersion": Seed(
        _rows(
            {
                "channel": "whatsapp",
                "name": "synthetic_reminder",
                "version": 1,
                "category": "utility",
                "purpose": "transactional",
                "body": "Lembrete sintetico.",
                "content_digest": DIGEST,
            },
            {
                "channel": "whatsapp",
                "name": "synthetic_marketing",
                "version": 1,
                "category": "marketing",
                "purpose": "marketing",
                "body": MARKETING_BODY,
                "parameter_count": 0,
                "approval_status": "approved",
                "content_digest": DIGEST,
            },
        )
    ),
}

AI: Final[dict[str, Seed]] = {
    "GatewaySwitch": Seed(),
    "ModelConfiguration": Seed(
        _rows(
            {
                "purpose": "brief",
                "capability_key": "llm_inference",
                "provider": "synthetic",
                "model_id": "upgrade-synthetic",
                "region": "br-synthetic",
                "jurisdiction_class": "BR",
                "endpoint": "https://ai.synthetic.invalid/",
                "retention_terms": "synthetic",
                "training_terms": "synthetic",
                "version": 1,
                "route": "primary",
                "state": "draft",
                "approval_ref": "",
            }
        )
    ),
    "AICircuit": Seed(),
    "AIArtifact": Seed(
        lambda c: (
            {
                "purpose": "brief",
                "kind": "input",
                "method": "complete",
                "content": "Synthetic upgrade input; no clinical data.",
                "retention_class": "transient_input",
                "patient_enrollment_id": c.enrollment,
                "expires_at": c.future,
            },
            {
                "purpose": "analyst",
                "kind": "input",
                "method": "complete",
                "content": "Synthetic analyst question; no clinical data.",
                "retention_class": "transient_input",
                "expires_at": c.future,
            },
        )
    ),
    # A reserved brief call and a settled analyst call.
    "AIInvocation": Seed(
        lambda c: (
            {
                "purpose": "brief",
                "config_version": 1,
                "prompt_sha256": DIGEST,
                "limit_tokens_in": 10,
                "limit_tokens_out": 10,
                "limit_audio_seconds": 10,
                "reserved_micros": 100,
                "reason_code": "",
            },
            {
                "purpose": "analyst",
                "inputs_ref_id": c.ref("ai.AIArtifact", 1),
                "config_version": 1,
                "prompt_sha256": DIGEST,
                "limit_tokens_in": 10,
                "limit_tokens_out": 10,
                "limit_audio_seconds": 10,
                "reserved_micros": 100,
                "tokens_in": 4,
                "tokens_out": 3,
                "status": "succeeded",
                "settled_at": c.now,
                "reason_code": "",
            },
        )
    ),
    "AgentPolicyVersion": Seed(
        _rows(
            {
                "capability": "front_desk",
                "version": 1,
                "autonomy_ceiling": "A1",
                "tool_allowlist": [],
                "budget_actions": 1,
                "budget_micros": 100,
                "timeout_seconds": 30,
                "escalation_queue": "administrative",
                "compensation_policy": {"mode": "manual", "version": 1},
            }
        )
    ),
    "AgentRun": Seed(
        _rows(
            {
                "prompt_sha256": DIGEST,
                "contract": {"synthetic": True},
                "evaluation_set": "synthetic-upgrade",
                "reason_code": "",
            }
        )
    ),
    "ActionProposal": Seed(
        _rows(
            {
                "payload": '{"synthetic":true}',
                "digest": DIGEST,
                "state": "approved",
                "reason_code": "",
            }
        )
    ),
    "Approval": Seed(
        _rows(
            {
                "digest": DIGEST,
                "authority_snapshot": {"synthetic": True},
                "step_up_evidence": {"synthetic": True},
                "attestation": DIGEST,
            }
        )
    ),
    "ActionExecution": Seed(_rows({"digest": DIGEST, "reason_code": ""})),
    "ActionReceipt": Seed(
        _rows(
            {
                "kind": "reconciliation",
                "outcome": "unknown",
                "digest": DIGEST,
                "fencing_token": 1,
                "attestation": DIGEST,
            }
        )
    ),
    "Compensation": Seed(_rows({"reason_code": "synthetic"})),
    "ExtractionJob": Seed(
        lambda c: (
            {
                "user_id": c.physician,
                "sha256": DIGEST,
                "rotations": [0],
                "attachment_id": c.ref("ehr.ClinicalAttachment"),
            },
        )
    ),
    "ExtractionCandidateRecord": Seed(
        _rows({"position": 0, "payload": {"synthetic": True}, "status": "candidate"})
    ),
    "EvaluationProtocol": Seed(
        _rows(
            {
                "capability": "brief",
                "version": 1,
                "corpus_version": "synthetic-upgrade",
                "corpus_sha256": DIGEST,
                "case_manifest": [{"id": "synthetic", "encounter": "synthetic"}],
                "thresholds_document": '{"minimum_cases":1}',
                "thresholds_sha256": DIGEST,
                "clinical_owner_ref": "synthetic-not-clinical-approval",
            }
        )
    ),
    # Three distinct physician reviewers; the creator rates nothing.
    "EvaluationRun": Seed(
        lambda c: (
            {
                "configuration_version": 1,
                "versions": {
                    "prompt": DIGEST,
                    "retrieval": DIGEST,
                    "schema": DIGEST,
                    "model": DIGEST,
                    "version": "1",
                },
                "rater_one_id": c.physician,
                "rater_two_id": c.key("rater-two"),
                "adjudicator_id": c.key("adjudicator"),
                "created_by_id": c.owner,
                "status": "pending",
            },
        ),
        support=tuple(
            support
            for name in ("rater-two", "adjudicator")
            for support in (
                ("identity.User", Seed(_reviewer(name))),
                ("identity.UserClinicRole", Seed(_reviewer_role(name))),
            )
        ),
    ),
    "EvaluationRating": Seed(
        lambda c: (
            {
                "case_id": "synthetic",
                "slot": "rater_one",
                "user_id": c.physician,
                "labels": {"error": 0},
            },
        )
    ),
    "ModelChangeRequest": Seed(_rows({"approval_ref": "synthetic-upgrade"})),
    "FrontDeskThread": Seed(),
    "KnowledgeArticle": Seed(
        _rows(
            {
                "key": "synthetic",
                "version": 1,
                "topic": "hours",
                "title": "Synthetic",
                "body": "Synthetic hours.",
                "keywords": ["synthetic"],
            }
        )
    ),
    "FrontDeskTurn": Seed(),
    "CareProposal": Seed(
        lambda c: (
            {
                "plan_revision": 1,
                "position": 0,
                "kind": "order",
                "audience": "clinical",
                "state": "pending",
                "window_start": c.now,
                "window_end": c.future,
                "source_version_id": c.ref(
                    "ehr.ClinicalDocumentVersion", state="finalized"
                ),
                "requested_by_id": c.physician,
            },
        )
    ),
}

SCRIBE: Final[dict[str, Seed]] = {
    "RecordingSession": Seed(
        lambda c: (
            {
                "physician_id": c.physician,
                "token_digest": DIGEST,
                "capability_snapshot": {"synthetic": True},
                "state": "recording",
            },
        ),
        actor="physician",
    ),
    "CaptureEpoch": Seed(_rows({"number": 1})),
    "RecordingCommand": Seed(
        _rows(
            {
                "action": "pause",
                "payload_digest": DIGEST,
                "result_state": "paused",
                "result_epoch": 1,
            }
        )
    ),
    "AudioChunk": Seed(
        _rows(
            {
                "track": "microphone",
                "seq": 0,
                "sha256": DIGEST,
                "duration_ms": 1000,
                "codec": "audio/webm;codecs=opus",
                "size_bytes": 1,
                "storage_key": "b" * 32,
                "state": "accepted",
            }
        )
    ),
    "ChunkTranscription": Seed(),
    "TranscriptVersion": Seed(
        lambda c: (
            {
                "revision": 1,
                "state": "partial",
                "mutation": "recognized",
                "coverage": {"chunks": [0]},
                "recognition_index": 0,
                "created_by_id": c.physician,
            },
        )
    ),
    "TranscriptSegment": Seed(
        _rows(
            {
                "start_ms": 0,
                "end_ms": 1000,
                "text": "Synthetic transcript, no clinical content.",
                "uncertainty": [],
                "language": "pt-BR",
            }
        )
    ),
    "AIDraft": Seed(
        lambda c: (
            {
                "generation": 1,
                "base_revision": 1,
                "base_epochs": {"subjective": 0, "objective": 0},
                "base_content": {"synthetic": True},
                "sections": ["subjective"],
                "source_refs": [{"synthetic": True}],
                "requested_by_id": c.physician,
                "version_id": c.ref("ehr.ClinicalDocumentVersion", state="finalized"),
            },
        )
    ),
    "DraftSectionSuggestion": Seed(
        lambda c: (
            {
                "section": "subjective",
                "text": "Synthetic pending suggestion.",
                "citations": [{"synthetic": True}],
                "assertion_class": "reported",
                "version_id": c.ref("ehr.ClinicalDocumentVersion", state="finalized"),
            },
        )
    ),
    "DraftActionProposal": Seed(
        lambda c: (
            {
                "ordinal": 0,
                "kind": "task",
                "text": "Synthetic separate proposal.",
                "citations": [{"synthetic": True}],
                "version_id": c.ref("ehr.ClinicalDocumentVersion", state="finalized"),
            },
        )
    ),
    "ReviewDecision": Seed(
        lambda c: (
            {
                "request_digest": DIGEST,
                "action": "rejected",
                "section": "subjective",
                "expected_revision": 1,
                "result_revision": 1,
                "result_sha256": DIGEST,
                "capability_snapshot": {"purpose": "scribe_draft"},
                "reviewed_by_id": c.physician,
                "version_id": c.ref("ehr.ClinicalDocumentVersion", state="finalized"),
            },
        )
    ),
}

ORDERS: Final[dict[str, Seed]] = {
    "LabAccount": Seed(_rows({"signing_key_digest": DIGEST})),
    "Order": Seed(lambda c: ({"ordered_by_id": c.physician, "fingerprint": DIGEST},)),
    "OrderEvent": Seed(_rows({"sequence": 1, "state": "placed"})),
    "OrderItem": Seed(_rows({"exam_code": "SYN-HB"})),
    "Result": Seed(),
    "ResultVersion": Seed(
        lambda c: (
            {
                "number": 1,
                "fingerprint": DIGEST,
                "identity": {"synthetic": True},
                "storage_key": "c" * 32,
                "sha256": DIGEST,
                "size_bytes": 1,
                "content_type": "application/pdf",
                "uploader_id": c.physician,
            },
        )
    ),
    "ResultMatch": Seed(_rows({"factors": {"synthetic": True}})),
    "ResultObservation": Seed(_rows({"position": 0, "source": {"synthetic": True}})),
    "ResultQuarantine": Seed(_rows({"reason": "identity_mismatch"})),
    "ResultRejection": Seed(_rows({"evidence_code": "synthetic"})),
    "ResultAcknowledgment": Seed(
        lambda c: ({"reviewer_id": c.physician, "disposition": "normal"},)
    ),
    "ResultRelease": Seed(
        lambda c: ({"released_by_id": c.physician, "audience": "patient"},)
    ),
    "ResultWorkItem": Seed(_rows({"kind": "review"})),
}

BILLING: Final[dict[str, Seed]] = {
    # The witness owner also holds the finance and front-desk roles the
    # finance, insurance and reception guards authorize against.
    "JournalBook": Seed(
        support=tuple(
            ("identity.UserClinicRole", Seed(_owner_role(role)))
            for role in ("finance", "receptionist", "clinic_admin")
        ),
    ),
    "Account": Seed(_rows({"code": "receivable"})),
    "JournalEntry": Seed(
        lambda c: (
            {
                "source_kind": "invoice",
                "source_id": c.ref("billing.Invoice"),
                "content_hash": BYTES32,
                "state": "proposed",
            },
        )
    ),
    "JournalLine": Seed(_rows({"side": "debit", "amount_minor": 15000})),
    "PractitionerPayoutRule": Seed(
        lambda c: ({"kind": "percent", "value": 40, "practitioner_id": c.physician},)
    ),
    "InvoiceTerms": Seed(_rows({"net_minor": 15000})),
    "JournalSettlement": Seed(
        _rows({"amount_minor": 5000, "kind": "pix", "account": "bank"})
    ),
    # A partial payment: the invoice stays open (no legacy settlement pays it).
    "SettlementAllocation": Seed(_rows({"amount_minor": 5000})),
    "Refund": Seed(lambda c: ({"amount_minor": 1000, "requested_by_id": c.owner},)),
    "Payout": Seed(
        lambda c: (
            {
                "amount_minor": 6000,
                "requested_by_id": c.owner,
                "practitioner_id": c.physician,
            },
        )
    ),
    "CashSession": Seed(_rows({"counted_minor": 0})),
    "CashSessionEvent": Seed(
        adopt="the session's AFTER trigger writes its opening event"
    ),
    "PspPolicy": Seed(),
    "PspIntent": Seed(
        lambda c: (
            {
                "capability_version_id": c.ref("providers.CapabilityVersion", 1),
                "requested_by_id": c.owner,
                "provider": "synthetic",
                "kind": "charge",
                "target_id": c.ref("billing.Invoice"),
                "method": "pix",
                "amount_minor": 15000,
                "expires_at": c.future,
            },
        )
    ),
    "PspBinding": Seed(_rows({"provider": "synthetic", "provider_reference": "syn-1"})),
    "PspEvidence": Seed(
        _rows(
            {
                "provider": "synthetic",
                "event_id": "syn-evt-1",
                "source": "webhook",
                "status": "paid",
                "decision": "accepted",
                "amount_minor": 15000,
                "currency": "BRL",
            }
        )
    ),
    "PspReceipt": Seed(_rows({"amount_minor": 5000})),
    "FiscalConfiguration": Seed(
        _rows(
            {
                "legal_entity": {"name": "Synthetic Clinic Ltda"},
                "provider": "synthetic",
                "municipality_code": "3550308",
                "service_code": "04.01",
                "tax_regime": "simples_nacional",
                "ibs_cbs_applicability": "not_applicable",
                "tax_fields": {"iss_rate_bp": 200},
                "review_kind": "synthetic",
                "cancellation_window_seconds": 86400,
            }
        )
    ),
    "FiscalDocument": Seed(
        _rows(
            {
                "provider": "synthetic",
                "municipality_code": "3550308",
                "service_code": "04.01",
                "tax_fields": {"iss_rate_bp": 200},
                "amount_minor": 15000,
                "rehearsal": True,
                "status": "draft",
                "create_fingerprint": BYTES32,
                "dps_id": "SYNTHETIC-DPS-0001",
                "request_xml": b"<synthetic/>",
                "rejection_code": "",
            }
        )
    ),
    "FiscalTransition": Seed(
        lambda c: (
            {"from_status": "draft", "to_status": "submitted", "actor_id": c.owner},
        )
    ),
}

INSURANCE: Final[dict[str, Seed]] = {
    "Payer": Seed(_rows({"ans_registry": "000000", "name": "Synthetic Payer"})),
    "PayerContract": Seed(
        lambda c: (
            {
                "version": 1,
                "starts_on": c.now.date(),
                "billing_identity": {"code": "SYNTHETIC"},
                "fingerprint": DIGEST,
            },
        )
    ),
    "PriceTable": Seed(_rows({"version": 1, "prices": {"10101012": 15000}})),
    "TussTerm": Seed(
        _rows(
            {
                "component_version": "synthetic-1",
                "source_sha256": DIGEST,
                "table_code": "22",
                "code": "10101012",
                "description": "Synthetic consultation term",
            }
        )
    ),
    "InsuranceMembership": Seed(
        lambda c: (
            {
                "details": {"card": "SYNTHETIC-CARD"},
                "valid_from": c.now.date(),
                "registration_id": c.ref("intake.InsuranceMembership"),
            },
        )
    ),
    "Guide": Seed(
        _rows(
            {
                "kind": "consultation",
                "number": "SYN-G-0001",
                "facts": {"synthetic": True},
                "total_minor": 15000,
            }
        )
    ),
    "Batch": Seed(
        _rows(
            {
                "number": "SYN-B-0001",
                "guide_ids": [],
                "versions": [],
                "xml": "<synthetic/>",
                "content_sha256": DIGEST,
            }
        )
    ),
    "BatchGuide": Seed(),
    "Submission": Seed(_rows({"channel": "portal", "content_sha256": DIGEST})),
    "Return": Seed(_rows({"source_sha256": DIGEST, "xml": b"<synthetic-return/>"})),
    "Glosa": Seed(
        _rows(
            {
                "sequence": 1,
                "charged_minor": 15000,
                "accepted_minor": 10000,
                "denied_minor": 5000,
                "code": "1801",
                "detail": {"synthetic": True},
            }
        )
    ),
    "Appeal": Seed(_rows({"body": "Synthetic appeal.", "content_sha256": DIGEST})),
    "ClinicalApproval": Seed(
        lambda c: ({"content_sha256": DIGEST, "approved_by_id": c.physician},),
        actor="physician",
    ),
    "PayerRequest": Seed(
        _rows({"kind": "eligibility", "evidence": {"synthetic": True}})
    ),
    "PaymentMatch": Seed(_rows({"amount_minor": 10000})),
    "CommandReceipt": Seed(_rows({"kind": "guide", "next_state": "draft"})),
}

INVENTORY: Final[dict[str, Seed]] = {
    "Location": Seed(_rows({"name": "Synthetic storage"})),
    "Item": Seed(_rows({"name": "Synthetic gloves"})),
    "Supplier": Seed(
        _rows({"name": "Synthetic Supplier", "contact": "sup@example.invalid"})
    ),
    "Batch": Seed(lambda c: ({"lot": "SYN-LOT-1", "expires_on": (c.future).date()},)),
    "MinimumStock": Seed(_rows({"quantity": 10})),
    "Equipment": Seed(_rows({"name": "Synthetic monitor", "serial": "SYN-SN-1"})),
    "Calibration": Seed(
        lambda c: (
            {
                "performed_on": c.now.date(),
                "due_on": (c.now + timedelta(days=365)).date(),
                "outcome": "pass",
            },
        )
    ),
    "Maintenance": Seed(
        lambda c: (
            {
                "performed_on": c.now.date(),
                "due_on": (c.now + timedelta(days=180)).date(),
                "outcome": "pass",
            },
        )
    ),
    "PurchaseBudget": Seed(lambda c: ({"amount_minor": 50000, "approved_at": c.now},)),
    "PurchaseRequest": Seed(_rows({"quantity": 5, "amount_minor": 2500})),
    "StockMovement": Seed(_rows({"kind": "receipt", "quantity": 5})),
    "CommandReceipt": Seed(
        after=("inventory.StockMovement",),
        rows=lambda c: (
            {
                "operation": "receive",
                "fingerprint": DIGEST,
                "object_type": "stock_movement",
                "object_id": c.ref("inventory.StockMovement"),
            },
        ),
    ),
}

CRM: Final[dict[str, Seed]] = {
    "ReferralSource": Seed(_rows({"code": "synthetic-web", "name": "Synthetic web"})),
    "Lead": Seed(
        _rows(
            {
                "name": "Synthetic Prospect",
                "email": "prospect@example.invalid",
                "state": "active",
            }
        )
    ),
    "PublicBookingProfile": Seed(
        _rows({"code": "synthetic-clinic", "description": "Synthetic booking."})
    ),
    "MarketingPreference": Seed(
        _rows({"channel": "email", "opted_in": False, "revision": 1})
    ),
    # A campaign mid-send on the approved neutral marketing template.
    "Campaign": Seed(
        lambda c: (
            {
                "title": "Synthetic neutral campaign",
                "template_id": c.ref("comms.TemplateVersion", 1),
                "state": "sending",
                "revision": 3,
            },
        )
    ),
    "CampaignSend": Seed(
        lambda c: (
            {"message_id": c.ref("comms.Message", 2), "enrollment_id": c.enrollment},
        )
    ),
    "SatisfactionSurvey": Seed(_rows({"score": 5})),
}


def _metric_window(c: SeedContext) -> Row:
    """Build a valid no-show metric query over the week before the stage."""
    end = c.now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=7)
    stamp = "%Y-%m-%dT%H:%M:%S.%fZ"
    return {
        "descriptor": {
            "metric_id": "no_show",
            "version": 1,
            "dimensions": [],
            "filters": {},
            "window": {"start": start.strftime(stamp), "end": end.strftime(stamp)},
            "tz": "America/Sao_Paulo",
        },
        "window_start": start,
        "window_end": end,
    }


ANALYTICS: Final[dict[str, Seed]] = {
    "MetricDefinitionVersion": Seed(
        _rows({"metric_id": "no_show", "specification": {"synthetic": True}})
    ),
    "SavedReport": Seed(lambda c: (_metric_window(c),)),
    "ReadModelRefresh": Seed(
        lambda c: (
            {
                **_metric_window(c),
                "authorization_version": DIGEST,
                "rows": [],
                "quality": "complete",
            },
        )
    ),
    "AnalystReview": Seed(
        lambda c: (
            {
                **_metric_window(c),
                "invocation_id": c.ref("ai.AIInvocation", 1),
                "authorization_version": DIGEST,
                "decision": "rejected",
            },
        )
    ),
}

SUBSCRIPTIONS: Final[dict[str, Seed]] = {
    "Plan": Seed(_rows({"code": "intelligence_usage", "version": 9})),
    "Entitlement": Seed(_rows({"meter": "messages", "quota": 1000})),
    "Subscription": Seed(adopt="0001 opens one trial subscription per organization"),
    "SubscriptionEvent": Seed(
        _rows(
            {
                "action": "trial_started",
                "from_state": "",
                "to_state": "trial",
                "revision": 1,
            }
        )
    ),
    "Seat": Seed(adopt="0001 seats every existing clinic membership"),
    "UsageEvent": Seed(_rows({"meter": "messages", "quantity": 3})),
    "UsageRollup": Seed(_rows({"meter": "messages", "quantity": 3})),
}

INTEROP: Final[dict[str, Seed]] = {
    "ImportJob": Seed(
        _rows(
            {
                "source_kind": "csv",
                "size_bytes": 128,
                "source_sha256": DIGEST,
                "fingerprint": DIGEST,
            }
        )
    ),
    "ImportMapping": Seed(
        _rows(
            {
                "code": "synthetic-patients",
                "version": 1,
                "source_kind": "csv",
                "document": {"columns": {"nome": "full_name"}},
                "document_sha256": DIGEST,
            }
        )
    ),
    "ImportFile": Seed(
        _rows(
            {
                "role": "source",
                "sha256": DIGEST,
                "size_bytes": 128,
                "media_type": "text/csv",
                "scan_verdict": "not_applicable",
                "data": b"nome\nSynthetic Upgrade Persona\n",
            }
        )
    ),
    "ImportTransition": Seed(_rows({"sequence": 1, "state": "uploaded"})),
    "ImportBatch": Seed(
        _rows(
            {
                "sequence": 1,
                "dataset": "patients",
                "first_row": 1,
                "last_row": 1,
                "rows_sha256": DIGEST,
            }
        )
    ),
    "ImportedRecord": Seed(
        _rows(
            {
                "dataset": "patients",
                "row_number": 1,
                "kind": "patient",
                "source_record_id": "SYN-ROW-1",
                "content_sha256": DIGEST,
            }
        )
    ),
    "HiddenEnrollment": Seed(lambda c: ({"enrollment_id": c.companion_enrollment},)),
    "ApiClient": Seed(
        _rows(
            {
                "name": "Synthetic partner",
                "scopes": ["appointments:read"],
                "rate_limit_per_minute": 60,
            }
        ),
    ),
    "ApiClientSecret": Seed(_rows({"version": 1, "secret_digest": DIGEST})),
    "ApiAccessToken": Seed(
        _rows({"token_digest": DIGEST, "scopes": ["appointments:read"]})
    ),
    "ApiWriteReceipt": Seed(
        _rows(
            {
                "idempotency_key": "synthetic-key",
                "operation": "webhook_create",
                "request_digest": DIGEST,
                "response_status": 201,
                "response_body": {"synthetic": True},
            }
        )
    ),
    "WebhookSubscription": Seed(
        _rows(
            {
                "url": "https://example.invalid/webhook",
                "events": ["appointment.created"],
            }
        )
    ),
    "WebhookDelivery": Seed(
        _rows(
            {
                "event_type": "appointment.created",
                "subject_state": "scheduled",
                "payload_digest": DIGEST,
            }
        )
    ),
    "WebhookAttempt": Seed(_rows({"attempt": 1, "outcome": "delivered"})),
    "OrganizationExport": Seed(),
    "OrganizationExportPart": Seed(
        _rows({"part": 1, "sha256": DIGEST, "size_bytes": 1, "data": b"synthetic"})
    ),
    "RndsSubmission": Seed(
        _rows(
            {
                "request_digest": DIGEST,
                "environment": "homologation",
                "profile": "BRResultadoExameLaboratorial-1.1",
                "bundle": b'{"resourceType":"Bundle"}',
                "bundle_sha256": DIGEST,
            }
        )
    ),
}

DOMAIN_SEEDS: Final[dict[str, dict[str, Seed]]] = {
    "ai": AI,
    "analytics": ANALYTICS,
    "billing": BILLING,
    "comms": COMMS,
    "consent": CONSENT,
    "crm": CRM,
    "ehr": EHR,
    "identity": IDENTITY,
    "insurance": INSURANCE,
    "intake": INTAKE,
    "interop": INTEROP,
    "inventory": INVENTORY,
    "orders": ORDERS,
    "prescription": PRESCRIPTION,
    "providers": PROVIDERS,
    "retention": RETENTION,
    "scheduling": SCHEDULING,
    "scribe": SCRIBE,
    "subscriptions": SUBSCRIPTIONS,
    "teleconsult": TELECONSULT,
    "workflows": WORKFLOWS,
}
