"""Non-staff PSP observation; no dispatch, Finance, or journal authority."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db import connection

from apps.audit.canonical import AuditTrustedContext, _content_hash, _normalize_payload
from apps.audit.events import build_integration_audit_event, build_phase1_audit_event
from apps.billing.psp_contracts import PaymentUnavailableError
from apps.billing.psp_models import PspIntent
from apps.billing.psp_observer_key import observer_key
from apps.tenancy.envelope import protect

if TYPE_CHECKING:
    from apps.audit.events import Phase1AuditAppend
    from apps.billing.psp_models import PspEvidence
    from apps.billing.psp_storage import PspScope

# A non-user UUID claimed only by the bounded audit writers in billing 0016.
PRINCIPAL: Final = UUID("681a59f4-1475-4b9d-8bf5-73a61fd285e1")


def observation_intent(scope: PspScope) -> PspIntent:
    """Read a single claimed intent through the credential-checked resolver."""
    intent = next(
        iter(
            PspIntent.objects.raw(
                "SELECT * FROM clinic_app.billing_psp_observer_request(%s,%s)",
                [scope.intent_id, observer_key(clinic_id=scope.clinic_id)],
            )
        ),
        None,
    )
    if intent is None:
        raise PaymentUnavailableError
    return intent


def _audit_hash(scope: PspScope, append: Phase1AuditAppend) -> bytes:
    return _content_hash(
        append.event,
        AuditTrustedContext(scope.organization_id, PRINCIPAL),
        _normalize_payload(append.payload),
    )


def append_observation(
    *, scope: PspScope, evidence: PspEvidence, provider_reference: str | None
) -> str:
    """Append one retained fact with a canonical, system-attributed audit event."""
    append = build_phase1_audit_event(
        "billing.psp.evidence",
        clinic_id=scope.clinic_id,
        affected_record_id=evidence.pk,
    )
    fact = {
        "id": str(evidence.pk),
        "source": evidence.source,
        "event_id": evidence.event_id,
        "status": evidence.status,
        "decision": evidence.decision,
        "amount_minor": evidence.amount_minor,
        "currency": evidence.currency,
        "fee_minor": evidence.fee_minor,
        "version": evidence.version,
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.billing_psp_observer_record("
            "%s,%s,%s,%s::jsonb,%s,%s,%s,%s)",
            [
                scope.intent_id,
                observer_key(clinic_id=scope.clinic_id),
                provider_reference,
                json.dumps(fact),
                protect(
                    purpose="billing.psp.payment_code",
                    plaintext=evidence.payment_code.encode(),
                )
                if evidence.payment_code
                else None,
                protect(
                    purpose="billing.psp.payment_url",
                    plaintext=evidence.payment_url.encode(),
                )
                if evidence.payment_url
                else None,
                append.event.occurred_at_utc,
                _audit_hash(scope, append),
            ],
        )
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], str):
        raise PaymentUnavailableError
    return row[0]


def finish_observation(
    *,
    scope: PspScope,
    operation_id: UUID,
    provider_reference: str,
    observation_id: str,
) -> bool:
    """Let the database close a first or recovered send only on current evidence."""
    append = build_integration_audit_event(
        "comms.operation.succeeded",
        clinic_id=scope.clinic_id,
        affected_record_id=operation_id,
        operation_id=operation_id,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.billing_psp_observer_finish(%s,%s,%s,%s,%s,%s,%s)",
            [
                scope.intent_id,
                operation_id,
                observer_key(clinic_id=scope.clinic_id),
                provider_reference,
                observation_id,
                append.event.occurred_at_utc,
                _audit_hash(scope, append),
            ],
        )
        row = cursor.fetchone()
        return row is not None and row[0] is True
