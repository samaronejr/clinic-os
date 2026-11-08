"""Explicit frozen service terms and completion/collection accounting policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection, transaction

from apps.audit.services import record_phase1_event
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.journal import audit_postings
from apps.billing.models import (
    Invoice,
    InvoiceTerms,
    JournalEntry,
    JournalSettlement,
    PractitionerPayoutRule,
    SettlementAllocation,
)
from apps.billing.money_types import (
    BASIS_POINTS,
    NO_ADJUSTMENTS,
    ChargeAdjustments,
    JournalConflictError,
    JournalInputError,
    PayoutRuleTerms,
    minor_amount,
)
from apps.billing.security import book_write
from apps.scheduling.models import Appointment

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID


def create_payout_rule(
    *,
    clinic_id: UUID,
    practitioner_id: UUID,
    terms: PayoutRuleTerms,
    command_id: UUID,
) -> PractitionerPayoutRule:
    """Version a per-service fixed-centavo or basis-point rule; never edit a rule."""
    kind, value, service_type_id = terms.kind, terms.value, terms.service_type_id
    minor_amount(value, zero=True)
    if kind not in {"percent", "fixed"} or (kind == "percent" and value > BASIS_POINTS):
        raise JournalInputError
    with book_write(clinic_id=clinic_id, permission="finance.policy") as book:
        existing = PractitionerPayoutRule.objects.filter(
            book=book, command_id=command_id
        ).first()
        if existing is not None:
            if (
                existing.practitioner_id,
                existing.kind,
                existing.value,
                existing.service_type_id,
            ) != (practitioner_id, kind, value, service_type_id):
                raise JournalConflictError
            return existing
        rule = PractitionerPayoutRule.objects.create(
            organization_id=book.organization_id,
            book=book,
            practitioner_id=practitioner_id,
            kind=kind,
            value=value,
            service_type_id=service_type_id,
            command_id=command_id,
        )
        record_phase1_event(
            "billing.rule.created", clinic_id=clinic_id, affected_record_id=rule.pk
        )
        return rule


def prepare_invoice_terms(
    *,
    clinic_id: UUID,
    invoice_id: UUID,
    payer: str = "patient",
    adjustments: ChargeAdjustments = NO_ADJUSTMENTS,
    payout_rule_id: UUID | None = None,
) -> InvoiceTerms:
    """Freeze net terms before collection; no practitioner rule is inferred."""
    discount_minor = adjustments.discount_minor
    payer_adjustment_minor = adjustments.payer_adjustment_minor
    minor_amount(discount_minor, zero=True)
    minor_amount(payer_adjustment_minor, zero=True)
    if payer not in {"patient", "insurer"}:
        raise JournalInputError
    with book_write(clinic_id=clinic_id, permission="settlement.post") as book:
        try:
            invoice = Invoice.objects.get(pk=invoice_id, clinic_id=clinic_id)
        except Invoice.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        net = invoice.amount_minor - discount_minor - payer_adjustment_minor
        minor_amount(net)
        existing = InvoiceTerms.objects.filter(book=book, invoice=invoice).first()
        if existing is not None:
            if (
                existing.payer,
                existing.discount_minor,
                existing.payer_adjustment_minor,
                existing.payout_rule_id,
            ) != (payer, discount_minor, payer_adjustment_minor, payout_rule_id):
                raise JournalConflictError
            return existing
        terms = InvoiceTerms.objects.create(
            organization_id=book.organization_id,
            book=book,
            invoice=invoice,
            payer=payer,
            discount_minor=discount_minor,
            payer_adjustment_minor=payer_adjustment_minor,
            net_minor=net,
            payout_rule_id=payout_rule_id,
        )
        record_phase1_event(
            "billing.terms.prepared", clinic_id=clinic_id, affected_record_id=terms.pk
        )
        if (
            invoice.appointment_id is not None
            and Appointment.objects.filter(
                pk=invoice.appointment_id, clinic_id=clinic_id, status="completed"
            ).exists()
        ):
            record_completion(clinic_id=clinic_id, invoice_id=invoice_id)
            terms.refresh_from_db()
        return terms


def _completion_audit(clinic_id: UUID, results: Sequence[tuple[UUID, bool]]) -> None:
    for entry_id, changed in results:
        if changed:
            record_phase1_event(
                "billing.journal.posted",
                clinic_id=clinic_id,
                affected_record_id=entry_id,
            )
            record_phase1_event(
                "billing.service.completed",
                clinic_id=clinic_id,
                affected_record_id=entry_id,
            )


def record_completion(
    *, clinic_id: UUID, invoice_id: UUID, evidence_reference: UUID | None = None
) -> JournalEntry:
    """Recognize completed care or attested unscheduled care."""
    with book_write(clinic_id=clinic_id, permission="settlement.post"):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.billing_recognize_completion(%s,%s,%s)",
                [clinic_id, invoice_id, evidence_reference],
            )
            result = cursor.fetchone()
        if result is None:
            raise BillingAccessDeniedError
        _completion_audit(clinic_id, (result,))
        return JournalEntry.objects.get(pk=result[0], book__clinic_id=clinic_id)


def record_appointment_completion(*, clinic_id: UUID, appointment_id: UUID) -> None:
    """Join actual clinician completion atomically; no book means legacy behavior."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.billing_complete_appointment(%s,%s)",
            [clinic_id, appointment_id],
        )
        results = cursor.fetchall()
    for entry_id, changed, terms_id, prepared in results:
        if prepared:
            record_phase1_event(
                "billing.terms.prepared",
                clinic_id=clinic_id,
                affected_record_id=terms_id,
            )
        _completion_audit(clinic_id, ((entry_id, changed),))


