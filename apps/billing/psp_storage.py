"""Stored-only scope and append-only PSP evidence; no external call owns a txn."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from django.db import IntegrityError, connection, transaction
from psycopg import Error as PostgresError

from apps.audit.services import record_phase1_event
from apps.billing.money_types import JournalConflictError
from apps.billing.psp_contracts import PaymentUnavailableError
from apps.billing.psp_gate import require_psp_sandbox
from apps.billing.psp_models import PspBinding, PspEvidence, PspIntent
from apps.billing.psp_observer import (
    append_observation,
    observation_intent,
)
from apps.billing.psp_observer_key import observer_key
from apps.billing.psp_policy import classify_payment
from apps.billing.psp_services import payment_request
from apps.tenancy.db import clear_connection_tenant_gucs

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from apps.billing.psp_contracts import PaymentRequest, PaymentStatus, StatementLine

__all__ = ("PspEvidence",)


@dataclass(frozen=True, slots=True)
class PspScope:
    """Scope comes exclusively from stored requests and provider bindings."""

    intent_id: UUID
    organization_id: UUID
    clinic_id: UUID
    actor_id: UUID
    observer: bool = False


def resolve_intent(intent_id: UUID) -> PspScope | None:
    """Resolve committed worker input, not callback tenant claims."""
    if connection.in_atomic_block:
        raise JournalConflictError
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT s.*,clinic_app.billing_psp_dispatched(s.intent_id) IS NOT NULL "
            "FROM clinic_app.billing_psp_intent_scope(%s) s",
            [intent_id],
        )
        row = cursor.fetchone()
    return PspScope(*row) if row is not None else None


@contextmanager
def stored_context(scope: PspScope) -> Iterator[None]:
    """Use the stored tenant; the observer never installs a human actor."""
    if connection.in_atomic_block:
        raise JournalConflictError
    try:
        with transaction.atomic(durable=True), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true),"
                "set_config('app.current_user_id',%s,true)",
                [
                    str(scope.organization_id),
                    "" if scope.observer else str(scope.actor_id),
                ],
            )
            yield
    finally:
        clear_connection_tenant_gucs()


def lock_evidence(scope: PspScope) -> None:
    """Use clinic/audit/intent ordering, matching all journal mutation boundaries."""
    with connection.cursor() as cursor:
        for key in (
            f"clinic-lock-v1:clinic:{scope.clinic_id}",
            f"clinic-audit:{scope.organization_id}",
            f"clinic-lock-v1:psp:{scope.intent_id}",
        ):
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", [key]
            )


def load_intent(scope: PspScope) -> PspIntent:
    """Reload exact immutable terms without borrowing a user after dispatch."""
    if scope.observer:
        return observation_intent(scope)
    with stored_context(scope):
        intent = PspIntent.objects.filter(pk=scope.intent_id).first()
        if intent is None:
            raise PaymentUnavailableError
        return intent


def status_event_id(status: PaymentStatus | None) -> str:
    """Exact repeated status lookups converge without consuming later facts."""
    payload = asdict(status) if status is not None else None
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"status-{digest}"


def observation_id(intent_id: UUID, status: PaymentStatus | None) -> str:
    """Hash an observation under its immutable intent, not a delivery ID."""
    return _status_key(intent_id, status_event_id(status))


def _status_key(intent_id: UUID, event_id: str) -> str:
    return hashlib.sha256(f"{intent_id}:{event_id}".encode()).hexdigest()


def _evidence(
    *,
    intent: PspIntent,
    authoritative: PaymentStatus | None,
    event: tuple[str, str],
    decision: str,
    statement: StatementLine | None,
) -> PspEvidence:
    source, event_id = event
    if source == "status":
        event_id = _status_key(intent.pk, event_id)
    return PspEvidence(
        organization_id=intent.organization_id,
        intent=intent,
        provider=intent.provider,
        event_id=event_id,
        source=source,
        status=authoritative.status if authoritative else "unknown",
        decision=decision,
        amount_minor=(
            statement.amount_minor
            if statement
            else authoritative.amount_minor
            if authoritative
            else intent.amount_minor
        ),
        currency=(
            statement.currency
            if statement
            else authoritative.currency
            if authoritative
            else intent.currency
        ),
        fee_minor=(
            statement.fee_minor
            if statement
            else authoritative.fee_minor
            if authoritative
            else 0
        ),
        version=authoritative.version if authoritative else 0,
        payment_code=authoritative.payment_code
        if authoritative and decision == "recorded"
        else "",
        payment_url=authoritative.payment_url
        if authoritative and decision == "recorded"
        else "",
    )


def _append(*, scope: PspScope, evidence: PspEvidence, reference: str | None) -> str:
    # Provider-wide event uniqueness still wins when different intent locks
    # race on one delivery. Roll back the losing insert, not a fresh lookup.
    try:
        with transaction.atomic():
            return _insert(scope=scope, evidence=evidence, reference=reference)
    except IntegrityError as error:
        cause = error.__cause__
        if (
            isinstance(cause, PostgresError)
            and cause.diag.constraint_name == "billing_psp_event_uniq"
        ):
            return "duplicate"
        raise


def _insert(*, scope: PspScope, evidence: PspEvidence, reference: str | None) -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.billing_psp_seen(%s,%s,%s)",
            [evidence.provider, evidence.source, evidence.event_id],
        )
        if cursor.fetchone() == (True,):
            return "duplicate"
    if scope.observer:
        return append_observation(
            scope=scope, evidence=evidence, provider_reference=reference
        )
    if reference is not None:
        binding, _ = PspBinding.objects.get_or_create(
            intent_id=scope.intent_id,
            defaults={
                "organization_id": scope.organization_id,
                "provider": evidence.provider,
                "provider_reference": reference,
            },
        )
        if binding.provider_reference != reference:
            raise JournalConflictError
    evidence.save(force_insert=True)
    evidence.refresh_from_db(fields=("decision",))
    record_phase1_event(
        "billing.psp.evidence",
        clinic_id=scope.clinic_id,
        affected_record_id=evidence.pk,
    )
    return evidence.decision


def _already_observed(scope: PspScope, status: PaymentStatus) -> bool:
    if not scope.observer:
        return (
            PspEvidence.objects.filter(
                intent_id=scope.intent_id,
                status=status.status,
                version=status.version,
                amount_minor=status.amount_minor,
                currency=status.currency,
                fee_minor=status.fee_minor,
            )
            .exclude(source="statement", decision="quarantined")
            .exists()
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.billing_psp_observed(%s,%s,%s,%s,%s,%s,%s)",
            [
                scope.intent_id,
                status.status,
                status.version,
                status.amount_minor,
                status.currency,
                status.fee_minor,
                observer_key(clinic_id=scope.clinic_id),
            ],
        )
        row = cursor.fetchone()
        return row is not None and row[0] is True


def record_status(
    *,
    scope: PspScope,
    authoritative: PaymentStatus | None,
    event: tuple[str, str],
    decision: str,
    statement: StatementLine | None = None,
) -> str:
    """Retain evidence, never settle or release an outgoing money reservation."""
    with stored_context(scope):
        lock_evidence(scope)
        intent = (
            observation_intent(scope)
            if scope.observer
            else PspIntent.objects.get(pk=scope.intent_id)
        )
        reference = authoritative.provider_reference if authoritative else None
        outcome = _append(
            scope=scope,
            reference=reference,
            evidence=_evidence(
                intent=intent,
                authoritative=authoritative,
                event=event,
                decision=decision,
                statement=statement,
            ),
        )
        if (
            event[0] == "callback"
            and outcome == "duplicate"
            and authoritative is not None
            and not _already_observed(scope, authoritative)
        ):
            # Keep the delivery identity unchanged, but retain what this lookup
            # independently learned about the current economic fact.
            _append(
                scope=scope,
                reference=reference,
                evidence=_evidence(
                    intent=intent,
                    authoritative=authoritative,
                    event=("status", status_event_id(authoritative)),
                    decision=classify_payment(payment_request(intent), authoritative),
                    statement=None,
                ),
            )
        return outcome


class DatabasePaymentEventStore:
    """The pure callback boundary authenticates before it invokes this resolver."""

    def __init__(self) -> None:
        """Begin with no tenant, actor or stored charge resolved."""
        self.scope: PspScope | None = None

    def resolve(self, provider: str, reference: str) -> PaymentRequest | None:
        """Resolve only the stored provider/reference pair after authentication."""
        if connection.in_atomic_block:
            raise JournalConflictError
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT s.*,clinic_app.billing_psp_dispatched(s.intent_id) IS NOT NULL "
                "FROM clinic_app.billing_psp_scope(%s,%s) s",
                [provider, reference],
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            return None
        self.scope = PspScope(*rows[0])
        try:
            intent = load_intent(self.scope)
            require_psp_sandbox(
                clinic_id=self.scope.clinic_id,
                method=intent.method,
                version_id=intent.capability_version_id,
                lookup_only=True,
            )
        except PaymentUnavailableError:
            return None
        return payment_request(intent)

    def record(
        self,
        request: PaymentRequest,
        event_id: str,
        authoritative: PaymentStatus | None,
        decision: str,
    ) -> str:
        """Recheck newer versions and event uniqueness under the evidence lock."""
        if self.scope is None or self.scope.intent_id != request.idempotency_key:
            raise JournalConflictError
        return record_status(
            scope=self.scope,
            authoritative=authoritative,
            event=("callback", event_id),
            decision=decision,
        )
