"""Stable, clinic-authorized synthetic PIX operations and immutable results.

Prepare inside tenant_context and commit before completing in a subsequent
context. An interrupted completion leaves its request intact for exact replay.
Completion is local only: no external call runs inside a database transaction.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.billing.adapters import (
    SYNTHETIC_PROVIDER,
    PixRequest,
    SyntheticPixAdapter,
    require_synthetic_pix,
)
from apps.billing.models import Invoice, PixCharge, PixOperation
from apps.billing.services import BillingConflictError, view_invoice
from apps.identity.current_context import (
    MANAGER_ROLES,
    require_current_actor_clinic_roles,
)

if TYPE_CHECKING:
    from uuid import UUID


def prepare_pix_charge(
    *,
    clinic_id: UUID,
    invoice_id: UUID,
    previous_id: UUID | None = None,
    provider: str = SYNTHETIC_PROVIDER,
) -> PixOperation:
    """Bind one stable request per invoice or explicitly expired predecessor.

    No caller-provided amount/expiry or random retry key can change an operation.
    Repeated regeneration of the same predecessor returns the same successor.
    """
    require_synthetic_pix(provider=provider)
    with transaction.atomic():
        view_invoice(clinic_id=clinic_id, invoice_id=invoice_id)
        actor_id = require_current_actor_clinic_roles(clinic_id, MANAGER_ROLES)
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        if invoice.state != Invoice.State.OPEN:
            raise BillingConflictError
        existing = PixOperation.objects.filter(
            invoice=invoice, previous_id=previous_id
        ).first()
        if existing is not None:
            return existing
        if previous_id is not None:
            previous = PixOperation.objects.filter(
                pk=previous_id, invoice=invoice, expires_at__lte=timezone.now()
            ).first()
            if previous is None:
                raise BillingConflictError
        now = timezone.now()
        return PixOperation.objects.create(
            organization_id=invoice.organization_id,
            invoice=invoice,
            previous_id=previous_id,
            invoice_reference=invoice.reference,
            amount_minor=invoice.amount_minor,
            currency=invoice.currency,
            provider=provider,
            actor_id=actor_id,
            created_at=now,
            expires_at=now + timedelta(minutes=30),
        )


def complete_pix_charge(
    *, clinic_id: UUID, invoice_id: UUID, operation_id: UUID
) -> PixCharge:
    """Verify and retain exact local QR/copy bytes once, without marking paid."""
    require_synthetic_pix()
    with transaction.atomic():
        view_invoice(clinic_id=clinic_id, invoice_id=invoice_id)
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        operation = PixOperation.objects.filter(
            pk=operation_id, invoice=invoice
        ).first()
        if operation is None or invoice.state != Invoice.State.OPEN:
            raise BillingConflictError
        existing = PixCharge.objects.filter(operation=operation).first()
        if existing is not None:
            return existing
        if operation.expires_at <= timezone.now():
            raise BillingConflictError
        request = PixRequest(
            operation_id=operation.pk,
            invoice_reference=operation.invoice_reference,
            amount_minor=operation.amount_minor,
            currency=operation.currency,
            expires_at=operation.expires_at,
        )
        adapter = SyntheticPixAdapter()
        response = adapter.create_charge(request)
        adapter.verify(request, response)
        return PixCharge.objects.create(
            organization_id=invoice.organization_id,
            operation=operation,
            invoice=invoice,
            provider_reference=response.provider_reference,
            copy_code=response.copy_code,
            qr_base64=response.qr_base64,
            synthetic=response.synthetic,
        )
