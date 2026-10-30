"""One governed admission path; model calls run only through the action outbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection, transaction
from django.utils.translation import gettext_noop

from apps.ai.artifacts import input_for
from apps.ai.contracts import (
    Budget,
    GatewayAccessDeniedError,
    GatewayRefusedError,
    InvocationResult,
    Purpose,
    Reason,
)
from apps.ai.models import AIInvocation
from apps.ai.policy import enabled_policy, price_for, select_configuration
from apps.ai.prompt_registry import load_prompt
from apps.audit.services import record_phase1_event
from apps.core.integration import ActionOperationRequest, enqueue_operation
from apps.identity.current_context import current_actor_id

if TYPE_CHECKING:
    from uuid import UUID

__all__ = ("invoke",)

_MANUAL_MESSAGE = gettext_noop("AI is unavailable. Continue working manually.")


def _refused(reason: Reason) -> InvocationResult:
    return InvocationResult(None, None, reason, _MANUAL_MESSAGE)


@transaction.atomic
def invoke(
    *,
    clinic_id: UUID,
    purpose: Purpose,
    principal: UUID,
    inputs_ref: UUID,
    budget: Budget,
) -> InvocationResult:
    """Reserve exact worst-case spend, then enqueue only an opaque reference.

    ``principal`` is an assertion about the current human GUC, never authority.
    The existing action outbox intentionally refuses machine impersonation.
    Input references are single-use idempotency keys; replays keep original terms.
    """
    if principal != current_actor_id():
        raise GatewayAccessDeniedError
    artifact = input_for(clinic_id=clinic_id, inputs_ref=inputs_ref, purpose=purpose)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"ai-budget:{artifact.organization_id}"],
        )
    try:
        policy = enabled_policy(purpose)
        previous = AIInvocation.objects.filter(inputs_ref_id=inputs_ref).first()
        if previous is not None:
            if (
                previous.limit_tokens_in != budget.limits.tokens_in
                or previous.limit_tokens_out != budget.limits.tokens_out
                or previous.limit_audio_seconds != budget.limits.audio_seconds
                or previous.reserved_micros > budget.maximum_micros
            ):
                return _refused("budget_exhausted")
            return InvocationResult(previous.pk, previous.operation_id)
        configuration = select_configuration(clinic_id=clinic_id, purpose=purpose)
        reserved = price_for(configuration).reservation(budget.limits)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.ai_budget_usage(%s)", [purpose.value]
            )
            usage = cursor.fetchone()
        if usage is None or (
            reserved > budget.maximum_micros
            or int(usage[0]) + reserved > policy.monthly_budget_micros
            or int(usage[1]) + reserved > dict(policy.purpose_budgets)[purpose.value]
        ):
            return _refused("budget_exhausted")
    except GatewayRefusedError as error:
        return _refused(error.reason)
    _prompt, prompt_sha256 = load_prompt(purpose)
    invocation = AIInvocation.objects.create(
        organization_id=artifact.organization_id,
        clinic_id=clinic_id,
        user_id=principal,
        purpose=purpose.value,
        configuration=configuration,
        config_version=configuration.version,
        prompt_sha256=prompt_sha256,
        inputs_ref=artifact,
        limit_tokens_in=budget.limits.tokens_in,
        limit_tokens_out=budget.limits.tokens_out,
        limit_audio_seconds=budget.limits.audio_seconds,
        reserved_micros=reserved,
    )
    record_phase1_event(
        "ai.invocation.reserved", clinic_id=clinic_id, affected_record_id=invocation.pk
    )
    operation_id = enqueue_operation(
        ActionOperationRequest(
            provider="ai_gateway",
            clinic_id=clinic_id,
            subject_type="ai.invocation",
            subject_id=invocation.pk,
            idempotency_key=invocation.pk,
            payload_digest=prompt_sha256,
        )
    )
    invocation.operation_id = operation_id
    invocation.save(update_fields=("operation_id",))
    return InvocationResult(invocation.pk, operation_id)
