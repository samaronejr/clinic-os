"""Executable actor-authority oracles for the todo 50 data-subject request SQL."""

from __future__ import annotations

from typing import Final

# Both staff paths read the actor setting and delegate the active-account
# check to clinic_app.has_permission. The oracle refuses every other staff
# role, an inactive manager at the row guard and at the resolver, and admits
# the same manager again once the account is active.
ACTOR_PROTOCOL_ORACLES: Final = dict.fromkeys(
    (
        "clinic_app.data_request_guard()",
        "clinic_app.transition_data_request(uuid,uuid,integer,text,text)",
    ),
    (
        "patients.test_dsar."
        "test_only_management_roles_answer_and_other_roles_match_unknown_requests",
    ),
)
