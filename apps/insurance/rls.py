"""Exact insurance table, grant and custom-policy allowlists."""

from typing import Final

TABLES: Final = (
    "insurance_payer",
    "insurance_payercontract",
    "insurance_tussterm",
    "insurance_pricetable",
    "insurance_insurancemembership",
    "insurance_guide",
    "insurance_batch",
    "insurance_batchguide",
    "insurance_submission",
    "insurance_return",
    "insurance_glosa",
    "insurance_appeal",
    "insurance_clinicalapproval",
    "insurance_paymentmatch",
    "insurance_payerrequest",
    "insurance_commandreceipt",
)
RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final = frozenset(TABLES)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final = {table: frozenset({"SELECT", "INSERT"}) for table in TABLES}
COLUMN_GRANTS: Final = frozenset(
    (table, "state", "UPDATE")
    for table in ("insurance_guide", "insurance_batch", "insurance_appeal")
)
