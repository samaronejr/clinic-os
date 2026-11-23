"""Bind evaluation SQL discovery to executed real-role and workflow tests."""

from collections.abc import Callable

from ai import (
    test_agent_execution,
    test_evaluation_authority,
    test_evaluation_fidelity,
    test_evaluation_native,
    test_evaluation_runtime,
    test_native_score_receipts,
    test_rollout,
    test_rollout_posture,
)

CASES: dict[str, Callable[..., None]] = {
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
        test_rollout.test_native_evaluation_run_forgery_is_refused_by_its_row_guard,
    )
}
# ai.0019's evaluation-run guard is reached only by raw run forgery; the
# shared cases go through the services, which refuse first.
_ROW_GUARD_CASE = "test_native_evaluation_run_forgery_is_refused_by_its_row_guard"
_SHARED_CASES = [name for name in CASES if name != _ROW_GUARD_CASE]
SQL_PROTOCOL_ORACLES = {
    name: list(_SHARED_CASES)
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
SQL_PROTOCOL_ORACLES["clinic_app.ai_evaluation_rollout_guard"] = [
    *_SHARED_CASES,
    _ROW_GUARD_CASE,
]
_ORIGIN_CASE = (
    test_evaluation_fidelity.test_shadow_rejects_offline_samples_and_forged_counts
)
CASES[_ORIGIN_CASE.__name__] = _ORIGIN_CASE
_RECEIPT_CASES = (
    test_native_score_receipts.test_native_writer_refuses_nonissued_or_spent_score,
    test_native_score_receipts.test_native_scorer_receipt_catalog_posture,
)
CASES.update({case.__name__: case for case in _RECEIPT_CASES})
SQL_PROTOCOL_ORACLES["clinic_app.ai_eval_store"] = [
    _ORIGIN_CASE.__name__,
    *(case.__name__ for case in _RECEIPT_CASES),
]
SQL_PROTOCOL_ORACLES["clinic_app.ai_eval_record_guard"].extend(
    case.__name__ for case in _RECEIPT_CASES
)
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
