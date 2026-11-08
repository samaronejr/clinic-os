"""Keyword-only clinic-scoped Inventory service boundary."""

from apps.inventory.access import InventoryAccessDeniedError, InventoryConflictError
from apps.inventory.catalog import (
    create_batch,
    create_item,
    create_location,
    create_supplier,
    set_minimum_stock,
)
from apps.inventory.equipment import (
    EquipmentStatus,
    equipment_status,
    record_calibration,
    record_maintenance,
    register_equipment,
)
from apps.inventory.purchasing import (
    approve_budget,
    approve_purchase,
    create_purchase_request,
    propose_replenishment,
    receive_purchase,
)
from apps.inventory.stock import (
    StockAlert,
    balance,
    receive,
    return_stock,
    stock_alerts,
    transfer,
    use,
    write_off,
)

__all__ = (
    "EquipmentStatus",
    "InventoryAccessDeniedError",
    "InventoryConflictError",
    "StockAlert",
    "approve_budget",
    "approve_purchase",
    "balance",
    "create_batch",
    "create_item",
    "create_location",
    "create_purchase_request",
    "create_supplier",
    "equipment_status",
    "propose_replenishment",
    "receive",
    "receive_purchase",
    "record_calibration",
    "record_maintenance",
    "register_equipment",
    "return_stock",
    "set_minimum_stock",
    "stock_alerts",
    "transfer",
    "use",
    "write_off",
)
