"""Executable protocol oracles for scheduling, workflows and teleconsult."""

from ai import (
    test_agent_execution,
    test_agent_guards,
    test_agent_staff_authority,
    test_revenue_cycle,
    test_vote_attestation,
)
from ai import test_controls as ai_controls
from ai import test_posture as ai_posture
from ai.care_oracles import CASES as CARE_CASES
from ai.care_oracles import SQL_PROTOCOL_ORACLES as CARE_PROTOCOLS
from ai.evaluation_oracles import CASES as EVALUATION_CASES
from ai.evaluation_oracles import SQL_PROTOCOL_ORACLES as EVALUATION_PROTOCOLS
from ai.extraction_oracles import CASES as EXTRACTION_CASES
from ai.extraction_oracles import SQL_PROTOCOL_ORACLES as EXTRACTION_PROTOCOLS
from ai.front_desk_oracles import CASES as FRONT_DESK_CASES
from ai.front_desk_oracles import SQL_PROTOCOL_ORACLES as FRONT_DESK_PROTOCOLS
from analytics.sql_oracles import CASES as ANALYTICS_CASES
from analytics.sql_oracles import SQL_PROTOCOL_ORACLES as ANALYTICS_PROTOCOLS
from billing.fiscal_oracles import CASES as FISCAL_CASES
from billing.fiscal_oracles import SQL_PROTOCOL_ORACLES as FISCAL_PROTOCOLS
from billing.journal_oracles import CASES as BILLING_CASES
from billing.journal_oracles import SQL_PROTOCOL_ORACLES as BILLING_PROTOCOLS
from billing.psp_oracles import CASES as PSP_CASES
from billing.psp_oracles import SQL_PROTOCOL_ORACLES as PSP_PROTOCOLS
from comms import test_portal_messages
from crm.sql_oracles import CASES as CRM_CASES
from crm.sql_oracles import SQL_PROTOCOL_ORACLES as CRM_PROTOCOLS
from ehr.test_note_grant_lifecycle import (
    test_grant_and_recipient_loss_hold_the_same_transaction_fence,
    test_ineligible_recipient_cannot_receive_note_grant,
    test_recipient_loss_rollback_preserves_the_existing_grant,
    test_recipient_loss_terminally_revokes_existing_note_grant,
)
from ehr.test_prior_notes import test_copy_requires_review_at_the_current_revision
from ehr.test_sql_protocols import (
    test_longitudinal_functions_keep_exact_execute_privileges,
    test_longitudinal_sql_uses_current_role_and_actor,
    test_medication_guard_uses_current_role_and_actor,
    test_note_sql_rechecks_grants_scope_and_active_actor,
    test_projection_worker_refuses_every_ambient_authority,
)
from ehr.test_timeline import (
    test_concurrent_edits_and_duplicate_workers_preserve_every_source,
    test_projection_is_after_commit_and_replay_safe,
    test_restricted_note_does_not_leak_a_timeline_count,
)
from identity.enterprise_sql_oracles import CASES as ENTERPRISE_CASES
from identity.enterprise_sql_oracles import SQL_PROTOCOL_ORACLES as ENTERPRISE_PROTOCOLS
from identity.inventory_authority_oracle import (
    INVENTORY_CASES,
    INVENTORY_SQL_ORACLES,
)
from identity.test_permission_parity import (
    test_sql_guards_allow_and_deny_all_staff_roles,
)
from identity.test_reception_sql_principal import test_reception_display_device_contract
from insurance.sql_oracles import CASES as INSURANCE_CASES
from insurance.sql_oracles import SQL_PROTOCOL_ORACLES as INSURANCE_PROTOCOLS
from orders import test_patient_downloads
from orders.sql_oracles import CASES as ORDERS_CASES
from orders.sql_oracles import SQL_PROTOCOL_ORACLES as ORDERS_PROTOCOLS
from patients import test_delegates, test_dsar, test_portal_reads
from patients.duplicate_authority_cases import (
    SQL_PROTOCOL_ORACLES as DUPLICATE_SQL_ORACLES,
)
from patients.duplicate_authority_cases import (
    test_merge_authority_checks_every_stored_role_and_exact_live_session,
)
from patients.duplicate_forward_cases import (
    test_merge_recreates_forward_rows_with_links_and_fresh_access,
)
from patients.duplicate_signal_cases import (
    test_forward_waitlist_keeps_integer_source_and_replacement_links,
)
from patients.test_duplicates import test_new_charge_for_an_aliased_identity_is_rejected
from prescription.test_catalog_guards import (
    test_favorite_trigger_binds_the_stored_physician_role,
    test_renewal_trigger_requires_source_care_for_every_role,
    test_signature_class_trigger_rejects_rebinding,
)
from prescription.test_document_kinds import (
    test_current_document_sql_guard_rechecks_authority_and_version,
)
from prescription.test_sncr_adapter import (
    test_issuance_trigger_binds_the_stored_physician_role,
)
from renewal import test_patient_service_booking as service_booking
from retention.sql_oracles import CASES as RETENTION_CASES
from retention.sql_oracles import SQL_PROTOCOL_ORACLES as RETENTION_PROTOCOLS
from scheduling.test_appointment_states import (
    test_database_trigger_decides_every_status_pair_by_actor,
    test_guards_decide_by_the_exact_permission_not_a_sibling,
    test_python_guard_decides_each_action_by_its_pinned_terms,
)
from scheduling.test_lifecycle_service_bookings import (
    test_every_service_booking_status_change_keeps_the_todo_21_guard,
)
from scheduling.test_resource_permission_identity import GUARDS
from scheduling.test_resource_role_matrix import (
    test_effective_interval_is_actor_invariant,
)
from scribe import test_actor_readers as scribe_actor
from scribe import test_draft_authority as scribe_draft_authority
from scribe import test_draft_runtime as scribe_draft_runtime
from scribe import test_ingest as scribe_ingest
from scribe import test_refusals as scribe_refusals
from scribe import test_room_capture as scribe_room
from scribe import test_transcript_concurrency as scribe_transcript_native
from scribe import test_transcript_runtime as scribe_transcript
from subscriptions.sql_oracles import CASES as SUBSCRIPTIONS_CASES
from subscriptions.sql_oracles import SQL_PROTOCOL_ORACLES as SUBSCRIPTIONS_PROTOCOLS
from teleconsult.test_mode_concurrency import (
    test_concurrent_real_sql_mode_calls_keep_the_actual_winning_authority,
)
from teleconsult.test_mode_transition import (
    test_second_valid_session_cannot_supply_or_rebind_a_receipt,
    test_two_patient_sessions_and_physician_keep_independent_mode_receipts,
)
from workflows.test_bundle_issuer import (
    test_bundle_issuance_requires_the_owner_login_for_every_role,
)
from workflows.test_sql_authority import SQL_CASES

