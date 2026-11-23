"""PSP action adapter on the shared durable outbox, with lookup-only recovery."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from django.db import connection
from django.utils import timezone

from apps.billing.psp_contracts import (
    PaymentLookupError,
    PaymentRequest,
    PaymentUnavailableError,
)
from apps.billing.psp_flow import dispatch_payment
from apps.billing.psp_gate import require_psp_rehearsal, require_psp_sandbox
from apps.billing.psp_models import PspIntent
from apps.billing.psp_observer import finish_observation
from apps.billing.psp_policy import classify_payment
from apps.billing.psp_services import SUBJECT_TYPE, payment_request, request_digest
from apps.billing.psp_storage import (
    PspScope,
    load_intent,
    lock_evidence,
    observation_id,
    record_status,
    resolve_intent,
    status_event_id,
    stored_context,
)
from apps.comms.adapters import (
    AmbiguousSendError,
    PermanentSendError,
    SendResult,
)
from apps.core.integration import (
    register_action_adapter,
    register_action_recovery_adapter,
    register_subject_recheck,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from apps.billing.psp_contracts import PaymentProvider
    from apps.comms.adapters import OperationScope
    from apps.comms.models import IntegrationOperation

_PROVIDERS: dict[str, PaymentProvider] = {}


def payment_provider(provider: str) -> PaymentProvider:
    """Only explicitly registered local fixture adapters can be resolved."""
    client = _PROVIDERS.get(provider)
    if client is None:
        raise PaymentUnavailableError
    return client


def _eligible(scope: OperationScope) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.billing_psp_dispatchable(subject_id,false) "
            "FROM clinic_app.comms_integrationoperation WHERE id=%s "
            "AND subject_type=%s",
            [scope.operation_id, SUBJECT_TYPE],
        )
        return bool(cursor.fetchone() == (True,))


@dataclass(frozen=True, slots=True)
class PreparedPayment:
    """The committed request and stored scope survive both sends and recovery."""

    operation_id: UUID
    request: PaymentRequest
    scope: PspScope
    version_id: UUID
    expires_at: datetime


class PspSendAdapter:
    """Create outside transactions; retain unknown effects for status lookup."""

    def __init__(self, client: PaymentProvider) -> None:
        """Bind the human-authorized first send adapter."""
        self.client = client
        self.provider = client.provider

    def prepare(self, operation: IntegrationOperation) -> PreparedPayment:
        """Reload authorized immutable terms and verify the outbox digest."""
        try:
            intent = PspIntent.objects.get(
                pk=operation.subject_id,
                organization_id=operation.organization_id,
                book__clinic_id=operation.clinic_id,
                requested_by_id=operation.actor_id,
                provider=operation.provider,
            )
        except PspIntent.DoesNotExist as error:
            raise PermanentSendError from error
        request = payment_request(intent)
        if (
            operation.subject_type != SUBJECT_TYPE
            or operation.idempotency_key != intent.pk
            or operation.payload_digest != request_digest(request)
        ):
            raise PermanentSendError
        try:
            require_psp_sandbox(
                clinic_id=operation.clinic_id,
                method=request.method,
                version_id=intent.capability_version_id,
            )
        except PaymentUnavailableError as error:
            raise PermanentSendError from error
        return PreparedPayment(
            operation.pk,
            request,
            PspScope(
                intent.pk,
                intent.organization_id,
                operation.clinic_id,
                operation.actor_id,
            ),
            intent.capability_version_id,
            intent.expires_at,
        )

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Look up the original key; recovery never calls a mutating method."""
        if (
            not isinstance(prepared, PreparedPayment)
            or prepared.operation_id != operation_id
            or connection.in_atomic_block
        ):
            raise PermanentSendError
        try:
            with stored_context(prepared.scope), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.billing_psp_dispatchable(%s,%s)",
                    [prepared.scope.intent_id, True],
                )
                if cursor.fetchone() != (True,):
                    raise PermanentSendError
            require_psp_sandbox(
                clinic_id=prepared.scope.clinic_id,
                method=prepared.request.method,
                version_id=prepared.version_id,
            )
            # Do not start a new remote effect with no provisioned way to
            # observe this key if the process dies before recording acceptance.
            load_intent(replace(prepared.scope, observer=True))
            status = dispatch_payment(
                provider=self.client,
                request=prepared.request,
                recovery=prepared.expires_at <= timezone.now(),
            )
        except (PaymentLookupError, TimeoutError):
            status = None
        except PaymentUnavailableError as error:
            raise PermanentSendError from error
        try:
            record_status(
                scope=replace(prepared.scope, observer=True),
                authoritative=status,
                event=("status", status_event_id(status)),
                decision=classify_payment(prepared.request, status),
            )
        except PaymentUnavailableError as error:
            # Authority to observe can close after the send. The effect remains
            # unresolved, not retryable and not a known rejection.
            raise AmbiguousSendError from error
        if status is None or status.status == "unknown":
            raise AmbiguousSendError
        return SendResult(status.provider_reference)


def _recover(operation: IntegrationOperation) -> Literal["reconcile", "succeeded"]:
    """Look up a claimed key with no staff membership, step-up, or send authority."""
    try:
        require_psp_rehearsal()
        scope = resolve_intent(operation.subject_id)
        if (
            scope is None
            or not scope.observer
            or (scope.organization_id, scope.clinic_id, scope.actor_id)
            != (operation.organization_id, operation.clinic_id, operation.actor_id)
        ):
            return "reconcile"
        intent = load_intent(scope)
        request = payment_request(intent)
        if (
            operation.subject_type != SUBJECT_TYPE
            or operation.provider != intent.provider
            or operation.idempotency_key != request.idempotency_key
            or operation.payload_digest != request_digest(request)
        ):
            return "reconcile"
        client = payment_provider(intent.provider)
        try:
            status = client.lookup(request=request)
        except (PaymentLookupError, TimeoutError):
            status = None
        decision = classify_payment(request, status)
        retained = record_status(
            scope=scope,
            authoritative=status,
            event=("status", status_event_id(status)),
            decision=decision,
        )
        if (
            status is None
            or decision not in {"verified", "recorded"}
            or retained
            not in {
                "verified",
                "recorded",
                "duplicate",
            }
        ):
            return "reconcile"
        with stored_context(scope):
            lock_evidence(scope)
            if finish_observation(
                scope=scope,
                operation_id=operation.pk,
                provider_reference=status.provider_reference,
                observation_id=observation_id(intent.pk, status),
            ):
                return "succeeded"
    except PaymentUnavailableError:
        # Revoked binding or missing observer proof: no new send, no false
        # cancellation/no-effect decision, and no reuse of the staff actor.
        return "reconcile"
    return "reconcile"


def register_payment_provider(client: PaymentProvider) -> None:
    """Register the outbox action, lookup-only recovery and current-policy recheck."""
    from apps.billing.psp_fixtures import RecordedPaymentProvider  # noqa: PLC0415

    # This source lane does not accept a real/network provider implementation.
    if (
        not isinstance(client, RecordedPaymentProvider)
        or client.provider != "recorded-psp-v1"
    ):
        raise PaymentUnavailableError
    _PROVIDERS[client.provider] = client
    register_action_adapter(PspSendAdapter(client))
    register_action_recovery_adapter(client.provider, _recover)
    register_subject_recheck(SUBJECT_TYPE, _eligible)
