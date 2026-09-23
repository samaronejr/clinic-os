"""Authenticated payment-event reconciliation boundary.

Contract:
- ``receive_payment_event`` authenticates the raw provider event first; only
  verified event fields are used, and tenant claims inside the payload are
  never read. The stored ``(provider, provider_reference)`` pair on the
  charge result resolves the tenant, clinic, invoice and recorded actor.
- Every authentic event is verified against the provider's authoritative
  charge lookup before any settlement is attempted; the lookup runs with no
  open database transaction. A provider query failure records the event as
  requiring operator action instead of guessing. Because the lookup precedes
  the per-operation lock, a newer recorded event with different
  authoritative terms (status, amount or currency) supersedes it:
  superseded observations are recorded ``operator_required`` and never
  settle.
- Exactly one immutable ``PaymentEvent`` row is recorded per
  ``(provider, event_id)``; redelivery converges on the stored row.
- Only a verified exact settlement creates the settlement and its receipt;
  expiration, cancellation, reversal, mismatched or overpaid amounts and
  unverifiable claims are recorded as explicit operator-required states.
  Reconciliation never sends money, cancels an invoice or refunds.
- ``reconcile_charge`` recovers a missed event through the same verified
  path using the provider's authoritative lookup, without duplicating
  money records. Each attempt carries a unique generated event id; a fresh
  lookup is suppressed only when an identical authoritative observation
  was already recorded or settled, so a superseded or rejected attempt
  never permanently consumes the recovered facts.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid4

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.audit.events import build_integration_audit_event
from apps.audit.services import record_event
from apps.billing.adapters import (
    CURRENCY_CODE_LENGTH,
    AuthenticatedPaymentEvent,
    PaymentEventAdapter,
    PaymentEventAuthenticationError,
    PaymentLookupError,
    PaymentOperationFacts,
    ProviderChargeStatus,
)
from apps.billing.models import PaymentEvent, Settlement
from apps.tenancy.db import clear_connection_tenant_gucs

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from datetime import datetime

type _Resolution = Literal["settled", "recorded", "operator_required"]
type PaymentEventResult = Literal[
    "settled", "recorded", "operator_required", "duplicate", "rejected"
]
type ReconcileResult = Literal[
    "settled", "recorded", "operator_required", "duplicate", "rejected", "pending"
]

_EVENT_LOCK_NAMESPACE: Final = "clinic-lock-v1:billing-payment:"
_PAYMENT_ADAPTERS: dict[str, PaymentEventAdapter] = {}


class PaymentContextError(RuntimeError):
    """Reject reconciliation work inside an existing transaction."""

    def __init__(self) -> None:
        """Expose one stable non-identifying message."""
        super().__init__("payment reconciliation requires an outermost transaction")


def register_payment_event_adapter(adapter: PaymentEventAdapter) -> None:
    """Register the authenticator/lookup adapter for one provider."""
    _PAYMENT_ADAPTERS[adapter.provider] = adapter


def clear_payment_event_adapters() -> None:
    """Remove every registered payment event adapter."""
    _PAYMENT_ADAPTERS.clear()


@dataclass(frozen=True, slots=True)
class _ChargeState:
    """Stored scope and financial facts resolved for one provider reference."""

    operation_id: UUID
    invoice_id: UUID
    organization_id: UUID
    clinic_id: UUID
    actor_id: UUID
    invoice_state: str
    invoice_amount_minor: int
    invoice_currency: str
    operation_amount_minor: int
    operation_currency: str
    invoice_reference: UUID
    expires_at: datetime
    settled_amount_minor: int | None
    settled_currency: str | None


def _resolve_charge(provider: str, provider_reference: str) -> _ChargeState | None:
    """Resolve stored scope and facts for one authenticated reference.

    Correlation is the stored ``(provider, provider_reference)`` pair only;
    a reference that resolves to anything but exactly one stored charge is
    rejected rather than silently attributed to a tenant.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT scope.operation_id, scope.invoice_id, "
            "scope.organization_id, scope.clinic_id, scope.actor_id, "
            "scope.invoice_state, scope.invoice_amount_minor, "
            "scope.invoice_currency, scope.operation_amount_minor, "
            "scope.operation_currency, scope.invoice_reference, "
            "scope.expires_at, scope.settled_amount_minor, "
            "scope.settled_currency "
            "FROM clinic_app.billing_payment_event_scope(%s, %s) AS scope",
            [provider, provider_reference],
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    return _ChargeState(
        operation_id=row[0],
        invoice_id=row[1],
        organization_id=row[2],
        clinic_id=row[3],
        actor_id=row[4],
        invoice_state=row[5],
        invoice_amount_minor=row[6],
        invoice_currency=row[7],
        operation_amount_minor=row[8],
        operation_currency=row[9],
        invoice_reference=row[10],
        expires_at=row[11],
        settled_amount_minor=row[12],
        settled_currency=row[13],
    )