# Task 50: patient service booking is executed by its own contract suite.
SERVICE_BOOKING_CASES = {
    case.__name__: case
    for case in (
        service_booking.test_patient_sees_only_active_services_of_the_session_clinic,
        service_booking.test_service_slots_bind_duration_buffers_and_the_free_room,
        service_booking.test_service_without_a_free_required_resource_offers_no_slot,
        service_booking.test_two_patients_racing_for_the_last_room_get_one_booking,
        service_booking.test_booking_screen_takes_the_service_choice_in_the_post_body,
    )
}
SCHEDULING_CASES = {
    "clinic_app.scheduling_capacity_guard": [
        "capacity_insert_trigger",
        "capacity_update_trigger",
    ],
    "clinic_app.scheduling_definition_guard": ["definition_trigger"],
    "clinic_app.scheduling_generated_block_guard": ["generated_block_trigger"],
    "clinic_app.scheduling_service_practitioners": ["projection"],
}
DELEGATE_CASES = {
    case.__name__: case
    for case in (
        test_delegates.test_delegate_sm_verification_idempotency_and_stale_commands,
        test_delegates.test_delegate_scope_is_stored_and_cannot_widen_or_cross_clinic,
        test_delegates.test_restricted_records_are_hidden_in_database_and_unknown_denials_match,
        test_delegates.test_delegate_consent_keeps_exact_purpose_and_truthful_authority,
        test_delegates.test_revoke_and_expiry_deny_next_request_and_renewal_stays_pending,
    )
}
DELEGATE_PROTOCOLS = frozenset(
    {
        "clinic_app.consent_delegate_guard",
        "clinic_app.current_patient_authority",
        "clinic_app.delegate_audit",
        "clinic_app.delegate_audit_scope",
        "clinic_app.delegate_confidentiality_guard",
        "clinic_app.delegate_grantor",
        "clinic_app.delegate_guard",
        "clinic_app.delegate_now",
        "clinic_app.delegate_purpose_allowed",
        "clinic_app.delegate_recovery_identity",
        "clinic_app.delegate_session_guard",
        "clinic_app.delegate_session_status",
        "clinic_app.delegate_staff",
        "clinic_app.patient_authority",
        "clinic_app.patient_identity",
        "clinic_app.patient_is_delegate",
        "clinic_app.renew_delegate_grant",
        "clinic_app.request_delegate_grant",
        "clinic_app.switch_patient_delegate",
        "clinic_app.transition_delegate_grant",
    }
)
# Task 50: every data-subject request function is executed by the whole
# native DSAR matrix; portal reads and result downloads by their own cases.
DATA_REQUEST_CASES = {
    name: getattr(test_dsar, name)
    for name in dir(test_dsar)
    if name.startswith("test_")
}
DATA_REQUEST_PROTOCOLS = frozenset(
    {
        "clinic_app.data_request_audit",
        "clinic_app.data_request_audit_scope",
        "clinic_app.data_request_close_task",
        "clinic_app.data_request_emit_task",
        "clinic_app.data_request_guard",
        "clinic_app.data_request_now",
        "clinic_app.data_request_task_close_valid",
        "clinic_app.data_request_task_valid",
        "clinic_app.data_request_withdrawn",
        "clinic_app.patient_data_requests",
        "clinic_app.submit_data_request",
        "clinic_app.transition_data_request",
    }
)
# Task 50 portal messages: every patient-side resolver, the audit binding and the
# message guard's portal branch are executed by the whole native matrix.
PORTAL_MESSAGE_CASES = {
    name: getattr(test_portal_messages, name)
    for name in dir(test_portal_messages)
    if name.startswith("test_")
}
PORTAL_MESSAGE_PROTOCOLS = frozenset(
    {
        "clinic_app.comms_message_guard_v1",
        "clinic_app.comms_patient_audit",
        "clinic_app.comms_patient_audit_scope",
        "clinic_app.comms_patient_conversations",
        "clinic_app.comms_patient_messages",
        "clinic_app.comms_patient_reply",
        "clinic_app.comms_patient_scope",
    }
)
PORTAL_CASES = {
    case.__name__: case
    for case in (
        test_portal_reads.test_delegate_options_list_only_active_verified_grants_of_the_original_identity,
        test_portal_reads.test_channel_preferences_summarize_channels_without_destinations,
        test_patient_downloads.test_released_source_downloads_exact_bytes_with_one_session_bound_event,
        test_patient_downloads.test_unreleased_unknown_and_staff_contexts_are_one_denial,
    )
}
_DOWNLOAD_CASES = [
    "test_released_source_downloads_exact_bytes_with_one_session_bound_event",
    "test_unreleased_unknown_and_staff_contexts_are_one_denial",
]
PORTAL_PROTOCOLS = {
    "clinic_app.patient_delegate_options": [
        "test_delegate_options_list_only_active_verified_grants_of_the_original_identity"
    ],
    "clinic_app.patient_channel_preferences": [
        "test_channel_preferences_summarize_channels_without_destinations"
    ],
    "clinic_app.orders_patient_download_scope": _DOWNLOAD_CASES,
    "clinic_app.orders_patient_download_audit": _DOWNLOAD_CASES,
}
EHR_CASES = {
    case.__name__: case
    for case in (
        test_grant_and_recipient_loss_hold_the_same_transaction_fence,
        test_ineligible_recipient_cannot_receive_note_grant,
        test_recipient_loss_rollback_preserves_the_existing_grant,
        test_recipient_loss_terminally_revokes_existing_note_grant,
        test_copy_requires_review_at_the_current_revision,
        test_longitudinal_functions_keep_exact_execute_privileges,
        test_longitudinal_sql_uses_current_role_and_actor,
        test_medication_guard_uses_current_role_and_actor,
        test_note_sql_rechecks_grants_scope_and_active_actor,
        test_projection_worker_refuses_every_ambient_authority,
        test_concurrent_edits_and_duplicate_workers_preserve_every_source,
        test_projection_is_after_commit_and_replay_safe,
        test_restricted_note_does_not_leak_a_timeline_count,
    )
}
EHR_PROTOCOLS = {
    "clinic_app.ehr_patient_permission": [
        "test_longitudinal_sql_uses_current_role_and_actor"
    ],
    "clinic_app.ehr_longitudinal_guard": [
        "test_longitudinal_sql_uses_current_role_and_actor"
    ],
    "clinic_app.ehr_medicationstatement_guard": [
        "test_medication_guard_uses_current_role_and_actor"
    ],
    "clinic_app.ehr_observation_guard": [
        "test_longitudinal_sql_uses_current_role_and_actor"
    ],
    "clinic_app.ehr_note_author": [
        "test_note_sql_rechecks_grants_scope_and_active_actor"
    ],
    "clinic_app.ehr_note_grantee": [
        "test_note_sql_rechecks_grants_scope_and_active_actor",
        "test_ineligible_recipient_cannot_receive_note_grant",
    ],
    "clinic_app.ehr_note_visible": [
        "test_note_sql_rechecks_grants_scope_and_active_actor",
        "test_recipient_loss_terminally_revokes_existing_note_grant",
        "test_restricted_note_does_not_leak_a_timeline_count",
    ],
    "clinic_app.ehr_note_guard": [
        "test_note_sql_rechecks_grants_scope_and_active_actor",
        "test_ineligible_recipient_cannot_receive_note_grant",
        "test_recipient_loss_terminally_revokes_existing_note_grant",
        "test_grant_and_recipient_loss_hold_the_same_transaction_fence",
        "test_recipient_loss_rollback_preserves_the_existing_grant",
        "test_copy_requires_review_at_the_current_revision",
    ],
    "clinic_app.ehr_copy_finalization_guard": [
        "test_copy_requires_review_at_the_current_revision"
    ],
    "clinic_app.ehr_timeline_enqueue": [
        "test_projection_is_after_commit_and_replay_safe",
        "test_concurrent_edits_and_duplicate_workers_preserve_every_source",
    ],
    "clinic_app.ehr_timeline_visible": [
        "test_note_sql_rechecks_grants_scope_and_active_actor",
        "test_restricted_note_does_not_leak_a_timeline_count",
    ],
    "clinic_app.ehr_timeline_due": [
        "test_projection_worker_refuses_every_ambient_authority",
        "test_projection_is_after_commit_and_replay_safe",
    ],
    "clinic_app.ehr_timeline_refresh": [
        "test_projection_worker_refuses_every_ambient_authority",
        "test_concurrent_edits_and_duplicate_workers_preserve_every_source",
    ],
}
EHR_PROTOCOLS = {
    name: [*cases, "test_longitudinal_functions_keep_exact_execute_privileges"]
    for name, cases in EHR_PROTOCOLS.items()
}
SCRIBE_PROTOCOL_ORACLES = {
    "clinic_app.scribe_note_author": [
        "test_note_scope_and_sql_readers_keep_assigned_and_current_authority",
    ],
    "clinic_app.scribe_note_visible": [
        "test_note_scope_and_sql_readers_keep_assigned_and_current_authority",
    ],
    "clinic_app.scribe_citation_allowed": [
        "test_generation_and_three_clinician_decisions_never_finalize_automatically",
        "test_raw_citation_and_history_guards_reject_foreign_references",
    ],
    "clinic_app.scribe_draft_guard": [
        "test_generation_and_three_clinician_decisions_never_finalize_automatically",
        "test_raw_citation_and_history_guards_reject_foreign_references",
        "test_raw_review_guard_enforces_applied_text_by_decision_kind",
        "test_exact_review_replay_is_read_only_and_changed_replay_is_refused",
    ],
    "clinic_app.scribe_asr_allowed": [
        "test_governed_completion_review_and_final_are_retained_successors",
        "test_ai_revocation_refuses_an_already_queued_invocation",
    ],
    "clinic_app.scribe_asr_consent_fence": [
        "test_ai_revocation_refuses_an_already_queued_invocation",
        "test_native_consent_publication_holds_asr_send_fence",
    ],
    "clinic_app.scribe_transcript_coverage": [
        "test_coverage_reader_rechecks_native_assignment_and_active_actor",
        "test_finalize_without_full_coverage_requires_explicit_disposition",
        "test_read_unknown_and_foreign_clinic_have_the_same_denial",
    ],
    "clinic_app.scribe_transcript_guard": [
        "test_governed_completion_review_and_final_are_retained_successors",
        "test_finalize_without_full_coverage_requires_explicit_disposition",
        "test_database_coverage_change_is_a_recoverable_transcript_conflict",
        "test_native_coverage_storage_has_per_version_and_recording_bounds",
    ],
    "clinic_app.scribe_assigned": [
        "test_native_capture_authority_refuses_inactive_actor",
        "test_outbox_stores_only_ciphertext_and_object_keys_are_not_authority",
    ],
    "clinic_app.scribe_consent": [
        "test_revoked_consent_stops_new_chunks_and_direct_sql",
    ],
    "clinic_app.scribe_consent_fence": [
        "test_revoked_consent_stops_new_chunks_and_direct_sql",
    ],
    "clinic_app.scribe_gaps": [
        "test_reordered_chunks_close_only_the_gap_they_fill",
        "test_resume_fences_old_epoch_and_commands_replay",
    ],
    "clinic_app.scribe_guard": [
        "test_native_capture_authority_refuses_inactive_actor",
        "test_resume_fences_old_epoch_and_commands_replay",
        "test_role_loss_refuses_every_action_without_session_or_domain_mutation",
    ],
    "clinic_app.scribe_room_capture_allowed": [
        "test_room_bits_require_explicit_live_capture_and_asr_stays_closed",
        "test_revoked_consent_stops_new_chunks_and_direct_sql",
    ],
    "clinic_app.scribe_room_cleanup": [
        "test_room_bits_require_explicit_live_capture_and_asr_stays_closed",
        "test_revoked_consent_stops_new_chunks_and_direct_sql",
    ],
    "clinic_app.scribe_room_guard": [
        "test_room_bits_require_explicit_live_capture_and_asr_stays_closed",
    ],
}

