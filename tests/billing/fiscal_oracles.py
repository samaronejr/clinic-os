"""Executable fiscal binding, transition and command-receipt witnesses."""

from billing import test_fiscal_database

ACTOR_PROTOCOL_ORACLES = {
    "clinic_app.billing_fiscal_snapshot()": (
        "billing.test_fiscal_inactive_actor."
        "test_fiscal_native_insert_requires_current_active_actor",
    ),
}

CASES = {
    case.__name__: case
    for case in (
        test_fiscal_database.test_fixture_authorization_is_separate_from_receipts_and_posts_no_money,
        test_fiscal_database.test_replay_cannot_change_the_invoice_or_fiscal_review,
        test_fiscal_database.test_runtime_cannot_forge_review_or_skip_submission,
        test_fiscal_database.test_cancellation_records_one_event_across_exact_retries,
        test_fiscal_database.test_cancellation_after_the_reviewed_deadline_preserves_authorization,
    )
}
SQL_PROTOCOL_ORACLES = {
    "clinic_app.billing_fiscal_guard": list(CASES),
    "clinic_app.billing_fiscal_snapshot": [
        "test_fixture_authorization_is_separate_from_receipts_and_posts_no_money",
        "test_cancellation_records_one_event_across_exact_retries",
    ],
}