def _event_seen(provider: str, event_id: str) -> bool:
    """Deduplicate on the stored ``(provider, event_id)`` pair only."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.billing_payment_event_seen(%s, %s)",
            [provider, event_id],
        )
        row = cursor.fetchone()
    return row is not None and bool(row[0])


def _superseded(
    state: _ChargeState, observed_at: datetime, authoritative: ProviderChargeStatus
) -> bool:
    """Report whether a newer recorded event contradicts this lookup.

    The authoritative lookup runs before the per-operation lock, so a newer
    event can commit while it is in flight. When that event recorded
    different authoritative terms (status, amount or currency), this
    lookup's answer is stale and must not drive settlement.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.billing_payment_event_superseded(%s, %s, %s, %s, %s)",
            [
                state.operation_id,
                observed_at,
                authoritative.status,
                authoritative.amount_minor,
                authoritative.currency,
            ],
        )
        row = cursor.fetchone()
    return row is not None and bool(row[0])


def _recovered(
    provider: str,
    state: _ChargeState,
    event_id: str,
    authoritative: ProviderChargeStatus,
) -> bool:
    """Report whether these authoritative facts were already reconciled.

    Recovery event ids are generated per attempt, so deduplication matches
    the recorded authoritative terms instead: an identical observation
    already recorded or settled makes this attempt a duplicate, while a
    superseded or otherwise operator-required attempt leaves the facts
    recoverable.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.billing_payment_event_recovered(%s, %s, %s, %s, %s, %s)",
            [
                provider,
                state.operation_id,
                event_id,
                authoritative.status,
                authoritative.amount_minor,
                authoritative.currency,
            ],
        )
        row = cursor.fetchone()
    return row is not None and bool(row[0])


@contextmanager
def _stored_scope_context(state: _ChargeState) -> Iterator[None]:
    """Attribute bookkeeping to stored scope in one outermost transaction.

    Used only after provider authentication resolved the stored charge. It
    never reads tenant claims from external input and never performs domain
    reads; RLS and guards still validate every write.
    """
    if connection.in_atomic_block:
        raise PaymentContextError
    try:
        with transaction.atomic(durable=True), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true), "
                "pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(state.actor_id), str(state.organization_id)],
            )
            yield
    finally:
        clear_connection_tenant_gucs()


def _hold_event_lock(operation_id: UUID) -> None:
    """Serialize reconciliation for one charge operation until commit."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended(%s, 0))",
            [f"{_EVENT_LOCK_NAMESPACE}{operation_id}"],
        )


def _operation_facts(state: _ChargeState) -> PaymentOperationFacts:
    """Project stored facts for the provider's authoritative lookup."""
    return PaymentOperationFacts(
        operation_id=state.operation_id,
        invoice_reference=state.invoice_reference,
        amount_minor=state.operation_amount_minor,
        currency=state.operation_currency,
        expires_at=state.expires_at,
    )


def _authoritative(
    adapter: PaymentEventAdapter, state: _ChargeState
) -> ProviderChargeStatus | None:
    """Query the provider's authoritative state with no open transaction.

    A failed query yields ``None`` so the authentic event is still recorded
    as requiring operator action; unexpected adapter errors propagate.
    """
    try:
        status = adapter.lookup(_operation_facts(state))
    except PaymentLookupError:
        return None
    if (
        status.status not in ("pending", "settled", "expired", "cancelled", "reversed")
        or type(status.amount_minor) is not int
        or status.amount_minor <= 0
        or type(status.currency) is not str
        or len(status.currency) != CURRENCY_CODE_LENGTH
    ):
        return None
    return status


