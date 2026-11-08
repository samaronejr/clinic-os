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
from apps.billing.psp_models import PspIntent
from apps.billing.psp_policy import classify_payment, match_statement
from apps.billing.psp_storage import (
    DatabasePaymentEventStore,
    load_request,
    record_status,
    resolve_intent,
    status_event_id,
    stored_context,
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
    request = load_request(scope)
    with stored_context(scope):
        intent = PspIntent.objects.get(pk=intent_id)
        version_id = intent.capability_version_id
        provider = intent.provider
    try:
        require_psp_sandbox(
            clinic_id=scope.clinic_id,
            method=request.method,
            version_id=version_id,
            lookup_only=True,
        )
        authoritative = payment_provider(provider).lookup(request=request)
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
    request = load_request(scope)
    with stored_context(scope):
        intent = PspIntent.objects.get(pk=intent_id)
        version_id = intent.capability_version_id
        book_id = intent.book_id
        client: PaymentProvider = payment_provider(intent.provider)
    require_psp_sandbox(
        clinic_id=scope.clinic_id,
        method=request.method,
        version_id=version_id,
        lookup_only=True,
    )
    try:
        status = client.lookup(request=request)
        lines = client.fetch_statement(account_reference=book_id)
    except PaymentLookupError:
        return ("unknown",)
    # A statement may include other books. Never resolve or import those references.
    if status is None:
        return ("unknown",)
    if not lines:
        return ("unmatched",)
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
    )
