"""Draft-only replenishment and approval-bound physical stock receipts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection

from apps.inventory.access import (
    MAX_AMOUNT,
    InventoryConflictError,
    authority,
    execute,
    positive,
    record,
)
from apps.inventory.models import (
    PurchaseBudget,
    PurchaseRequest,
    StockMovement,
)

if TYPE_CHECKING:
    from uuid import UUID


def request(  # noqa: PLR0913 - closed procurement command
    *,
    clinic_id: UUID,
    item_id: UUID,
    supplier_id: UUID,
    quantity: int,
    amount_minor: int,
    command_id: UUID,
    budget_id: UUID | None,
    operation: str,
) -> PurchaseRequest:
    """Retain only a draft; never authorize, contact suppliers or move stock."""
    authority(clinic_id=clinic_id, permission="inventory.use")
    positive(quantity)
    positive(amount_minor, maximum=MAX_AMOUNT)

    def create(_actor: UUID, tenant: UUID) -> PurchaseRequest:
        return PurchaseRequest.objects.create(
            organization_id=tenant,
            clinic_id=clinic_id,
            item_id=item_id,
            supplier_id=supplier_id,
            budget_id=budget_id,
            quantity=quantity,
            amount_minor=amount_minor,
            command_id=command_id,
        )

    return execute(
        PurchaseRequest,
        clinic_id=clinic_id,
        command_id=command_id,
        operation=operation,
        permission="inventory.use",
        payload={
            "item": str(item_id),
            "supplier": str(supplier_id),
            "quantity": quantity,
            "amount_minor": amount_minor,
            "budget": str(budget_id),
        },
        create=create,
    )


def create_purchase_request(  # noqa: PLR0913 - pinned service contract
    *,
    clinic_id: UUID,
    item_id: UUID,
    supplier_id: UUID,
    quantity: int,
    amount_minor: int,
    command_id: UUID,
) -> PurchaseRequest:
    """Record a staff purchase request awaiting manager approval."""
    return request(
        clinic_id=clinic_id,
        item_id=item_id,
        supplier_id=supplier_id,
        quantity=quantity,
        amount_minor=amount_minor,
        command_id=command_id,
        budget_id=None,
        operation="create_purchase_request",
    )


def propose_replenishment(  # noqa: PLR0913 - pinned service contract
    *,
    clinic_id: UUID,
    budget_id: UUID,
    item_id: UUID,
    supplier_id: UUID,
    quantity: int,
    amount_minor: int,
    command_id: UUID,
) -> PurchaseRequest:
    """Propose within remaining approved budget; no autonomous purchase effect."""
    return request(
        clinic_id=clinic_id,
        item_id=item_id,
        supplier_id=supplier_id,
        quantity=quantity,
        amount_minor=amount_minor,
        command_id=command_id,
        budget_id=budget_id,
        operation="propose_replenishment",
    )


def approve_budget(
    *,
    clinic_id: UUID,
    amount_minor: int,
    command_id: UUID,
) -> PurchaseBudget:
    """Publish a retained manager-approved spending envelope."""
    authority(clinic_id=clinic_id, permission="inventory.manage")
    positive(amount_minor, maximum=MAX_AMOUNT)

    def create(actor: UUID, tenant: UUID) -> PurchaseBudget:
        with connection.cursor() as cursor:
            cursor.execute("SELECT statement_timestamp()")
            approved_at = cursor.fetchone()[0]
        return PurchaseBudget.objects.create(
            organization_id=tenant,
            clinic_id=clinic_id,
            amount_minor=amount_minor,
            approved_by_id=actor,
            approved_at=approved_at,
            command_id=command_id,
        )

    return execute(
        PurchaseBudget,
        clinic_id=clinic_id,
        command_id=command_id,
        operation="approve_budget",
        permission="inventory.manage",
        payload={"amount_minor": amount_minor},
        create=create,
    )


def approve_purchase(
    *,
    clinic_id: UUID,
    purchase_request_id: UUID,
    command_id: UUID,
) -> PurchaseRequest:
    """Reserve approved budget and bind approval to the immutable request."""

    def create(actor: UUID, _tenant: UUID) -> PurchaseRequest:
        row = record(
            PurchaseRequest, clinic_id=clinic_id, record_id=purchase_request_id
        )
        if row.state != "draft":
            reason = "purchase_transition"
            raise InventoryConflictError(reason)
        PurchaseRequest.objects.filter(pk=row.pk).update(
            state="approved", approved_by_id=actor, last_command_id=command_id
        )
        row.refresh_from_db()
        return row

    return execute(
        PurchaseRequest,
        clinic_id=clinic_id,
        command_id=command_id,
        operation="approve_purchase",
        permission="inventory.manage",
        payload={"purchase_request": str(purchase_request_id)},
        create=create,
    )


def receive_purchase(
    *,
    clinic_id: UUID,
    purchase_request_id: UUID,
    batch_id: UUID,
    location_id: UUID,
    command_id: UUID,
) -> PurchaseRequest:
    """Record physical receipt once; its DB trigger advances the approved order."""

    def create(actor: UUID, tenant: UUID) -> PurchaseRequest:
        row = record(
            PurchaseRequest, clinic_id=clinic_id, record_id=purchase_request_id
        )
        if row.state != "approved":
            reason = "purchase_not_approved"
            raise InventoryConflictError(reason)
        StockMovement.objects.create(
            organization_id=tenant,
            clinic_id=clinic_id,
            batch_id=batch_id,
            location_id=location_id,
            quantity=row.quantity,
            command_id=command_id,
            kind="receipt",
            purchase_request=row,
            recorded_by_id=actor,
        )
        row.refresh_from_db()
        return row

    return execute(
        PurchaseRequest,
        clinic_id=clinic_id,
        command_id=command_id,
        operation="receive_purchase",
        permission="inventory.manage",
        payload={
            "purchase_request": str(purchase_request_id),
            "batch": str(batch_id),
            "location": str(location_id),
        },
        create=create,
    )
