"""Bind retained-source admission to the real acceptance and native matrices."""

from ai import test_care_rollout, test_extraction_rollout
from analytics import test_analyst_rollout
from scribe import test_draft_rollout

CASES = {
    case.__name__: case
    for case in (
        test_draft_rollout.test_retained_draft_cannot_apply_after_rollout_invalidation,
        test_extraction_rollout.test_retained_extraction_cannot_file_after_rollout_invalidation,
        test_care_rollout.test_retained_clinical_care_cannot_apply_after_rollout_invalidation,
        test_care_rollout.test_retained_administrative_care_cannot_repropose_under_new_permission,
        test_analyst_rollout.test_retained_analyst_review_cannot_borrow_current_model_permission,
    )
}
SQL_PROTOCOL_ORACLES = {
    name: list(CASES)
    for name in (
        "clinic_app.ai_effect_source",
        "clinic_app.ai_effect_allowed",
        "clinic_app.ai_effect_current",
        "clinic_app.ai_authorize_effect",
    )
}