def _terms_reason(event: AuthenticatedPaymentEvent, state: _ChargeState) -> str | None:
    """Return the mismatch reason for event terms, or None when exact."""
    if (event.amount_minor, event.currency) == (
        state.operation_amount_minor,
        state.operation_currency,
    ):
        return None
    if event.currency != state.operation_currency:
        return "currency_mismatch"
    return (
        "overpaid" if event.amount_minor > state.operation_amount_minor else "underpaid"
    )


def _verified_settled(
    event: AuthenticatedPaymentEvent,
    state: _ChargeState,
    authoritative: ProviderChargeStatus,
) -> tuple[_Resolution, str | None]:
    """Decide one event while the provider authoritatively reports settled."""
    if (authoritative.amount_minor, authoritative.currency) != (
        state.operation_amount_minor,
        state.operation_currency,
    ):
        return "operator_required", "authoritative_terms_mismatch"
    if event.status != "settled":
        return "operator_required", "status_mismatch"
    if state.invoice_state == "open":
        return "settled", None
    if state.invoice_state == "paid" and (
        state.settled_amount_minor,
        state.settled_currency,
    ) == (state.invoice_amount_minor, state.invoice_currency):
        return "recorded", "already_settled"
    return "operator_required", "invoice_not_open"


def _authoritative_decision(
    event: AuthenticatedPaymentEvent,
    state: _ChargeState,
    authoritative: ProviderChargeStatus,
) -> tuple[_Resolution, str | None]:
    """Decide one event against a successful authoritative lookup."""
    if authoritative.status == "settled":
        return _verified_settled(event, state, authoritative)
    if event.status == "settled":
        return "operator_required", "unverified_settlement"
    if authoritative.status == "pending":
        return "recorded", "pending"
    if event.status == authoritative.status:
        return "operator_required", authoritative.status
    return "operator_required", "status_mismatch"


def _decide(
    event: AuthenticatedPaymentEvent,
    state: _ChargeState,
    authoritative: ProviderChargeStatus | None,
) -> tuple[_Resolution, str | None]:
    """Map verified facts to one explicit resolution and reason vocabulary."""
    if event.status == "reversed":
        return "operator_required", "reversed"
    terms_reason = _terms_reason(event, state)
    if terms_reason is not None:
        return "operator_required", terms_reason
    if authoritative is None:
        return "operator_required", "provider_query_failed"
    return _authoritative_decision(event, state, authoritative)


def _record_audit(
    state: _ChargeState,
    event_row_id: UUID,
    resolution: str,
    reason_code: str | None,
) -> None:
    """Append one fixed-vocabulary billing event in the stored scope."""
    verb = {
        "settled": "settled",
        "recorded": "recorded",
        "operator_required": "flagged",
    }[resolution]
    append = build_integration_audit_event(
        f"billing.payment.{verb}",
        clinic_id=state.clinic_id,
        affected_record_id=event_row_id,
        operation_id=state.operation_id,
        reason_code=reason_code,
    )
    record_event(append.event, payload=append.payload)


