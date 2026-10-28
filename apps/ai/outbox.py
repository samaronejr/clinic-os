"""The shared outbox owns claim fencing; this adapter owns inference policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic_ns
from typing import TYPE_CHECKING

from apps.ai.adapters import provider_for
from apps.ai.artifacts import input_for
from apps.ai.brief_contracts import SCHEMA
from apps.ai.completion import finish_failure, finish_success
from apps.ai.context import recheck_context
from apps.ai.contracts import (
    GatewayAccessDeniedError,
    GatewayInputError,
    GatewayRefusedError,
    Method,
    ProviderRequest,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Purpose,
    Reason,
    Usage,
)
from apps.ai.models import AICircuit, AIInvocation
from apps.ai.output_validation import call_provider, parse_method
from apps.ai.policy import check_configuration, enabled_policy
from apps.ai.prompt_registry import load_prompt
from apps.comms.adapters import OperationScope, PermanentSendError, SendResult
from apps.comms.models import IntegrationOperation
from apps.identity.current_context import CurrentActorError
from apps.tenancy.db import tenant_context

if TYPE_CHECKING:
    from uuid import UUID


class AITransportError(PermanentSendError):
    """Sanitize all adapter failures before they reach the shared transport."""

    def __init__(self) -> None:
        """Retain no provider output or exception text."""
        super().__init__("AI unavailable; continue manually")


@dataclass(frozen=True, slots=True)
class PreparedInference:
    """Plaintext lives only in this ephemeral, non-rendering worker object."""

    scope: OperationScope
    invocation_id: UUID
    provider: str
    method: Method
    request: ProviderRequest = field(repr=False)


def eligible(scope: OperationScope) -> bool:
    """Recheck both kill switches and immutable terms before every send."""
    operation = IntegrationOperation.objects.filter(
        pk=scope.operation_id,
        provider="ai_gateway",
        kind="action",
        subject_type="ai.invocation",
        clinic_id=scope.clinic_id,
        actor_id=scope.actor_id,
    ).first()
    if operation is None:
        return False
    # Admission checks the subject before enqueue_operation returns its id.
    # The caller binds operation_id in that same transaction before commit;
    # preparation later requires the committed one-to-one link.
    invocation = (
        AIInvocation.objects.select_related("configuration")
        .filter(
            pk=operation.subject_id,
            clinic_id=scope.clinic_id,
            user_id=scope.actor_id,
        )
        .first()
    )
    if (
        invocation is None
        or invocation.status != "reserved"
        or invocation.operation_id not in {None, scope.operation_id}
        or invocation.prompt_sha256 != operation.payload_digest
    ):
        return False
    if AICircuit.objects.filter(
        configuration_id=invocation.configuration_id, closed_at__isnull=True
    ).exists():
        finish_failure(invocation, reason="circuit_open")
        return False
    try:
        enabled_policy(Purpose(invocation.purpose))
        check_configuration(invocation.configuration, scope.clinic_id)
        source = input_for(
            clinic_id=scope.clinic_id,
            inputs_ref=invocation.inputs_ref_id,
            purpose=Purpose(invocation.purpose),
        )
        if (
            invocation.purpose == Purpose.BRIEF
            and f'"schema": "{SCHEMA}"' in source.content
        ):
            recheck_context(clinic_id=scope.clinic_id, content=source.content)
    except GatewayRefusedError as error:
        finish_failure(invocation, reason=error.reason)
        return False
    except (GatewayAccessDeniedError, CurrentActorError, GatewayInputError) as error:
        finish_failure(
            invocation,
            reason="invalid_output"
            if isinstance(error, GatewayInputError)
            else "authority_revoked",
        )
        return False
    return True


def organization_lock_key(scope: OperationScope, _subject_id: UUID) -> str:
    """Serialize org policy changes with the final outbox send boundary."""
    return f"ai-organization:{scope.organization_id}"


class GatewayActionAdapter:
    """Prepare inside a tenant transaction, infer outside it, then settle."""

    provider = "ai_gateway"

    def prepare(self, operation: IntegrationOperation) -> PreparedInference:
        """Load encrypted source only after the outbox has rechecked authority."""
        invocation = AIInvocation.objects.select_related("configuration").get(
            pk=operation.subject_id,
            operation_id=operation.pk,
            clinic_id=operation.clinic_id,
            user_id=operation.actor_id,
        )
        source = input_for(
            clinic_id=operation.clinic_id,
            inputs_ref=invocation.inputs_ref_id,
            purpose=Purpose(invocation.purpose),
        )
        prompt, digest = load_prompt(Purpose(invocation.purpose))
        if digest != invocation.prompt_sha256 or digest != operation.payload_digest:
            finish_failure(invocation, reason="configuration_unavailable")
            raise AITransportError
        configuration = invocation.configuration
        return PreparedInference(
            scope=OperationScope(
                operation.pk,
                operation.organization_id,
                operation.clinic_id,
                operation.actor_id,
            ),
            invocation_id=invocation.pk,
            provider=configuration.provider,
            method=parse_method(source.method),
            request=ProviderRequest(
                configuration.model_id,
                configuration.endpoint,
                prompt,
                source.content,
                Usage(
                    tokens_in=invocation.limit_tokens_in,
                    tokens_out=invocation.limit_tokens_out,
                    audio_seconds=invocation.limit_audio_seconds,
                ),
                configuration.timeout_seconds,
            ),
        )

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Honor SendAdapter's object boundary, never put content in receipts."""
        if not isinstance(prepared, PreparedInference):
            raise AITransportError
        scope = prepared.scope
        if operation_id != scope.operation_id:
            raise AITransportError
        # No transaction spans the adapter call. Recheck after waiting for the
        # org lock as well as in core's presend callback.
        with tenant_context(scope.actor_id, scope.organization_id):
            allowed = eligible(scope)
        if not allowed:
            raise AITransportError
        started = monotonic_ns()
        reason: Reason | None = None
        try:
            result = call_provider(
                provider_for(prepared.provider),
                request=prepared.request,
                method=prepared.method,
            )
        except ProviderTimeoutError:
            reason = "provider_timeout"
        except ProviderUnavailableError:
            reason = "provider_unavailable"
        except GatewayInputError:
            reason = "invalid_output"
        latency_ms = min((monotonic_ns() - started) // 1_000_000, 2**31 - 1)
        if latency_ms > prepared.request.timeout_seconds * 1000:
            reason = "provider_timeout"
        with tenant_context(scope.actor_id, scope.organization_id):
            invocation = AIInvocation.objects.select_for_update().get(
                pk=prepared.invocation_id
            )
            if reason is not None:
                finish_failure(invocation, reason=reason, latency_ms=latency_ms)
            else:
                finish_success(
                    invocation_id=invocation.pk, result=result, latency_ms=latency_ms
                )
        if reason is not None:
            raise AITransportError from None
        return SendResult(provider_reference=prepared.invocation_id.hex)
