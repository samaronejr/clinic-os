"""Bind catalog-discovered care-coordination protocols to real authority tests."""

from ai import test_care_coordination

CASES = {
    case.__name__: case
    for case in (
        test_care_coordination.test_staff_approve_the_previewed_administrative_bundle,
        test_care_coordination.test_stale_proposals_are_rejected_after_the_plan_changes,
        test_care_coordination.test_clinical_action_without_clinician_approval_is_refused,
        test_care_coordination.test_database_guards_refuse_raw_forgery,
        test_care_coordination.test_care_sql_rechecks_inactive_and_foreign_actor,
    )
}
SQL_PROTOCOL_ORACLES = {
    name: list(CASES)
    for name in (
        "clinic_app.ai_care_guard",
        "clinic_app.ai_care_plan_current",
        "clinic_app.ai_care_staff",
        "clinic_app.ai_care_visible",
    )
}
ACTOR_PROTOCOL_ORACLES = {
    "clinic_app.ai_care_guard()": (
        "ai.test_care_coordination.test_care_sql_rechecks_inactive_and_foreign_actor",
    ),
}
