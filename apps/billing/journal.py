"""Clinic-only posting facade; PostgreSQL owns sealing, balance and fact identity."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast
from uuid import UUID

from django.db import connection, transaction
from psycopg.types.json import Jsonb

from apps.audit.services import record_phase1_event
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.journal_models import CHART
from apps.billing.models import JournalBook, JournalEntry
from apps.billing.money_types import (
    MINIMUM_LINES,
    JournalConflictError,
    JournalInputError,
    Line,
    SourceFact,
    largest_remainder,
    minor_amount,
    round_reais,
)
from apps.billing.security import book_write, financial_actor
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = (
    "JournalConflictError",
    "Line",
    "SourceFact",
    "audit_postings",
    "create_book",
    "largest_remainder",
    "post_entry",
    "round_reais",
)


class _LinePayload(TypedDict):
    account: str
    side: str
    amount_minor: str
    practitioner_id: str | None
    owner_id: str | None
    due_on: str | None


def create_book(*, clinic_id: UUID) -> JournalBook:
    """Record the explicit completion-net-v1 clinic policy and seed its chart once."""
    with transaction.atomic():
        financial_actor(clinic_id=clinic_id, permission="finance.policy")
        try:
            clinic = Clinic.objects.get(pk=clinic_id)
        except Clinic.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                [f"clinic-lock-v1:clinic:{clinic_id}"],
            )
        existing = JournalBook.objects.filter(clinic_id=clinic_id).first()
        if existing is not None:
            return existing
        book = JournalBook.objects.create(
            organization_id=clinic.organization_id, clinic_id=clinic_id
        )
        record_phase1_event(
            "billing.book.created", clinic_id=clinic_id, affected_record_id=book.pk
        )
        return book


def post_entry(
    *, book_id: UUID, source_fact: SourceFact, lines: Sequence[Line]
) -> JournalEntry:
    """Post once per fact, rejecting changed replay content without eager balancing.

    Balance deliberately remains a deferred COMMIT constraint. An unbalanced
    call can return inside an atomic block but can never commit. Automatic
    economic kinds are reserved to the guarded domain writers, not this API.
    """
    if (
        type(book_id) is not UUID
        or source_fact.kind != "manual"
        or type(source_fact.id) is not UUID
        or (
            source_fact.reversal_of is not None
            and type(source_fact.reversal_of) is not UUID
        )
        or len(lines) < MINIMUM_LINES
    ):
        raise JournalInputError
    payload: list[_LinePayload] = []
    for line in lines:
        minor_amount(line.amount_minor)
        if line.account not in CHART or line.side not in {"debit", "credit"}:
            raise JournalInputError
        for dimension in (line.practitioner_id, line.owner_id):
            if dimension is not None and type(dimension) is not UUID:
                raise JournalInputError
        payload.append(
            {
                "account": line.account,
                "side": line.side,
                "amount_minor": str(line.amount_minor),
                "practitioner_id": str(line.practitioner_id)
                if line.practitioner_id is not None
                else None,
                "owner_id": str(line.owner_id) if line.owner_id is not None else None,
                "due_on": line.due_on.isoformat() if line.due_on is not None else None,
            }
        )
    try:
        book = JournalBook.objects.get(pk=book_id)
    except JournalBook.DoesNotExist as error:
        raise BillingAccessDeniedError from error
    with book_write(clinic_id=book.clinic_id, permission="settlement.post"):
        with connection.cursor() as cursor:
            # Django's cursor stub omits psycopg's supported Jsonb adapter.
            execute = cast(
                "Callable[[str, Sequence[UUID | str | Jsonb | None]], object]",
                cursor.execute,
            )
            execute(
                "SELECT * FROM clinic_app.billing_post(%s,%s,%s,%s,%s)",
                [
                    book_id,
                    source_fact.kind,
                    source_fact.id,
                    Jsonb(payload),
                    source_fact.reversal_of,
                ],
            )
            result = cursor.fetchone()
        if result is None:
            raise BillingAccessDeniedError
        entry = JournalEntry.objects.get(pk=result[0], book_id=book_id)
        if result[1]:
            record_phase1_event(
                "billing.journal.posted",
                clinic_id=book.clinic_id,
                affected_record_id=entry.pk,
            )
        return entry


def audit_postings(*, clinic_id: UUID, source_id: UUID, kinds: Sequence[str]) -> None:
    """Audit newly created domain postings, called only on a changed command."""
    for entry_id in (
        JournalEntry.objects.filter(
            book__clinic_id=clinic_id, source_id=source_id, source_kind__in=kinds
        )
        .order_by("id")
        .values_list("id", flat=True)
    ):
        record_phase1_event(
            "billing.journal.posted", clinic_id=clinic_id, affected_record_id=entry_id
        )
