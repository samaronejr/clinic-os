"""Earned practitioner payables, reservation and explicit ambiguous outcomes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.audit.services import record_phase1_event
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.journal import audit_postings
from apps.billing.models import Payout
from apps.billing.money_types import (
    JournalConflictError,
    JournalInputError,
    minor_amount,
)
from apps.billing.security import (
    book_write,
    request_actor,
    require_finance_step_up,
    require_money_rehearsal,
)

if TYPE_CHECKING:
    from uuid import UUID


def request_payout(
    *, clinic_id: UUID, practitioner_id: UUID, amount_minor: int, command_id: UUID
) -> Payout:
    """Reserve only this practitioner's currently earned and unreserved debt."""
    minor_amount(amount_minor)
    actor = request_actor(clinic_id=clinic_id, kind="payout")
    with book_write(clinic_id=clinic_id, permission="charge.read") as book:
        existing = Payout.objects.filter(book=book, command_id=command_id).first()
        if existing is not None:
            if (existing.practitioner_id, existing.amount_minor) != (
                practitioner_id,
                amount_minor,
            ):
                raise JournalConflictError
            return existing
        payout = Payout.objects.create(
            organization_id=book.organization_id,
            book=book,
            practitioner_id=practitioner_id,
            amount_minor=amount_minor,
            command_id=command_id,
            requested_by_id=actor,
        )
        record_phase1_event(
            "billing.payout.requested",
            clinic_id=clinic_id,
            affected_record_id=payout.pk,
        )
        return payout


def approve_payout(*, clinic_id: UUID, payout_id: UUID) -> Payout:
    """Finance approves exact frozen terms with a fresh persisted verification."""
    with book_write(clinic_id=clinic_id, permission="payout.approve") as book:
        require_finance_step_up(clinic_id=clinic_id)
        try:
            payout = Payout.objects.get(pk=payout_id, book=book)
        except Payout.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        if payout.state != "requested":
            return payout
        payout.state = "approved"
        payout.save(update_fields=("state",))
        payout.refresh_from_db()
        record_phase1_event(
            "billing.payout.approved", clinic_id=clinic_id, affected_record_id=payout.pk
        )
        audit_postings(
            clinic_id=clinic_id, source_id=payout.pk, kinds=("payout_approved",)
        )
        return payout


def transition_payout(
    *,
    clinic_id: UUID,
    payout_id: UUID,
    next_state: str,
    evidence_reference: UUID | None = None,
) -> Payout:
    """Rehearse a transfer; unknown never releases or duplicates its reservation."""
    require_money_rehearsal()
    if next_state not in {"sending", "confirmed", "unknown"}:
        raise JournalInputError
    with book_write(clinic_id=clinic_id, permission="payout.approve") as book:
        require_finance_step_up(clinic_id=clinic_id)
        try:
            payout = Payout.objects.get(pk=payout_id, book=book)
        except Payout.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        if payout.state == next_state:
            if payout.evidence_reference != evidence_reference:
                raise JournalConflictError
            return payout
        payout.state = next_state
        payout.evidence_reference = evidence_reference
        payout.save(update_fields=("state", "evidence_reference"))
        payout.refresh_from_db()
        record_phase1_event(
            f"billing.payout.{next_state}",
            clinic_id=clinic_id,
            affected_record_id=payout.pk,
        )
        audit_postings(
            clinic_id=clinic_id, source_id=payout.pk, kinds=(f"payout_{next_state}",)
        )
        return payout
