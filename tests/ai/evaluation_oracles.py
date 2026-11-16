"""Bind evaluation SQL discovery to executed real-role and workflow tests."""

from ai import (
    test_agent_execution,
    test_evaluation_authority,
    test_evaluation_native,
    test_evaluation_runtime,
    test_rollout,
    test_rollout_posture,
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
        test_rollout.test_offline_approval_cannot_skip_shadow,
        test_rollout.test_progression_requires_reviewed_preceding_stage_and_separate_autonomy,
        test_rollout.test_native_skipped_stage_and_foreign_receipt_inserts_are_refused,
        test_rollout.test_revoked_approval_membership_invalidates_the_stage_and_its_receipt,
        test_rollout.test_kill_is_durable_across_reenable_and_requires_new_stage_evidence,
        test_agent_execution.test_revoked_approver_blocks_after_approval,
        test_rollout_posture.test_internal_stage_reader_is_not_a_runtime_cross_tenant_reader,
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
        "clinic_app.ai_rollout_authority",
        "clinic_app.ai_rollout_status",
        "clinic_app.ai_rollout_status_for",
        "clinic_app.ai_rollout_bound",
        "clinic_app.ai_rollout_admission",
        "clinic_app.ai_rollout_agent_allowed",
        "clinic_app.ai_rollout_guard",
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
