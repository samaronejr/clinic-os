"""Current authority and replay fences shared by Inventory commands."""

from __future__ import annotations

import hashlib
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

import rfc8785
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.db.models import QuerySet

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.inventory.models import CommandReceipt, ScopedRecord

if TYPE_CHECKING:
    from collections.abc import Callable


class InventoryAccessDeniedError(Exception):
    """Unknown and unavailable Inventory scopes have one payload-free refusal."""


class InventoryConflictError(Exception):
    """A closed machine code describes an invariant or replay conflict."""


CONFLICTS = frozenset(
    {
        "insufficient_stock",
        "batch_expired",
        "return_exceeds_use",
        "budget_exceeded",
        "purchase_not_approved",
        "purchase_transition",
        "inventory_immutable",
        "inventory_isolation",
    }
)
INVALID = "inventory_invalid"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
MAX_AMOUNT = 9_000_000_000_000_000
MAX_QUANTITY = 1_000_000_000


def authority(*, clinic_id: UUID, permission: str) -> UUID:
    """Translate current membership/permission loss without reflecting selectors."""
    try:
        return require_permission(permission, clinic_id=clinic_id)
    except CurrentActorError:
        raise InventoryAccessDeniedError from None


def positive(value: int, *, maximum: int = MAX_QUANTITY) -> None:
    """Accept only integer smallest units or centavos, never bool or float."""
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValidationError(INVALID)


def label(value: str, *, maximum: int) -> None:
    """Catalog labels are bounded nonempty plain text, not clinical documents."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(character in value for character in ("<", ">", "\x00", "\r", "\n"))
    ):
        raise ValidationError(INVALID)


def organization(*, clinic_id: UUID) -> UUID:
    """Resolve the organization only from the caller's authorized stored clinic."""
    clinic = Clinic.objects.filter(pk=clinic_id).first()
    if clinic is None:
        raise InventoryAccessDeniedError
    return clinic.organization_id


def record[T: ScopedRecord](model: type[T], *, clinic_id: UUID, record_id: UUID) -> T:
    """Refuse records outside the explicit clinic even when RLS exposes them."""
    row = QuerySet[T](model=model).filter(pk=record_id, clinic_id=clinic_id).first()
    if row is None:
        raise InventoryAccessDeniedError
    return row


def today(*, clinic_id: UUID) -> date:
    """Use the same authoritative local DB clock as movement expiry guards."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.inventory_today(%s)", [clinic_id])
        row = cursor.fetchone()
    if row is None or type(row[0]) is not date:
        raise InventoryAccessDeniedError
    value: date = row[0]
    return value


def execute[T: ScopedRecord](  # noqa: PLR0913 - the closed command envelope
    model: type[T],
    *,
    clinic_id: UUID,
    command_id: UUID,
    operation: str,
    permission: str,
    payload: dict[str, str | int | None],
    create: Callable[[UUID, UUID], T],
) -> T:
    """Serialize, revalidate, apply once, audit, and bind the exact command."""
    actor = authority(clinic_id=clinic_id, permission=permission)
    if not isinstance(command_id, UUID):
        raise ValidationError(INVALID)
    digest = hashlib.sha256(
        rfc8785.dumps({"operation": operation, "payload": payload})
    ).hexdigest()
    tenant = organization(clinic_id=clinic_id)
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    [f"inventory:{tenant}"],
                )
            authority(clinic_id=clinic_id, permission=permission)
            receipt = CommandReceipt.objects.filter(
                clinic_id=clinic_id, command_id=command_id
            ).first()
            if receipt is not None:
                if (
                    receipt.fingerprint != digest
                    or receipt.operation != operation
                    or receipt.object_type != model.__name__.lower()
                ):
                    raise InventoryConflictError(IDEMPOTENCY_CONFLICT)
                result = record(model, clinic_id=clinic_id, record_id=receipt.object_id)
            else:
                result = create(actor, tenant)
                CommandReceipt.objects.create(
                    organization_id=tenant,
                    clinic_id=clinic_id,
                    command_id=command_id,
                    operation=operation,
                    fingerprint=digest,
                    object_type=model.__name__.lower(),
                    object_id=result.pk,
                    recorded_by_id=actor,
                )
                record_phase1_event(
                    f"inventory.{operation}",
                    clinic_id=clinic_id,
                    affected_record_id=result.pk,
                )
            authority(clinic_id=clinic_id, permission=permission)
            return result
    except DatabaseError as error:
        # The savepoint has rolled back before any new authority decision.
        authority(clinic_id=clinic_id, permission=permission)
        state = getattr(error.__cause__, "sqlstate", None)
        code = getattr(getattr(error.__cause__, "diag", None), "message_primary", "")
        if state == "42501":
            raise InventoryAccessDeniedError from None
        if code in CONFLICTS:
            raise InventoryConflictError(code) from None
        if state == "23505":
            raise InventoryConflictError(IDEMPOTENCY_CONFLICT) from None
        if state == "23514" and code in {
            "inventory_invalid",
            "inventory_quantity",
            "inventory_amount",
        }:
            raise ValidationError(INVALID) from None
        raise
