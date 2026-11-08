"""Source-declared Inventory posture; the migrated catalog must prove it."""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final = frozenset(
    {
        "inventory_supplier",
        "inventory_item",
        "inventory_batch",
        "inventory_location",
        "inventory_minimumstock",
        "inventory_purchasebudget",
        "inventory_purchaserequest",
        "inventory_stockmovement",
        "inventory_equipment",
        "inventory_calibration",
        "inventory_maintenance",
        "inventory_commandreceipt",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final = {
    table: frozenset({"SELECT", "INSERT"}) for table in CUSTOM_RLS_TABLES
}
COLUMN_GRANTS: Final = frozenset(
    ("inventory_purchaserequest", column, "UPDATE")
    for column in ("state", "approved_by_id", "approved_at", "last_command_id")
)
AGENT_GRANTS: Final[dict[str, frozenset[str]]] = {}
