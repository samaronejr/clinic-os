"""Executable protocol oracles for scheduling and versioned workflow SQL."""

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
}


def assert_executable_protocol_oracles(observed: dict[str, list[str]]) -> None:
    """Keep every live protocol member bound to its executed domain matrix."""
    assert observed == PROTOCOL_ORACLES
    assert all(case in GUARDS for cases in SCHEDULING_CASES.values() for case in cases)
    assert all(callable(case) for case in SQL_CASES.values())
    assert callable(test_effective_interval_is_actor_invariant)
    assert callable(test_bundle_issuance_requires_the_owner_login_for_every_role)
    assert callable(test_database_trigger_decides_every_status_pair_by_actor)
    assert callable(test_guards_decide_by_the_exact_permission_not_a_sibling)
    assert callable(test_python_guard_decides_each_action_by_its_pinned_terms)
    assert callable(test_every_service_booking_status_change_keeps_the_todo_21_guard)
