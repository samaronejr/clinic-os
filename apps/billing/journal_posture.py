"""Explicit todo-5 posture for the additive operational journal."""

from typing import Final

TABLES: Final = frozenset(
    {
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
)
INSERT_TABLES: Final = frozenset(
    {
        "billing_journalbook",
        "billing_practitionerpayoutrule",
        "billing_invoiceterms",
        "billing_journalsettlement",
        "billing_settlementallocation",
        "billing_refund",
        "billing_payout",
        "billing_cashsession",
    }
)
RUNTIME_GRANTS: Final = {
    table: frozenset({"SELECT", "INSERT"} if table in INSERT_TABLES else {"SELECT"})
    for table in TABLES
}
COLUMN_GRANTS: Final = frozenset(
    {
        ("billing_journalsettlement", "state", "UPDATE"),
        ("billing_refund", "state", "UPDATE"),
        ("billing_refund", "evidence_reference", "UPDATE"),
        ("billing_payout", "state", "UPDATE"),
        ("billing_payout", "evidence_reference", "UPDATE"),
        ("billing_cashsession", "state", "UPDATE"),
        ("billing_cashsession", "counted_minor", "UPDATE"),
        ("billing_cashsession", "reason", "UPDATE"),
        ("billing_cashsession", "last_command_id", "UPDATE"),
    }
)
