"""Exact runtime grants for Finance fiscal evidence, not owner review authority."""

from typing import Final

TABLES: Final = frozenset(
    {
        "billing_fiscalconfiguration",
        "billing_fiscaldocument",
        "billing_fiscaltransition",
    }
)
RUNTIME_GRANTS: Final = {
    "billing_fiscalconfiguration": frozenset({"SELECT"}),
    "billing_fiscaldocument": frozenset({"SELECT", "INSERT"}),
    "billing_fiscaltransition": frozenset({"SELECT"}),
}
COLUMN_GRANTS: Final = frozenset(
    ("billing_fiscaldocument", column, "UPDATE")
    for column in (
        "status",
        "last_command_id",
        "xml_payload",
        "pdf_payload",
        "cancellation_payload",
        "xml_reference",
        "pdf_reference",
        "provider_reference",
        "rejection_code",
    )
)