def bridge_legacy_settlement(*, clinic_id: UUID, settlement_id: UUID) -> None:
    """Bridge trusted full settlements, never unchecked provider events."""
    existing = JournalSettlement.objects.filter(
        legacy_settlement_id=settlement_id, book__clinic_id=clinic_id
    ).exists()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT t.id,t.completion_entry_id FROM clinic_app.billing_invoiceterms t "
            "JOIN clinic_app.billing_settlement s ON s.invoice_id=t.invoice_id "
            "JOIN clinic_app.billing_journalbook b ON b.id=t.book_id "
            "WHERE s.id=%s AND b.clinic_id=%s",
            [settlement_id, clinic_id],
        )
        prior_terms = cursor.fetchone()
        cursor.execute(
            "SELECT clinic_app.billing_import_legacy(%s,%s)", [clinic_id, settlement_id]
        )
        result = cursor.fetchone()
    if existing or result is None or result[0] is None:
        return
    settlement = JournalSettlement.objects.get(pk=result[0], book__clinic_id=clinic_id)
    for state in ("observed", "verified", "posted"):
        record_phase1_event(
            f"billing.settlement.{state}",
            clinic_id=clinic_id,
            affected_record_id=settlement.pk,
        )
    audit_postings(clinic_id=clinic_id, source_id=settlement.pk, kinds=("collection",))
    allocation = SettlementAllocation.objects.get(settlement=settlement)
    terms = InvoiceTerms.objects.get(pk=allocation.terms_id)
    if prior_terms is None:
        record_phase1_event(
            "billing.terms.prepared",
            clinic_id=clinic_id,
            affected_record_id=terms.pk,
        )
    completed = terms.completion_entry_id
    if completed is not None and (prior_terms is None or prior_terms[1] is None):
        _completion_audit(clinic_id, ((completed, True),))
    record_phase1_event(
        "billing.allocation.created",
        clinic_id=clinic_id,
        affected_record_id=allocation.pk,
    )
    audit_postings(clinic_id=clinic_id, source_id=allocation.pk, kinds=("allocation",))
