"""Executable protocol oracles for scheduling, workflows and teleconsult."""

from identity.test_permission_parity import (
    test_sql_guards_allow_and_deny_all_staff_roles,
)
from patients import test_delegates
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
PROTOCOL_ORACLES = {
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
}


def assert_executable_protocol_oracles(observed: dict[str, list[str]]) -> None:
    """Keep every live protocol member bound to its executed domain matrix."""
    assert observed == PROTOCOL_ORACLES
    assert all(case in GUARDS for cases in SCHEDULING_CASES.values() for case in cases)
    assert all(callable(case) for case in SQL_CASES.values())
    assert callable(test_effective_interval_is_actor_invariant)
    assert callable(test_bundle_issuance_requires_the_owner_login_for_every_role)
    assert all(callable(case) for case in DELEGATE_CASES.values())
    assert callable(test_database_trigger_decides_every_status_pair_by_actor)
    assert callable(test_guards_decide_by_the_exact_permission_not_a_sibling)
    assert callable(test_python_guard_decides_each_action_by_its_pinned_terms)
    assert callable(test_every_service_booking_status_change_keeps_the_todo_21_guard)
    assert callable(test_sql_guards_allow_and_deny_all_staff_roles)
    assert callable(
        test_two_patient_sessions_and_physician_keep_independent_mode_receipts
    )
    assert callable(test_second_valid_session_cannot_supply_or_rebind_a_receipt)
    assert callable(
        test_concurrent_real_sql_mode_calls_keep_the_actual_winning_authority
    )
