"""Tenant posture declarations for the billing domain.

Billing tables carry the bespoke ``billing_staff`` policy and immutable
history policies declared in ``apps/billing/migrations/_invoice_sql.py`` and
the PIX/payment-event migrations; none uses the shared ``tenant_isolation``
policy.
"""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "billing_invoice",
        "billing_invoicerevision",
        "billing_settlement",
        "billing_receipt",
        "billing_pixoperation",
        "billing_pixcharge",
        "billing_paymentevent",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "billing_invoice": frozenset({"SELECT", "INSERT"}),
    "billing_invoicerevision": frozenset({"SELECT"}),
    "billing_settlement": frozenset({"SELECT", "INSERT"}),
    "billing_receipt": frozenset({"SELECT"}),
    "billing_pixoperation": frozenset({"SELECT", "INSERT"}),
    "billing_pixcharge": frozenset({"SELECT", "INSERT"}),
    "billing_paymentevent": frozenset({"SELECT", "INSERT"}),
}
# Column-level privileges held by the runtime role (pg_attribute.attacl).
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {
        ("billing_invoice", "amount_minor", "UPDATE"),
        ("billing_invoice", "currency", "UPDATE"),
        ("billing_invoice", "issued_at", "UPDATE"),
        ("billing_invoice", "released_at", "UPDATE"),
        ("billing_invoice", "revision", "UPDATE"),
        ("billing_invoice", "state", "UPDATE"),
    }
)
