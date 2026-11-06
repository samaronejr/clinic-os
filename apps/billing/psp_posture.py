"""Explicit PSP table and runtime privilege posture; no mutable runtime column."""

from typing import Final

TABLES: Final = frozenset(
    {
        "billing_psppolicy",
        "billing_pspintent",
        "billing_pspbinding",
        "billing_pspevidence",
        "billing_pspreceipt",
    }
)
RUNTIME_GRANTS: Final = {table: frozenset({"SELECT", "INSERT"}) for table in TABLES}
