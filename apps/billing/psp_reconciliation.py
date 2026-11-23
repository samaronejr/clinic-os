"""Authenticated callbacks and independent status/statement reconciliation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection

from apps.billing.money_types import JournalConflictError
from apps.billing.psp_contracts import (
    PaymentAuthenticationError,
    PaymentLookupError,
    PaymentUnavailableError,
)
from apps.billing.psp_flow import receive_event
from apps.billing.psp_gate import require_psp_rehearsal, require_psp_sandbox
from apps.billing.psp_policy import classify_payment, match_statement
from apps.billing.psp_services import payment_request
from apps.billing.psp_storage import (
    DatabasePaymentEventStore,
    load_intent,
    record_status,
    resolve_intent,
    status_event_id,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from apps.billing.psp_contracts import PaymentProvider


def receive_psp_event(*, provider: str, headers: Mapping[str, str], body: bytes) -> str:
    """Authenticate raw bytes before stored scope lookup; never read tenant claims."""
    from apps.billing.psp_worker import payment_provider  # noqa: PLC0415

    if connection.in_atomic_block:
        raise JournalConflictError
    try:
        require_psp_rehearsal()
        client = payment_provider(provider)
    except PaymentUnavailableError as error:
        raise PaymentAuthenticationError from error
    return receive_event(
        provider=client,
        headers=headers,
        body=body,
        store=DatabasePaymentEventStore(),
    )


def reconcile_psp_intent(*, intent_id: UUID) -> str:
    """Recover by the original provider idempotency key, with no money resend."""
    from apps.billing.psp_worker import payment_provider  # noqa: PLC0415

    require_psp_rehearsal()
    scope = resolve_intent(intent_id)
    if scope is None:
        return "rejected"
    try:
        intent = load_intent(scope)
    except PaymentUnavailableError:
        return "unknown" if scope.observer else "rejected"
    request = payment_request(intent)
    try:
        require_psp_sandbox(
            clinic_id=scope.clinic_id,
            method=request.method,
            version_id=intent.capability_version_id,
            lookup_only=True,
        )
        authoritative = payment_provider(intent.provider).lookup(request=request)
    except (PaymentLookupError, PaymentUnavailableError):
        authoritative = None
    return record_status(
        scope=scope,
        authoritative=authoritative,
        event=("status", status_event_id(authoritative)),
        decision=classify_payment(request, authoritative),
    )


def reconcile_psp_statement(*, intent_id: UUID) -> tuple[str, ...]:
    """Fetch outside transactions and match every line to one exact stored target."""
    from apps.billing.psp_worker import payment_provider  # noqa: PLC0415

    require_psp_rehearsal()
    scope = resolve_intent(intent_id)
    if scope is None:
        return ("rejected",)
    try:
        intent = load_intent(scope)
        client: PaymentProvider = payment_provider(intent.provider)
    except PaymentUnavailableError:
        return ("unknown",) if scope.observer else ("rejected",)
    request = payment_request(intent)
    require_psp_sandbox(
        clinic_id=scope.clinic_id,
        method=request.method,
        version_id=intent.capability_version_id,
        lookup_only=True,
    )
    try:
        status = client.lookup(request=request)
    except PaymentLookupError:
        return ("unknown",)
    if status is None:
        return ("unknown",)
    # This observation is independent of the statement: even an unavailable,
    # empty, repeated or mismatching statement must not hide a newer reversal.
    record_status(
        scope=scope,
        event=("status", status_event_id(status)),
        authoritative=status,
        decision=classify_payment(request, status),
    )
    try:
        lines = client.fetch_statement(account_reference=intent.book_id)
    except PaymentLookupError:
        return ("unknown",)
    # A statement may include other books. Never resolve or import those references.
    return tuple(
        record_status(
            scope=scope,
            authoritative=status,
            event=("statement", line.line_id),
            decision=match_statement(request, status, line),
            statement=line,
        )
        if line.provider_reference == status.provider_reference
        else "unmatched"
        for line in lines
    ) or ("unmatched",)
