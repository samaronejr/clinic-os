"""Bind retained-source admission to the real acceptance and native matrices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ai import test_care_rollout, test_extraction_rollout, test_revenue_rollout
from analytics import test_analyst_rollout
from scribe import test_draft_rollout

if TYPE_CHECKING:
    from collections.abc import Callable

CASES: dict[str, Callable[..., None]] = {
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

REVENUE_CASES = {
    case.__name__: case
    for case in (
        test_revenue_rollout.test_retained_revenue_reproposal_keeps_original_permission,
        test_revenue_rollout.test_native_revenue_run_requires_its_original_invocation,
        test_revenue_rollout.test_retained_revenue_idempotent_preview_rechecks_source,
        test_revenue_rollout.test_retained_revenue_payload_cannot_borrow_another_invocation,
        test_revenue_rollout.test_native_care_payload_keeps_its_own_invocation,
        test_revenue_rollout.test_retained_proposal_source_receipt_is_not_runtime_writable,
    )
}
CASES.update(REVENUE_CASES)
SQL_PROTOCOL_ORACLES.update(
    {
        name: list(REVENUE_CASES)
        for name in (
            "clinic_app.ai_authorize_retained_proposal",
            "clinic_app.ai_retained_proposal_guard",
        )
    }
)