AGENT_PROTOCOL_ORACLES = [
    "test_agent_scope_and_approval_refusals_are_side_effect_free",
    "test_fabricated_step_up_and_double_approval_cannot_add_votes",
    "test_revoked_approver_blocks_after_approval",
    "test_superseded_source_publication_blocks_execution",
    "test_native_agent_decisions_use_the_displayed_digest_and_preserve_refusals",
]
# ai.0011 binds revenue ancestry in these resolvers; each oracle fails on 0010.
REVENUE_BOUND_ORACLES = [
    "test_sql_hides_revenue_rows_from_automation_staff_without_finance_automation",
    "test_sql_stops_counting_a_vote_once_its_approver_loses_finance_automation",
    "test_sql_refuses_a_forged_vote_without_finance_automation",
    "test_sql_accepts_only_tiss_manage_snapshots_on_revenue_votes",
    "test_sql_refuses_raw_revenue_state_changes_without_finance_automation",
    "test_inactive_actor_is_refused_by_revenue_sql_itself",
]
# ai.0012 vote writer, counted-vote rule and quorum guard, for an A3 reminder and
# an AI-06 draft; the forged-vote and raw-approval oracles fail on ai.0011.
VOTE_ORACLES = [
    "test_sql_refuses_a_forged_vote_from_the_entitled_approver",
    "test_sql_never_counts_a_vote_without_verified_step_up",
    "test_sql_refuses_raw_approval_without_current_verified_votes",
    "test_sql_refuses_execution_and_dispatch_of_an_approval_without_votes",
    "test_python_approval_still_records_one_counted_vote",
    "test_vote_writer_requires_this_approvers_fresh_verified_session",
    "test_vote_writer_refuses_an_inactive_approver",
    "test_proposals_without_a_declared_quorum_never_advance",
    "test_zero_vote_dispatch_needs_the_bound_machine_principal",
]

