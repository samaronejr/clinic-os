"""Independent exact function, ACL and result contract for todo 56."""

from typing import Final

TABLES: Final = {
    "billing_journalbook",
    "billing_account",
    "billing_journalentry",
    "billing_journalline",
    "billing_practitionerpayoutrule",
    "billing_invoiceterms",
    "billing_journalsettlement",
    "billing_settlementallocation",
    "billing_refund",
    "billing_payout",
    "billing_cashsession",
    "billing_cashsessionevent",
}
TABLE_GRANTS: Final = {(table, "SELECT") for table in TABLES} | {
    (table, "INSERT")
    for table in (
        "billing_account",
        "billing_journalentry",
        "billing_journalline",
        "billing_invoiceterms",
        "billing_journalsettlement",
        "billing_settlementallocation",
        "billing_cashsessionevent",
    )
}
COLUMN_GRANTS: Final = {
    ("billing_journalbook", "id", "UPDATE"),
    ("billing_journalentry", "state", "UPDATE"),
    ("billing_journalentry", "posted_at", "UPDATE"),
    ("billing_invoiceterms", "completion_entry_id", "UPDATE"),
    ("billing_invoiceterms", "completion_evidence", "UPDATE"),
    ("billing_journalsettlement", "state", "UPDATE"),
}
RESULTS: Final = {
    ("billing_finance_read", "requested_clinic uuid"): "boolean",
    ("billing_lock_book", "requested_book uuid"): "void",
    ("billing_lock_clinic_book", "requested_clinic uuid"): "void",
    ("billing_book_access", "requested_book uuid, permission text"): "boolean",
    ("billing_entry_readable", "requested_entry uuid"): "boolean",
    (
        "billing_practitioners",
        "requested_clinic uuid",
    ): "TABLE(user_id uuid, display_label text)",
    ("billing_finance_session", "requested_clinic uuid"): "uuid",
    ("billing_round_ratio", "numerator numeric, denominator numeric"): "bigint",
    ("billing_line", "code text, side text, amount bigint, practitioner uuid"): "jsonb",
    (
        "billing_post_internal",
        "requested_book uuid, fact_kind text, fact_id uuid, "
        "proposed_lines jsonb, reversed_entry uuid, due_dates date[]",
    ): "uuid",
    (
        "billing_post",
        "requested_book uuid, fact_kind text, fact_id uuid, "
        "proposed_lines jsonb, reversed_entry uuid, due_dates date[]",
    ): "TABLE(entry_id uuid, changed boolean)",
    ("billing_journal_guard", ""): "trigger",
    (
        "billing_opening_balance",
        "requested_book uuid, requested_account text, amount bigint, evidence uuid",
    ): "TABLE(entry_id uuid, changed boolean)",
    ("billing_entry_balance", ""): "trigger",
    ("billing_definition_guard", ""): "trigger",
    ("billing_seed_chart", ""): "trigger",
    ("billing_charge_authorized", "requested_clinic uuid"): "boolean",
    ("billing_collection_authorized", "requested_clinic uuid"): "boolean",
    ("billing_settlement_journal_guard", ""): "trigger",
    ("billing_refunded_prepayments", "requested_terms uuid"): "numeric",
    ("billing_allocation_guard", ""): "trigger",
    (
        "billing_recognize_completion",
        "requested_clinic uuid, requested_invoice uuid, evidence uuid",
    ): "TABLE(entry_id uuid, changed boolean)",
    (
        "billing_complete_appointment",
        "requested_clinic uuid, requested_appointment uuid",
    ): "TABLE(entry_id uuid, changed boolean, terms_id uuid, prepared boolean)",
    ("billing_refund_guard", ""): "trigger",
    ("billing_payout_guard", ""): "trigger",
    (
        "billing_account_balance",
        "requested_book uuid, requested_account text",
    ): "numeric",
    ("billing_cash_guard", ""): "trigger",
    ("billing_cash_receipt", ""): "trigger",
    (
        "billing_import_legacy",
        "requested_clinic uuid, requested_settlement uuid",
    ): "uuid",
    ("billing_allocation_paid", ""): "trigger",
}
PUBLIC_API: Final = {
    "billing_finance_read",
    "billing_lock_book",
    "billing_lock_clinic_book",
    "billing_book_access",
    "billing_entry_readable",
    "billing_practitioners",
    "billing_finance_session",
    "billing_round_ratio",
    "billing_post",
    "billing_charge_authorized",
    "billing_opening_balance",
    "billing_recognize_completion",
    "billing_complete_appointment",
    "billing_import_legacy",
}
# The billing_invoice policy predicate: the owner evaluates that policy on
# maintenance writes, as it evaluated questionnaire_staff before todo 56.
OWNER_POLICY: Final = {"billing_charge_authorized"}
INVOKERS: Final = {"billing_line", "billing_round_ratio"}
PG_ONLY: Final = INVOKERS
IMMUTABLE: Final = INVOKERS
STABLE: Final = {"billing_account_balance", "billing_refunded_prepayments"}
POSTURE: Final = {
    name: (
        "i" if name in IMMUTABLE else "s" if name in STABLE else "v",
        name in PUBLIC_API,
        ["clinic_app", "clinic_owner", "clinic_resolver"]
        if name in OWNER_POLICY
        else ["clinic_app", "clinic_resolver"]
        if name in PUBLIC_API
        else ["clinic_resolver"],
    )
    for name, _arguments in RESULTS
}
