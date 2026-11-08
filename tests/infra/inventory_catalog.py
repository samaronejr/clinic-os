"""Inventory resolver contract verified against the fresh PG16 live catalog."""

from typing import Final

TABLES: Final = frozenset(
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
TABLE_GRANTS: Final = {(table, "SELECT") for table in TABLES}
COLUMN_GRANTS: Final = {
    ("inventory_purchaserequest", "state", "UPDATE"),
    ("inventory_purchaserequest", "last_command_id", "UPDATE"),
}
RESULTS: Final = {
    ("inventory_today", "clinic uuid"): "date",
    ("inventory_batch_allowed", "batch uuid, clinic uuid"): "boolean",
    ("inventory_sum", "batch uuid, location uuid"): "bigint",
    ("inventory_balance", "batch uuid, location uuid"): "bigint",
    ("inventory_guard", ""): "trigger",
    ("inventory_received", ""): "trigger",
    ("inventory_no_truncate", ""): "trigger",
}
POSTURE: Final = {
    name: (
        "v",
        name in {"inventory_today", "inventory_batch_allowed", "inventory_balance"},
        ["clinic_app", "clinic_resolver"]
        if name in {"inventory_today", "inventory_batch_allowed", "inventory_balance"}
        else ["clinic_resolver"],
    )
    for name, _arguments in RESULTS
}
