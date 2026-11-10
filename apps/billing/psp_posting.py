"""Apply verified PSP facts through the existing exact-authority journal services."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid5

from django.db.models import Q

from apps.audit.services import record_phase1_event
from apps.billing.collections import (
    allocate_settlement,
    observe_settlement,
    post_settlement,
    verify_settlement,
)
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.journal import Line, SourceFact, post_entry
from apps.billing.models import Payout, Refund
from apps.billing.money_types import JournalConflictError, SettlementObservation
from apps.billing.payouts import transition_payout
from apps.billing.psp_gate import require_psp_sandbox
from apps.billing.psp_models import PspEvidence, PspIntent, PspReceipt
from apps.billing.psp_storage import PspScope, lock_evidence
from apps.billing.refunds import transition_refund
from apps.billing.security import book_write

if TYPE_CHECKING:
    from uuid import UUID


def _collection(clinic_id: UUID, intent: PspIntent) -> UUID:
    """Post gross collections and repasse before any separately reconciled PSP fee."""
    settlement = observe_settlement(
        clinic_id=clinic_id,
        observation=SettlementObservation(
            intent.amount_minor, "patient", "psp_clearing", intent.pk
        ),
    )
    verify_settlement(clinic_id=clinic_id, settlement_id=settlement.pk)
    post_settlement(clinic_id=clinic_id, settlement_id=settlement.pk)
    allocation = allocate_settlement(
        clinic_id=clinic_id,
        settlement_id=settlement.pk,
        invoice_id=intent.target_id,
        amount_minor=intent.amount_minor,
        command_id=intent.pk,
    )
    receipt, _ = PspReceipt.objects.get_or_create(
        intent=intent,
        defaults={
            "organization_id": intent.organization_id,
            "settlement": settlement,
            "allocation": allocation,
            "amount_minor": intent.amount_minor,
        },
    )
    return receipt.pk


def _statement_transfer(intent: PspIntent, evidence: PspEvidence) -> None:
    """Release clearing to bank and expense fees without changing net repasse."""
    lines = []
    if intent.kind == "charge":
        if evidence.amount_minor > evidence.fee_minor:
            lines.append(
                Line("bank", "debit", evidence.amount_minor - evidence.fee_minor)
            )
        if evidence.fee_minor:
            lines.append(Line("psp_fees", "debit", evidence.fee_minor))
        lines.append(Line("psp_clearing", "credit", evidence.amount_minor))
    else:
        if intent.kind == "refund":
            # Recorded refund statement rows represent bank-funded PSP returns.
            lines = [
                Line("psp_clearing", "debit", evidence.amount_minor),
                Line("bank", "credit", evidence.amount_minor),
            ]
        if evidence.fee_minor:
            lines.extend(
                [
                    Line("psp_fees", "debit", evidence.fee_minor),
                    Line("bank", "credit", evidence.fee_minor),
                ]
            )
    if lines:
        post_entry(
            book_id=intent.book_id,
            source_fact=SourceFact(
                "manual", uuid5(intent.pk, "psp-statement-transfer-v1")
            ),
            lines=lines,
        )


def _outgoing(clinic_id: UUID, intent: PspIntent, state: str) -> UUID:
    """Apply one held/confirmed outcome through the existing fresh-session guard."""
    target = (
        Refund.objects.get(pk=intent.target_id, book_id=intent.book_id)
        if intent.kind == "refund"
        else Payout.objects.get(pk=intent.target_id, book_id=intent.book_id)
    )
    reference = intent.pk if state == "confirmed" else None
    if target.state == state:
        if target.evidence_reference != reference:
            raise JournalConflictError
        return target.pk
    if intent.kind == "refund":
        return transition_refund(
            clinic_id=clinic_id,
            refund_id=target.pk,
            next_state=state,
            evidence_reference=reference,
        ).pk
    return transition_payout(
        clinic_id=clinic_id,
        payout_id=target.pk,
        next_state=state,
        evidence_reference=reference,
    ).pk


def apply_psp_evidence(*, clinic_id: UUID, evidence_id: UUID) -> UUID:
    """Finance applies retained proof; callbacks cannot approve refunds or payouts."""
    try:
        evidence = PspEvidence.objects.select_related("intent").get(
            pk=evidence_id, intent__book__clinic_id=clinic_id
        )
    except PspEvidence.DoesNotExist as error:
        raise BillingAccessDeniedError from error
    intent = evidence.intent
    permission = (
        "settlement.post" if intent.kind == "charge" else f"{intent.kind}.approve"
    )
    with book_write(clinic_id=clinic_id, permission=permission):
        lock_evidence(
            PspScope(
                intent.pk, intent.organization_id, clinic_id, intent.requested_by_id
            )
        )
        require_psp_sandbox(
            clinic_id=clinic_id,
            method=intent.method,
            version_id=intent.capability_version_id,
            lookup_only=True,
        )
        if evidence.decision == "unknown" and intent.kind in {"refund", "payout"}:
            return _outgoing(clinic_id, intent, "unknown")
        if evidence.decision not in {"verified", "matched"}:
            raise JournalConflictError
        if (
            PspEvidence.objects.filter(intent=intent)
            .exclude(source="statement", decision="quarantined")
            .filter(
                Q(version__gt=evidence.version)
                | (
                    Q(version=evidence.version)
                    & ~Q(
                        status=evidence.status,
                        amount_minor=evidence.amount_minor,
                        currency=evidence.currency,
                        fee_minor=evidence.fee_minor,
                    )
                )
            )
            .exists()
        ):
            raise JournalConflictError
        if intent.kind == "charge":
            result = _collection(clinic_id, intent)
        else:
            result = _outgoing(clinic_id, intent, "confirmed")
        if evidence.source == "statement" and evidence.decision == "matched":
            _statement_transfer(intent, evidence)
        record_phase1_event(
            "billing.psp.applied", clinic_id=clinic_id, affected_record_id=evidence.pk
        )
        return result
