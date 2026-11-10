"""Prepare exact sandbox payment intent inside the existing tenant transaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.models import InvoiceTerms, Payout, Refund, SettlementAllocation
from apps.billing.money_types import JournalConflictError
from apps.billing.psp_contracts import (
    FIXTURE_PROVIDER,
    PaymentKind,
    PaymentMethod,
    PaymentRequest,
)
from apps.billing.psp_gate import require_psp_sandbox
from apps.billing.psp_models import PspBinding, PspIntent, PspPolicy
from apps.billing.psp_policy import validate_installments
from apps.billing.security import (
    book_write,
    financial_actor,
    require_finance_step_up,
)
from apps.core.integration import ActionOperationRequest, enqueue_operation

if TYPE_CHECKING:
    from uuid import UUID

SUBJECT_TYPE = "billing.psp_intent"


def payment_request(intent: PspIntent) -> PaymentRequest:
    """Project immutable terms; no patient identity or bank/card data crosses."""
    return PaymentRequest(
        intent.pk,
        cast("PaymentKind", intent.kind),
        intent.target_id,
        cast("PaymentMethod", intent.method),
        intent.amount_minor,
        intent.currency,
        intent.installments,
        intent.original_reference,
        intent.payee_reference,
    )


def request_digest(request: PaymentRequest) -> str:
    """Bind the outbox digest to exact frozen amounts and original provider key."""
    return hashlib.sha256(
        json.dumps(
            asdict(request), sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def set_psp_policy(
    *, clinic_id: UUID, max_installments: int, command_id: UUID
) -> PspPolicy:
    """Finance policy authority appends a clinic policy; no gate is advanced."""
    validate_installments("card", 1, maximum=max_installments)
    with book_write(clinic_id=clinic_id, permission="finance.policy") as book:
        existing = PspPolicy.objects.filter(book=book, command_id=command_id).first()
        if existing is not None:
            if existing.max_installments != max_installments:
                raise JournalConflictError
            return existing
        policy = PspPolicy.objects.create(
            organization_id=book.organization_id,
            book=book,
            max_installments=max_installments,
            command_id=command_id,
        )
        record_phase1_event(
            "billing.psp.policy", clinic_id=clinic_id, affected_record_id=policy.pk
        )
        return policy


def _enqueue(intent: PspIntent, clinic_id: UUID) -> None:
    enqueue_operation(
        ActionOperationRequest(
            provider=intent.provider,
            clinic_id=clinic_id,
            subject_type=SUBJECT_TYPE,
            subject_id=intent.pk,
            idempotency_key=intent.pk,
            payload_digest=request_digest(payment_request(intent)),
        )
    )


def prepare_psp_charge(
    *,
    clinic_id: UUID,
    invoice_id: UUID,
    method: str,
    installments: int = 1,
) -> PspIntent:
    """Freeze one PIX/card charge; neither code creation nor dispatch marks paid."""
    with book_write(clinic_id=clinic_id, permission="charge.collect") as book:
        version = require_psp_sandbox(clinic_id=clinic_id, method=method)
        existing = PspIntent.objects.filter(
            book=book, kind="charge", target_id=invoice_id
        ).first()
        if existing is not None:
            if (existing.method, existing.installments) != (method, installments):
                raise JournalConflictError
            return existing
        policy = (
            PspPolicy.objects.filter(book=book).order_by("-created_at", "-id").first()
        )
        if policy is None:
            raise BillingAccessDeniedError
        validate_installments(method, installments, maximum=policy.max_installments)
        try:
            terms = InvoiceTerms.objects.select_related("invoice").get(
                book=book, invoice_id=invoice_id, invoice__state="open", payer="patient"
            )
        except InvoiceTerms.DoesNotExist as error:
            raise BillingAccessDeniedError from error
        if SettlementAllocation.objects.filter(terms=terms).exists():
            raise JournalConflictError
        intent = PspIntent.objects.create(
            organization_id=book.organization_id,
            book=book,
            policy=policy,
            capability_version=version,
            requested_by_id=financial_actor(
                clinic_id=clinic_id, permission="charge.collect"
            ),
            provider=FIXTURE_PROVIDER,
            kind="charge",
            target_id=invoice_id,
            method=method,
            amount_minor=terms.net_minor,
            installments=installments,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        _enqueue(intent, clinic_id)
        record_phase1_event(
            "billing.psp.prepared", clinic_id=clinic_id, affected_record_id=intent.pk
        )
        return intent


def prepare_psp_outgoing(*, clinic_id: UUID, kind: str, target_id: UUID) -> PspIntent:
    """Bind only a Finance-approved reserved refund/payout, with fresh step-up."""
    if kind not in {"refund", "payout"}:
        raise BillingAccessDeniedError
    with book_write(clinic_id=clinic_id, permission=f"{kind}.approve") as book:
        dispatch_session_id = require_finance_step_up(clinic_id=clinic_id)
        existing = PspIntent.objects.filter(
            book=book, kind=kind, target_id=target_id
        ).first()
        if existing is not None:
            return existing
        target = (
            Refund.objects.filter(pk=target_id, book=book, kind="refund").first()
            if kind == "refund"
            else Payout.objects.filter(pk=target_id, book=book).first()
        )
        if target is None or target.state != "approved":
            raise BillingAccessDeniedError
        original = None
        method = "pix"
        if isinstance(target, Refund):
            original = PspIntent.objects.filter(
                book=book,
                kind="charge",
                id=target.settlement.evidence_reference,
            ).first()
            if (
                original is None
                or not PspBinding.objects.filter(intent=original).exists()
            ):
                raise BillingAccessDeniedError
            method = original.method
        version = require_psp_sandbox(clinic_id=clinic_id, method=method)
        policy = (
            PspPolicy.objects.filter(book=book).order_by("-created_at", "-id").first()
        )
        if policy is None:
            raise BillingAccessDeniedError
        intent = PspIntent.objects.create(
            organization_id=book.organization_id,
            book=book,
            policy=policy,
            capability_version=version,
            requested_by_id=financial_actor(
                clinic_id=clinic_id, permission=f"{kind}.approve"
            ),
            dispatch_session_id=dispatch_session_id,
            provider=FIXTURE_PROVIDER,
            kind=kind,
            target_id=target_id,
            method=method,
            amount_minor=target.amount_minor,
            original=original,
            original_reference=(
                PspBinding.objects.get(intent=original).provider_reference
                if original is not None
                else ""
            ),
            payee_reference=(
                target.practitioner_id.hex if isinstance(target, Payout) else ""
            ),
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        # The existing database guard rechecks this exact executor's session.
        target.state = "sending"
        target.save(update_fields=("state",))
        _enqueue(intent, clinic_id)
        record_phase1_event(
            f"billing.{kind}.sending", clinic_id=clinic_id, affected_record_id=target.pk
        )
        record_phase1_event(
            "billing.psp.prepared", clinic_id=clinic_id, affected_record_id=intent.pk
        )
        return intent