PROTOCOL_ORACLES = {
    **CRM_PROTOCOLS,
    **{name: list(cases) for name, cases in SUBSCRIPTIONS_PROTOCOLS.items()},
    **PSP_PROTOCOLS,
    **ANALYTICS_PROTOCOLS,
    "clinic_app.ai_revenue_ancestry": [
        *REVENUE_BOUND_ORACLES,
        "test_sql_refuses_revenue_policy_versions_without_publisher_finance_automation",
    ],
    "clinic_app.ai_revenue_row": [
        "test_inactive_actor_is_refused_by_revenue_sql_itself",
        "test_finance_scope_resolver_never_opens_another_capability_or_clinic",
        "test_human_digest_approval_creates_only_one_unsubmitted_administrative_draft",
    ],
    **RETENTION_PROTOCOLS,
    **SCRIBE_PROTOCOL_ORACLES,
    **DUPLICATE_SQL_ORACLES,
    **EXTRACTION_PROTOCOLS,
    **EVALUATION_PROTOCOLS,
    **CARE_PROTOCOLS,
    **FRONT_DESK_PROTOCOLS,
    "clinic_app.reception_display": ["test_reception_display_device_contract"],
    **EHR_PROTOCOLS,
    **dict.fromkeys(
        (
            "clinic_app.agent_action_payload",
            "clinic_app.agent_approval_current",
            "clinic_app.agent_audit",
            "clinic_app.agent_audit_scope",
            "clinic_app.agent_bound_snapshot",
            "clinic_app.agent_controls",
            "clinic_app.agent_mutation_lock",
            "clinic_app.agent_proposal_scope",
            "clinic_app.agent_record_guard",
            "clinic_app.agent_snapshot",
            "clinic_app.agent_source",
            "clinic_app.agent_transition",
            "clinic_app.identity_actor_permission",
            "clinic_app.identity_agent_grant_version",
        ),
        AGENT_PROTOCOL_ORACLES,
    ),
    "clinic_app.agent_controls": [*AGENT_PROTOCOL_ORACLES, *REVENUE_BOUND_ORACLES],
    **{
        name: [*AGENT_PROTOCOL_ORACLES, *REVENUE_BOUND_ORACLES, *VOTE_ORACLES]
        for name in (
            "clinic_app.agent_approval_current",
            "clinic_app.agent_record_guard",
        )
    },
    **dict.fromkeys(
        ("clinic_app.agent_record_approval", "clinic_app.agent_votes_current"),
        VOTE_ORACLES,
    ),
    "clinic_app.agent_staff": [
        "test_agent_scope_and_approval_refusals_are_side_effect_free",
        "test_fabricated_step_up_and_double_approval_cannot_add_votes",
        "test_revoked_approver_blocks_after_approval",
        "test_superseded_source_publication_blocks_execution",
        "test_native_agent_decisions_use_the_displayed_digest_and_preserve_refusals",
        "test_agent_staff_admits_only_automation_authority",
    ],
    "clinic_app.ai_staff": [
        "test_ai_sql_role_and_scope_decisions_follow_exact_permissions",
        "test_clinical_purposes_recheck_current_patient_authority",
    ],
    "clinic_app.configuration_guard": [
        "test_ai_policy_requires_organization_permission_in_every_clinic",
        "test_native_controls_execute_valid_invalid_stale_and_forbidden_requests",
    ],
    **SCHEDULING_CASES,
    "clinic_app.scheduling_capacity_guard": [
        *SCHEDULING_CASES["clinic_app.scheduling_capacity_guard"],
        "test_every_service_booking_status_change_keeps_the_todo_21_guard",
    ],
    "clinic_app.patient_service_slots": sorted(SERVICE_BOOKING_CASES),
    "clinic_app.scheduling_lifecycle_guard": [
        "test_database_trigger_decides_every_status_pair_by_actor",
        "test_guards_decide_by_the_exact_permission_not_a_sibling",
        "test_python_guard_decides_each_action_by_its_pinned_terms",
    ],
    "clinic_app.scheduling_effective_interval": [
        "test_effective_interval_is_actor_invariant"
    ],
    # A workflow-migration member another domain registry owns keeps that
    # registry's oracles (analytics_workflow_receipt: workflows 0009, todo 63).
    **{name: [name] for name in SQL_CASES if name not in ANALYTICS_PROTOCOLS},
    "clinic_app.workflows_bundle_selection_guard": [
        "test_bundle_issuance_requires_the_owner_login_for_every_role"
    ],
    **{name: list(DELEGATE_CASES) for name in DELEGATE_PROTOCOLS},
    **{name: sorted(DATA_REQUEST_CASES) for name in DATA_REQUEST_PROTOCOLS},
    **PORTAL_PROTOCOLS,
    **{name: sorted(PORTAL_MESSAGE_CASES) for name in PORTAL_MESSAGE_PROTOCOLS},
    "clinic_app.teleconsult_mode_transition_audit": [
        "test_sql_guards_allow_and_deny_all_staff_roles",
        "test_two_patient_sessions_and_physician_keep_independent_mode_receipts",
        "test_second_valid_session_cannot_supply_or_rebind_a_receipt",
        "test_concurrent_real_sql_mode_calls_keep_the_actual_winning_authority",
    ],
    **ORDERS_PROTOCOLS,
    **INVENTORY_SQL_ORACLES,
    **BILLING_PROTOCOLS,
    **FISCAL_PROTOCOLS,
    **INSURANCE_PROTOCOLS,
    "clinic_app.prescription_current_document_guard": [
        "test_current_document_sql_guard_rechecks_authority_and_version"
    ],
    "clinic_app.prescription_favorite_guard": [
        "test_favorite_trigger_binds_the_stored_physician_role"
    ],
    "clinic_app.prescription_signature_class_guard": [
        "test_signature_class_trigger_rejects_rebinding"
    ],
    "clinic_app.prescription_issuance_guard": [
        "test_issuance_trigger_binds_the_stored_physician_role"
    ],
    "clinic_app.prescription_draft_guard": [
        "test_renewal_trigger_requires_source_care_for_every_role"
    ],
    **ENTERPRISE_PROTOCOLS,
}


