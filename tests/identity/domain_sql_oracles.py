"""Executable protocol oracles for scheduling, workflows and teleconsult."""

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
    test_note_sql_rechecks_grants_scope_and_active_actor,
    test_projection_worker_refuses_every_ambient_authority,
)
from ehr.test_timeline import (
    test_concurrent_edits_and_duplicate_workers_preserve_every_source,
    test_projection_is_after_commit_and_replay_safe,
    test_restricted_note_does_not_leak_a_timeline_count,
)
from identity.test_permission_parity import (
    test_sql_guards_allow_and_deny_all_staff_roles,
)
from identity.test_reception_sql_principal import test_reception_display_device_contract
from orders.sql_oracles import CASES as ORDERS_CASES
from orders.sql_oracles import SQL_PROTOCOL_ORACLES as ORDERS_PROTOCOLS
from patients import test_delegates
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
from prescription.test_document_kinds import (
    test_current_document_sql_guard_rechecks_authority_and_version,
)
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
PROTOCOL_ORACLES = {
    **DUPLICATE_SQL_ORACLES,
    "clinic_app.reception_display": ["test_reception_display_device_contract"],
    **EHR_PROTOCOLS,
    **SCHEDULING_CASES,
    "clinic_app.scheduling_capacity_guard": [
        *SCHEDULING_CASES["clinic_app.scheduling_capacity_guard"],
        "test_every_service_booking_status_change_keeps_the_todo_21_guard",
    ],
    "clinic_app.scheduling_lifecycle_guard": [
        "test_database_trigger_decides_every_status_pair_by_actor",
        "test_guards_decide_by_the_exact_permission_not_a_sibling",
        "test_python_guard_decides_each_action_by_its_pinned_terms",
    ],
    "clinic_app.scheduling_effective_interval": [
        "test_effective_interval_is_actor_invariant"
    ],
    **{name: [name] for name in SQL_CASES},
    "clinic_app.workflows_bundle_selection_guard": [
        "test_bundle_issuance_requires_the_owner_login_for_every_role"
    ],
    **{name: list(DELEGATE_CASES) for name in DELEGATE_PROTOCOLS},
    "clinic_app.teleconsult_mode_transition_audit": [
        "test_sql_guards_allow_and_deny_all_staff_roles",
        "test_two_patient_sessions_and_physician_keep_independent_mode_receipts",
        "test_second_valid_session_cannot_supply_or_rebind_a_receipt",
        "test_concurrent_real_sql_mode_calls_keep_the_actual_winning_authority",
    ],
    **ORDERS_PROTOCOLS,
    "clinic_app.prescription_current_document_guard": [
        "test_current_document_sql_guard_rechecks_authority_and_version"
    ],
}


def assert_executable_protocol_oracles(observed: dict[str, list[str]]) -> None:
    """Keep every live protocol member bound to its executed domain matrix."""
    assert observed == PROTOCOL_ORACLES
    assert callable(test_reception_display_device_contract)
    assert all(case in GUARDS for cases in SCHEDULING_CASES.values() for case in cases)
    assert all(callable(case) for case in SQL_CASES.values())
    assert callable(test_effective_interval_is_actor_invariant)
    assert callable(test_bundle_issuance_requires_the_owner_login_for_every_role)
    assert all(callable(case) for case in DELEGATE_CASES.values())
    assert all(callable(case) for case in EHR_CASES.values())
    assert all(callable(case) for case in ORDERS_CASES.values())
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
    assert callable(
        test_two_patient_sessions_and_physician_keep_independent_mode_receipts
    )
    assert callable(test_second_valid_session_cannot_supply_or_rebind_a_receipt)
    assert callable(
        test_concurrent_real_sql_mode_calls_keep_the_actual_winning_authority
    )
