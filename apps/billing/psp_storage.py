"""Stored-only scope and append-only PSP evidence; no external call owns a txn."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from django.db import connection, transaction

from apps.audit.services import record_phase1_event
from apps.billing.money_types import JournalConflictError
from apps.billing.psp_models import PspBinding, PspEvidence, PspIntent
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


def resolve_intent(intent_id: UUID) -> PspScope | None:
    """Resolve committed worker input, not callback tenant claims."""
    if connection.in_atomic_block:
        raise JournalConflictError
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.billing_psp_intent_scope(%s)", [intent_id]
        )
        row = cursor.fetchone()
    return PspScope(*row) if row is not None else None


@contextmanager
def stored_context(scope: PspScope) -> Iterator[None]:
    """Permit authenticated evidence recording after original staff offboarding."""
    if connection.in_atomic_block:
        raise JournalConflictError
    try:
        with transaction.atomic(durable=True), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true),"
                "set_config('app.current_user_id',%s,true)",
                [str(scope.organization_id), str(scope.actor_id)],
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


def load_request(scope: PspScope) -> PaymentRequest:
    """Load immutable facts inside a short transaction, then release it."""
    with stored_context(scope):
        return payment_request(PspIntent.objects.get(pk=scope.intent_id))


def status_event_id(status: PaymentStatus | None) -> str:
    """Exact repeated status lookups converge without consuming later facts."""
    payload = asdict(status) if status is not None else None
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"status-{digest}"


def record_status(
    *,
    scope: PspScope,
    authoritative: PaymentStatus | None,
    event: tuple[str, str],
    decision: str,
    statement: StatementLine | None = None,
) -> str:
    """Retain evidence, never settle or release an outgoing money reservation."""
    source, event_id = event
    with stored_context(scope):
        lock_evidence(scope)
        intent = PspIntent.objects.get(pk=scope.intent_id)
        # Status ids are per intent; callback/statement ids are provider-global.
        if source == "status":
            event_id = hashlib.sha256(f"{intent.pk}:{event_id}".encode()).hexdigest()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.billing_psp_seen(%s,%s,%s)",
                [intent.provider, source, event_id],
            )
            if cursor.fetchone() == (True,):
                return "duplicate"
        if authoritative is not None:
            binding, _ = PspBinding.objects.get_or_create(
                intent=intent,
                defaults={
                    "organization_id": scope.organization_id,
                    "provider": intent.provider,
                    "provider_reference": authoritative.provider_reference,
                },
            )
            if binding.provider_reference != authoritative.provider_reference:
                raise JournalConflictError
        evidence = PspEvidence.objects.create(
            organization_id=scope.organization_id,
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
            payment_code=(
                authoritative.payment_code
                if authoritative and decision == "recorded"
                else ""
            ),
            payment_url=(
                authoritative.payment_url
                if authoritative and decision == "recorded"
                else ""
            ),
        )
        evidence.refresh_from_db(fields=("decision",))
        record_phase1_event(
            "billing.psp.evidence",
            clinic_id=scope.clinic_id,
            affected_record_id=evidence.pk,
        )
        return evidence.decision


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
                "SELECT * FROM clinic_app.billing_psp_scope(%s,%s)",
                [provider, reference],
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            return None
        self.scope = PspScope(*rows[0])
        return load_request(self.scope)

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
