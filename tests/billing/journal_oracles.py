"""Executable domain witnesses for each discovered operational journal SQL root."""

from billing import (
    test_accounting_policy,
    test_authority_census,
    test_cash_and_authority,
    test_collection_recovery,
    test_finance_views,
    test_journal,
    test_journal_read_policy,
    test_legacy_bridge,
    test_legacy_replay_scope,
    test_outgoing_sessions,
    test_refund_policy,
    test_suspense_policy,
)

# These are protocols, not exemptions from actor validation. Definition's own
# is_active clause checks the practitioner; its caller is checked by lock_book.
# Allocation, refund, payout and journal-settlement guards also enter lock_book,
# whose has_permission call reaches identity_actor_permission's active-user check.
# The legacy settlement guard uses collection_authorized's checked predicates.
# cash_receipt attributes a write already admitted by cash_guard; its native
# oracle requires both the active-actor refusal and the actual attributed event.
ACTOR_PROTOCOL_ORACLES = {
    "clinic_app.billing_definition_guard()": (
        "billing.test_sql_inactive_actor."
        "test_definition_guard_rechecks_active_actor_and_practitioner",
    ),
    **dict.fromkeys(
        (
            "clinic_app.billing_complete_appointment(uuid,uuid)",
            "clinic_app.billing_import_legacy(uuid,uuid)",
            "clinic_app.billing_recognize_completion(uuid,uuid,uuid)",
        ),
        (
            "billing.test_sql_inactive_actor."
            "test_journal_resolvers_require_current_active_actor",
        ),
    ),
    **dict.fromkeys(
        (
            "clinic_app.billing_allocation_guard()",
            "clinic_app.billing_cash_receipt()",
            "clinic_app.billing_payout_guard()",
            "clinic_app.billing_refund_guard()",
            "clinic_app.billing_settlement_journal_guard()",
        ),
        (
            "billing.test_sql_inactive_actor."
            "test_journal_native_writes_require_current_active_actor",
        ),
    ),
    "clinic_app.billing_settlement_guard()": (
        "identity.test_sql_inactive_actor.test_delegating_gates_refuse_inactive_actor",
    ),
}

CASES = {
    case.__name__: case
    for case in (
        test_authority_census.test_journal_sql_readers_and_legacy_charge_roots_use_exact_roles,
        test_accounting_policy.test_charge_issue_posts_nothing_and_completion_recognizes_accrual,
        test_accounting_policy.test_prepaid_net_collection_accrues_repasse_before_fees_and_applies_on_completion,
        test_accounting_policy.test_source_fact_replay_is_exact_and_different_content_is_refused,
        test_cash_and_authority.test_cash_close_has_zero_unexplained_balance_and_reopen_requires_persisted_step_up,
        test_cash_and_authority.test_first_cash_count_requires_an_explicit_retained_opening_fact,
        test_cash_and_authority.test_finance_approval_does_not_inherit_legacy_manager_equivalence,
        test_cash_and_authority.test_payout_reserves_only_earned_payable_and_unknown_stays_in_transit,
        test_collection_recovery.test_refunded_prepayment_can_settle_the_completion_receivable_again,
        test_finance_views.test_scoped_csv_and_ofx_like_exports_have_identical_exact_line_values,
        test_journal.test_unbalanced_entry_is_rejected_only_when_transaction_commits,
        test_journal_read_policy.test_narrowed_charge_read_returns_no_journal_record_row,
        test_legacy_bridge.test_first_legacy_receipt_prepares_missing_terms_and_commits_the_bridge,
        test_legacy_replay_scope.test_legacy_import_preflight_uses_the_requested_clinic,
        test_outgoing_sessions.test_fresh_executor_can_use_frozen_instruction_after_old_session_revocation,
        test_refund_policy.test_split_refunds_reverse_exact_total_entitlement_with_cumulative_half_even,
        test_suspense_policy.test_suspense_requires_a_real_scoped_owner_not_an_arbitrary_uuid,
    )
}
_READERS = [
    "test_journal_sql_readers_and_legacy_charge_roots_use_exact_roles",
]
# The record read predicates and choices also run with charge.read narrowed.
_NARROWED_READ = "test_narrowed_charge_read_returns_no_journal_record_row"
_RECORD_READERS = [*_READERS, _NARROWED_READ]
_POSTING = [
    "test_source_fact_replay_is_exact_and_different_content_is_refused",
    "test_suspense_requires_a_real_scoped_owner_not_an_arbitrary_uuid",
]
_COMPLETION = [
    "test_charge_issue_posts_nothing_and_completion_recognizes_accrual",
    "test_prepaid_net_collection_accrues_repasse_before_fees_and_applies_on_completion",
]
_CASH = [
    "test_cash_close_has_zero_unexplained_balance_and_reopen_requires_persisted_step_up",
]
SQL_PROTOCOL_ORACLES = {
    "clinic_app.billing_account_balance": _CASH,
    "clinic_app.billing_allocation_guard": _COMPLETION,
    "clinic_app.billing_allocation_paid": _COMPLETION,
    "clinic_app.billing_book_access": _RECORD_READERS,
    "clinic_app.billing_cash_guard": _CASH,
    "clinic_app.billing_cash_receipt": _CASH,
    "clinic_app.billing_charge_authorized": _READERS,
    "clinic_app.billing_collection_authorized": _READERS,
    "clinic_app.billing_complete_appointment": _COMPLETION,
    "clinic_app.billing_definition_guard": _COMPLETION,
    "clinic_app.billing_entry_balance": [
        "test_unbalanced_entry_is_rejected_only_when_transaction_commits",
    ],
    "clinic_app.billing_entry_readable": [
        "test_scoped_csv_and_ofx_like_exports_have_identical_exact_line_values",
        _NARROWED_READ,
    ],
    "clinic_app.billing_finance_read": _RECORD_READERS,
    "clinic_app.billing_finance_session": [
        *_CASH,
        "test_fresh_executor_can_use_frozen_instruction_after_old_session_revocation",
    ],
    "clinic_app.billing_import_legacy": [
        "test_first_legacy_receipt_prepares_missing_terms_and_commits_the_bridge",
        "test_legacy_import_preflight_uses_the_requested_clinic",
    ],
    "clinic_app.billing_journal_guard": _POSTING,
    "clinic_app.billing_lock_book": _POSTING,
    "clinic_app.billing_lock_clinic_book": [
        "test_first_legacy_receipt_prepares_missing_terms_and_commits_the_bridge",
        "test_legacy_import_preflight_uses_the_requested_clinic",
    ],
    "clinic_app.billing_opening_balance": [
        "test_first_cash_count_requires_an_explicit_retained_opening_fact",
    ],
    "clinic_app.billing_payout_guard": [
        "test_payout_reserves_only_earned_payable_and_unknown_stays_in_transit",
        "test_fresh_executor_can_use_frozen_instruction_after_old_session_revocation",
    ],
    "clinic_app.billing_post": _POSTING,
    "clinic_app.billing_post_internal": _POSTING,
    "clinic_app.billing_practitioners": _RECORD_READERS,
    "clinic_app.billing_recognize_completion": _COMPLETION,
    "clinic_app.billing_refund_guard": [
        "test_split_refunds_reverse_exact_total_entitlement_with_cumulative_half_even",
        "test_finance_approval_does_not_inherit_legacy_manager_equivalence",
    ],
    "clinic_app.billing_refunded_prepayments": [
        "test_refunded_prepayment_can_settle_the_completion_receivable_again",
    ],
    "clinic_app.billing_seed_chart": _COMPLETION,
    "clinic_app.billing_settlement_journal_guard": _COMPLETION,
}
