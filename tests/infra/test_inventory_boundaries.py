"""Explicit Inventory service seams remain keyword-only and clinic-scoped."""

from inspect import Parameter, signature

import pytest
from apps.inventory import services

BOUNDARIES = {
    "receive": ("clinic_id", "batch_id", "location_id", "quantity", "command_id"),
    "use": (
        "clinic_id",
        "batch_id",
        "location_id",
        "quantity",
        "command_id",
        "procedure_entry_id",
    ),
    "return_stock": ("clinic_id", "use_movement_id", "quantity", "command_id"),
    "write_off": (
        "clinic_id",
        "batch_id",
        "location_id",
        "quantity",
        "reason",
        "command_id",
    ),
    "transfer": (
        "clinic_id",
        "batch_id",
        "source_location_id",
        "destination_location_id",
        "quantity",
        "command_id",
    ),
    "balance": ("clinic_id", "batch_id", "location_id"),
    "stock_alerts": ("clinic_id",),
    "create_purchase_request": (
        "clinic_id",
        "item_id",
        "supplier_id",
        "quantity",
        "amount_minor",
        "command_id",
    ),
    "propose_replenishment": (
        "clinic_id",
        "budget_id",
        "item_id",
        "supplier_id",
        "quantity",
        "amount_minor",
        "command_id",
    ),
    "approve_budget": ("clinic_id", "amount_minor", "command_id"),
    "approve_purchase": ("clinic_id", "purchase_request_id", "command_id"),
    "receive_purchase": (
        "clinic_id",
        "purchase_request_id",
        "batch_id",
        "location_id",
        "command_id",
    ),
    "register_equipment": (
        "clinic_id",
        "supplier_id",
        "location_id",
        "name",
        "serial",
        "command_id",
    ),
    "record_calibration": (
        "clinic_id",
        "equipment_id",
        "performed_on",
        "due_on",
        "outcome",
        "command_id",
    ),
    "record_maintenance": (
        "clinic_id",
        "equipment_id",
        "performed_on",
        "due_on",
        "outcome",
        "command_id",
    ),
    "equipment_status": ("clinic_id", "equipment_id"),
    "create_item": ("clinic_id", "name", "unit", "command_id"),
    "create_location": ("clinic_id", "name", "command_id"),
    "create_batch": ("clinic_id", "item_id", "lot", "expires_on", "command_id"),
    "create_supplier": ("clinic_id", "name", "contact", "command_id"),
    "set_minimum_stock": (
        "clinic_id",
        "item_id",
        "location_id",
        "quantity",
        "command_id",
    ),
}


@pytest.mark.parametrize("name", BOUNDARIES)
def test_inventory_boundaries_have_exact_scoped_keyword_parameters(name: str) -> None:
    parameters = signature(getattr(services, name)).parameters
    assert tuple(parameters) == BOUNDARIES[name]
    assert all(value.kind is Parameter.KEYWORD_ONLY for value in parameters.values())
