"""Bind evaluation SQL discovery to executed real-role and workflow tests."""

from ai import (
    test_evaluation_authority,
    test_evaluation_native,
    test_evaluation_runtime,
)

CASES = {
    case.__name__: case
    for case in (
        test_evaluation_authority.test_evaluation_sql_checks_every_stored_role_and_exact_clinic,
        test_evaluation_authority.test_evaluation_raw_readers_and_rating_guard_refuse_inactive_or_foreign_actor,
        test_evaluation_runtime.test_two_independent_reviews_and_explicit_owner_change_are_required,
        test_evaluation_runtime.test_threshold_history_is_immutable_and_new_registration_invalidates_old_run,
        test_evaluation_runtime.test_disagreement_is_blinded_until_both_votes_and_requires_distinct_adjudicator,
        test_evaluation_runtime.test_assignments_scope_and_raw_verdict_writes_cannot_be_forged,
        test_evaluation_native.test_adjudicator_only_receives_votes_after_both_independent_reviews,
    )
}
SQL_PROTOCOL_ORACLES = {
    name: list(CASES)
    for name in (
        "clinic_app.ai_eval_activation_allowed",
        "clinic_app.ai_eval_finalize",
        "clinic_app.ai_eval_model_gate",
        "clinic_app.ai_eval_rating_visible",
        "clinic_app.ai_eval_record_guard",
        "clinic_app.ai_eval_run_visible",
        "clinic_app.ai_eval_staff",
        "clinic_app.ai_eval_verdict",
    )
}
ACTOR_PROTOCOL_ORACLES = dict.fromkeys(
    (
        "clinic_app.ai_eval_rating_visible(uuid,text,uuid)",
        "clinic_app.ai_eval_record_guard()",
        "clinic_app.ai_eval_run_visible(uuid)",
    ),
    (
        "ai.test_evaluation_authority."
        "test_evaluation_raw_readers_and_rating_guard_refuse_inactive_or_foreign_actor",
    ),
)