def _apply(  # noqa: PLR0913 - one flag per entry-point contract
    adapter: PaymentEventAdapter,
    event: AuthenticatedPaymentEvent,
    state: _ChargeState,
    authoritative: ProviderChargeStatus | None,
    observed_at: datetime,
    *,
    recovery: bool,
) -> PaymentEventResult:
    """Record one verified event and settle only on exact verified terms."""
    event_pk = uuid4()
    with _stored_scope_context(state):
        _hold_event_lock(state.operation_id)
        if recovery:
            if authoritative is not None and _recovered(
                adapter.provider, state, event.event_id, authoritative
            ):
                return "duplicate"
        elif _event_seen(adapter.provider, event.event_id):
            return "duplicate"
        fresh = _resolve_charge(adapter.provider, event.provider_reference)
        if fresh is None:
            return "rejected"
        resolution: _Resolution
        reason_code: str | None
        if authoritative is not None and _superseded(fresh, observed_at, authoritative):
            resolution, reason_code = "operator_required", "superseded"
        else:
            resolution, reason_code = _decide(event, fresh, authoritative)
        settlement_id: UUID | None = None
        if resolution == "settled":
            try:
                with transaction.atomic():
                    settlement = Settlement.objects.create(
                        organization_id=fresh.organization_id,
                        invoice_id=fresh.invoice_id,
                        confirmation_reference=event_pk,
                        amount_minor=fresh.operation_amount_minor,
                        currency=fresh.operation_currency,
                        confirmed_by_id=fresh.actor_id,
                    )
                    settlement_id = settlement.pk
            except DatabaseError:
                # A settlement for a sibling operation of the same invoice may
                # have committed while this insert waited on the unique key;
                # re-resolve under the operation lock before deciding.
                refreshed = _resolve_charge(adapter.provider, event.provider_reference)
                if refreshed is not None:
                    fresh = refreshed
                if (
                    refreshed is not None
                    and refreshed.invoice_state == "paid"
                    and (
                        refreshed.settled_amount_minor,
                        refreshed.settled_currency,
                    )
                    == (
                        refreshed.invoice_amount_minor,
                        refreshed.invoice_currency,
                    )
                ):
                    resolution, reason_code = "recorded", "already_settled"
                else:
                    resolution, reason_code = (
                        "operator_required",
                        "settlement_rejected",
                    )
        # A dedupe-constraint violation here means the same event id was
        # recorded for another operation; propagating rolls back any
        # settlement above so money records never exist without their event.
        PaymentEvent.objects.create(
            id=event_pk,
            organization_id=fresh.organization_id,
            operation_id=fresh.operation_id,
            invoice_id=fresh.invoice_id,
            settlement_id=settlement_id,
            provider=adapter.provider,
            event_id=event.event_id,
            provider_reference=event.provider_reference,
            actor_id=fresh.actor_id,
            reported_status=event.status,
            reported_amount_minor=event.amount_minor,
            reported_currency=event.currency,
            authoritative_status=(
                None if authoritative is None else authoritative.status
            ),
            authoritative_amount_minor=(
                None if authoritative is None else authoritative.amount_minor
            ),
            authoritative_currency=(
                None if authoritative is None else authoritative.currency
            ),
            resolution=resolution,
            reason_code=reason_code,
        )
        _record_audit(fresh, event_pk, resolution, reason_code)
    return resolution


def receive_payment_event(
    *,
    provider: str,
    headers: Mapping[str, str],
    body: bytes,
) -> PaymentEventResult:
    """Apply one authenticated provider payment event to its stored charge.

    Authentication runs before any stored charge or tenant is resolved;
    only the verified event id, provider reference, status and terms are
    used. The provider's authoritative lookup then verifies every claim
    before settlement.
    """
    if connection.in_atomic_block:
        raise PaymentContextError
    adapter = _PAYMENT_ADAPTERS.get(provider)
    if adapter is None:
        raise PaymentEventAuthenticationError
    event = adapter.authenticate(headers=headers, body=body)
    state = _resolve_charge(provider, event.provider_reference)
    if state is None:
        return "rejected"
    if _event_seen(provider, event.event_id):
        return "duplicate"
    observed_at = timezone.now()
    authoritative = _authoritative(adapter, state)
    return _apply(adapter, event, state, authoritative, observed_at, recovery=False)


def reconcile_charge(*, provider: str, provider_reference: str) -> ReconcileResult:
    """Recover a missed event through the approved provider query.

    The authoritative lookup supplies the event facts; a still-pending
    charge records nothing, while any other authoritative state flows
    through the same verified settlement path as a delivered event. Each
    attempt mints a unique event id, so a superseded or rejected attempt
    never blocks a later fresh lookup from settling.
    """
    if connection.in_atomic_block:
        raise PaymentContextError
    adapter = _PAYMENT_ADAPTERS.get(provider)
    if adapter is None:
        raise PaymentEventAuthenticationError
    state = _resolve_charge(provider, provider_reference)
    if state is None:
        return "rejected"
    observed_at = timezone.now()
    authoritative = _authoritative(adapter, state)
    if authoritative is not None and authoritative.status == "pending":
        return "pending"
    status = "pending" if authoritative is None else authoritative.status
    amount = (
        state.operation_amount_minor
        if authoritative is None
        else authoritative.amount_minor
    )
    currency = (
        state.operation_currency if authoritative is None else authoritative.currency
    )
    event = AuthenticatedPaymentEvent(
        event_id=(
            f"reconcile:{provider_reference}:{status}:{amount}:{currency}:{uuid4().hex}"
        ),
        provider_reference=provider_reference,
        status=status,
        amount_minor=amount,
        currency=currency,
    )
    return _apply(adapter, event, state, authoritative, observed_at, recovery=True)