def assert_executable_protocol_oracles(observed: dict[str, list[str]]) -> None:
    """Keep every live protocol member bound to its executed domain matrix."""
    assert observed == PROTOCOL_ORACLES
    assert all(
        callable(CRM_CASES[name]) for names in CRM_PROTOCOLS.values() for name in names
    )
    assert all(
        callable(FISCAL_CASES[name])
        for names in FISCAL_PROTOCOLS.values()
        for name in names
    )
    assert all(
        callable(SUBSCRIPTIONS_CASES[name])
        for names in SUBSCRIPTIONS_PROTOCOLS.values()
        for name in names
    )
    assert all(
        callable(BILLING_CASES[name])
        for names in BILLING_PROTOCOLS.values()
        for name in names
    )
    assert all(
        callable(PSP_CASES[name]) for names in PSP_PROTOCOLS.values() for name in names
    )
    assert all(
        callable(INSURANCE_CASES[name])
        for names in INSURANCE_PROTOCOLS.values()
        for name in names
    )
    assert all(
        callable(ANALYTICS_CASES[name])
        for cases in ANALYTICS_PROTOCOLS.values()
        for name in cases
    )
    assert all(
        callable(RETENTION_CASES[name])
        for names in RETENTION_PROTOCOLS.values()
        for name in names
    )
    assert callable(test_reception_display_device_contract)
    assert all(case in GUARDS for cases in SCHEDULING_CASES.values() for case in cases)
    assert all(callable(case) for case in SERVICE_BOOKING_CASES.values())
    assert all(callable(case) for case in SQL_CASES.values())
    assert all(
        callable(INVENTORY_CASES[name])
        for names in INVENTORY_SQL_ORACLES.values()
        for name in names
    )
    assert callable(test_effective_interval_is_actor_invariant)
    assert callable(test_bundle_issuance_requires_the_owner_login_for_every_role)
    assert all(callable(case) for case in DELEGATE_CASES.values())
    assert all(callable(case) for case in DATA_REQUEST_CASES.values())
    assert all(callable(case) for case in PORTAL_MESSAGE_CASES.values())
    assert all(
        callable(PORTAL_CASES[name])
        for names in PORTAL_PROTOCOLS.values()
        for name in names
    )
    assert all(callable(case) for case in EHR_CASES.values())
    assert all(callable(case) for case in ORDERS_CASES.values())
    assert all(callable(case) for case in EXTRACTION_CASES.values())
    assert all(callable(case) for case in EVALUATION_CASES.values())
    assert all(callable(case) for case in CARE_CASES.values())
    assert all(
        name in CARE_CASES for cases in CARE_PROTOCOLS.values() for name in cases
    )
    assert all(callable(case) for case in FRONT_DESK_CASES.values())
    assert all(
        name in FRONT_DESK_CASES
        for cases in FRONT_DESK_PROTOCOLS.values()
        for name in cases
    )
    assert all(
        name in EVALUATION_CASES
        for cases in EVALUATION_PROTOCOLS.values()
        for name in cases
    )
    assert all(
        name in EXTRACTION_CASES
        for cases in EXTRACTION_PROTOCOLS.values()
        for name in cases
    )
    assert all(
        callable(ENTERPRISE_CASES[name])
        for names in ENTERPRISE_PROTOCOLS.values()
        for name in names
    )
    assert all(
        name in ORDERS_CASES for cases in ORDERS_PROTOCOLS.values() for name in cases
    )
    assert all(name in EHR_CASES for cases in EHR_PROTOCOLS.values() for name in cases)
    assert callable(test_database_trigger_decides_every_status_pair_by_actor)
    assert callable(test_guards_decide_by_the_exact_permission_not_a_sibling)
    assert callable(test_python_guard_decides_each_action_by_its_pinned_terms)
    assert callable(test_every_service_booking_status_change_keeps_the_todo_21_guard)
    duplicate_oracles = {
        oracle.__name__: oracle
        for oracle in (
            test_merge_authority_checks_every_stored_role_and_exact_live_session,
            test_merge_recreates_forward_rows_with_links_and_fresh_access,
            test_forward_waitlist_keeps_integer_source_and_replacement_links,
            test_new_charge_for_an_aliased_identity_is_rejected,
        )
    }
    assert all(
        callable(duplicate_oracles[name])
        for names in DUPLICATE_SQL_ORACLES.values()
        for name in names
    )
    assert callable(test_sql_guards_allow_and_deny_all_staff_roles)
    assert callable(test_current_document_sql_guard_rechecks_authority_and_version)
    assert callable(test_favorite_trigger_binds_the_stored_physician_role)
    assert callable(test_signature_class_trigger_rejects_rebinding)
    assert callable(test_issuance_trigger_binds_the_stored_physician_role)
    assert callable(test_renewal_trigger_requires_source_care_for_every_role)
    assert callable(
        test_two_patient_sessions_and_physician_keep_independent_mode_receipts
    )
    assert callable(test_second_valid_session_cannot_supply_or_rebind_a_receipt)
    assert callable(
        test_concurrent_real_sql_mode_calls_keep_the_actual_winning_authority
    )
    for module, names in (
        (test_revenue_cycle, PROTOCOL_ORACLES["clinic_app.ai_revenue_row"]),
        (test_revenue_cycle, PROTOCOL_ORACLES["clinic_app.ai_revenue_ancestry"]),
        (test_vote_attestation, VOTE_ORACLES),
        (
            scribe_draft_authority,
            (
                "test_note_scope_and_sql_readers_keep_assigned_and_current_authority",
                "test_raw_citation_and_history_guards_reject_foreign_references",
                "test_raw_review_guard_enforces_applied_text_by_decision_kind",
            ),
        ),
        (
            scribe_draft_runtime,
            (
                "test_generation_and_three_clinician_decisions_never_finalize_automatically",
                "test_exact_review_replay_is_read_only_and_changed_replay_is_refused",
            ),
        ),
        (
            test_agent_guards,
            (
                "test_agent_scope_and_approval_refusals_are_side_effect_free",
                "test_fabricated_step_up_and_double_approval_cannot_add_votes",
                "test_native_agent_decisions_use_the_displayed_digest_and_preserve_refusals",
            ),
        ),
        (
            test_agent_execution,
            (
                "test_revoked_approver_blocks_after_approval",
                "test_superseded_source_publication_blocks_execution",
            ),
        ),
        (
            test_agent_staff_authority,
            ("test_agent_staff_admits_only_automation_authority",),
        ),
        (ai_posture, PROTOCOL_ORACLES["clinic_app.ai_staff"]),
        (ai_controls, PROTOCOL_ORACLES["clinic_app.configuration_guard"]),
        (
            scribe_actor,
            ("test_native_capture_authority_refuses_inactive_actor",),
        ),
        (
            scribe_ingest,
            (
                "test_outbox_stores_only_ciphertext_and_object_keys_are_not_authority",
                "test_revoked_consent_stops_new_chunks_and_direct_sql",
                "test_reordered_chunks_close_only_the_gap_they_fill",
                "test_resume_fences_old_epoch_and_commands_replay",
            ),
        ),
        (
            scribe_refusals,
            ("test_role_loss_refuses_every_action_without_session_or_domain_mutation",),
        ),
        (
            scribe_room,
            ("test_room_bits_require_explicit_live_capture_and_asr_stays_closed",),
        ),
        (
            scribe_transcript,
            (
                "test_coverage_reader_rechecks_native_assignment_and_active_actor",
                "test_governed_completion_review_and_final_are_retained_successors",
                "test_ai_revocation_refuses_an_already_queued_invocation",
                "test_finalize_without_full_coverage_requires_explicit_disposition",
                "test_read_unknown_and_foreign_clinic_have_the_same_denial",
            ),
        ),
        (
            scribe_transcript_native,
            (
                "test_native_consent_publication_holds_asr_send_fence",
                "test_database_coverage_change_is_a_recoverable_transcript_conflict",
                "test_native_coverage_storage_has_per_version_and_recording_bounds",
            ),
        ),
    ):
        assert all(callable(getattr(module, name)) for name in names)
