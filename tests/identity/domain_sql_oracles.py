"""Executable protocol oracles for scheduling, workflow and teleconsult SQL."""

from identity.test_permission_parity import (
    test_sql_guards_allow_and_deny_all_staff_roles,
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
PROTOCOL_ORACLES = {
    **SCHEDULING_CASES,
    "clinic_app.scheduling_effective_interval": [
        "test_effective_interval_is_actor_invariant"
    ],
    **{name: [name] for name in SQL_CASES},
    "clinic_app.workflows_bundle_selection_guard": [
        "test_bundle_issuance_requires_the_owner_login_for_every_role"
    ],
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
    assert callable(test_sql_guards_allow_and_deny_all_staff_roles)
    assert callable(
        test_two_patient_sessions_and_physician_keep_independent_mode_receipts
    )
    assert callable(test_second_valid_session_cannot_supply_or_rebind_a_receipt)
    assert callable(
        test_concurrent_real_sql_mode_calls_keep_the_actual_winning_authority
    )
