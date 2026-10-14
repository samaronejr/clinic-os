"""Executable protocol oracles for scheduling, workflows and representation."""

from patients import test_delegates
from scheduling.test_resource_permission_identity import GUARDS
from scheduling.test_resource_role_matrix import (
    test_effective_interval_is_actor_invariant,
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
    "clinic_app.scheduling_effective_interval": [
        "test_effective_interval_is_actor_invariant"
    ],
    **{name: [name] for name in SQL_CASES},
    "clinic_app.workflows_bundle_selection_guard": [
        "test_bundle_issuance_requires_the_owner_login_for_every_role"
    ],
    **{name: list(DELEGATE_CASES) for name in DELEGATE_PROTOCOLS},
}


def assert_executable_protocol_oracles(observed: dict[str, list[str]]) -> None:
    """Keep every live protocol member bound to its executed domain matrix."""
    assert observed == PROTOCOL_ORACLES
    assert all(case in GUARDS for cases in SCHEDULING_CASES.values() for case in cases)
    assert all(callable(case) for case in SQL_CASES.values())
    assert callable(test_effective_interval_is_actor_invariant)
    assert callable(test_bundle_issuance_requires_the_owner_login_for_every_role)
    assert all(callable(case) for case in DELEGATE_CASES.values())
