"""Executable authority protocols for the new public SQL subscription readers."""

from subscriptions.test_authority import (
    test_sql_scope_and_commands_check_every_role_and_current_actor,
)
from subscriptions.test_contracts import (
    test_subscription_sql_readers_refuse_an_inactive_actor,
)
from subscriptions.test_state import (
    test_trial_active_failure_grace_restriction_cure_and_event_replay,
)

CASES = {
    case.__name__: case
    for case in (
        test_subscription_sql_readers_refuse_an_inactive_actor,
        test_sql_scope_and_commands_check_every_role_and_current_actor,
        test_trial_active_failure_grace_restriction_cure_and_event_replay,
    )
}
SQL_PROTOCOL_ORACLES = {
    "clinic_app.subscription_scope": (
        "test_sql_scope_and_commands_check_every_role_and_current_actor",
    ),
    "clinic_app.subscription_seats": (
        "test_sql_scope_and_commands_check_every_role_and_current_actor",
    ),
    "clinic_app.subscription_allowance": (
        "test_subscription_sql_readers_refuse_an_inactive_actor",
    ),
    "clinic_app.subscription_entitled": (
        "test_subscription_sql_readers_refuse_an_inactive_actor",
    ),
    "clinic_app.subscription_command": (
        "test_sql_scope_and_commands_check_every_role_and_current_actor",
    ),
    "clinic_app.subscription_platform_event": (
        "test_trial_active_failure_grace_restriction_cure_and_event_replay",
    ),
}
