"""An explicit policy-owner opening fact is not an automatic cash-close plug."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection

from apps.audit.services import record_phase1_event
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.models import JournalEntry
from apps.billing.money_types import JournalInputError, minor_amount
from apps.billing.security import book_write

if TYPE_CHECKING:
    from uuid import UUID


def record_opening_balance(
    *, clinic_id: UUID, account: str, amount_minor: int, evidence_reference: UUID
) -> JournalEntry:
    """Record one evidenced positive asset opening before that account's first use."""
    minor_amount(amount_minor)
    if account not in {"bank", "petty_cash", "psp_clearing"}:
        raise JournalInputError
    with book_write(clinic_id=clinic_id, permission="finance.policy") as book:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.billing_opening_balance(%s,%s,%s,%s)",
                [book.pk, account, amount_minor, evidence_reference],
            )
            result = cursor.fetchone()
        if result is None:
            raise BillingAccessDeniedError
        entry = JournalEntry.objects.get(pk=result[0], book=book)
        if result[1]:
            record_phase1_event(
                "billing.journal.posted",
                clinic_id=clinic_id,
                affected_record_id=entry.pk,
            )
        return entry
