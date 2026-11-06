"""Read-only patient PSP instructions from the released, exact-session projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from config.settings.contracts import resolved_data_mode
from django.conf import settings
from django.db import connection

from apps.tenancy.envelope import reveal

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class PatientPspPayment:
    """Expose instructions or a sealed receipt, never an internal PSP reference."""

    payment_code: str
    payment_url: str
    installments: int
    amount_minor: int
    receipt_reference: UUID | None
    receipt_issued_at: datetime | None


def patient_psp_payment(*, invoice_id: UUID) -> PatientPspPayment | None:
    """Revalidate patient/delegate billing authority inside the database resolver."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.billing_patient_psp_instructions(%s)",
            [invoice_id],
        )
        row = cursor.fetchone()
    if row is None:
        return None
    enabled = (
        resolved_data_mode() == "synthetic"
        and getattr(settings, "BILLING_SYNTHETIC_PSP", False) is True
    )
    code = (
        reveal(purpose="billing.psp.payment_code", envelope=bytes(row[0])).decode()
        if enabled and row[0] is not None
        else ""
    )
    url = (
        reveal(purpose="billing.psp.payment_url", envelope=bytes(row[1])).decode()
        if enabled and row[1] is not None
        else ""
    )
    # No network-capable provider is registered, but never turn stored data into
    # an arbitrary link or falsely label a payable PIX payload as a rehearsal.
    if code and not code.startswith("SYNTHETIC-NOT-PAYABLE|"):
        code = ""
    if url and not url.startswith("https://payments.invalid/"):
        url = ""
    return PatientPspPayment(code, url, row[2], row[3], row[4], row[5])
