"""Settle one reservation without ever copying content to a telemetry sink."""

from __future__ import annotations

from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from apps.ai.contracts import MICROS_PER_CENTAVO, ProviderResult, Reason
from apps.ai.models import AIArtifact, AICircuit, AIInvocation
from apps.audit.services import record_phase1_event
from apps.core.telemetry import METRICS

if TYPE_CHECKING:
    from uuid import UUID


def finish_failure(
    invocation: AIInvocation, *, reason: Reason, latency_ms: int = 0
) -> None:
    """Release known refusals; keep unknown provider charges reserved."""
    if invocation.status != "reserved":
        return
    invocation.status = "failed"
    invocation.reason_code = reason
    invocation.latency_ms = latency_ms
    invocation.settled_at = timezone.now()
    # Once dispatched, timeout/unavailable/invalid usage is an unknown charge.
    # Conservatively account the entire reservation, never claim zero spend.
    if reason in {"provider_timeout", "provider_unavailable", "invalid_output"}:
        invocation.cost_micros = invocation.reserved_micros
        invocation.cost_minor = (
            invocation.cost_micros + MICROS_PER_CENTAVO - 1
        ) // MICROS_PER_CENTAVO
    invocation.save(
        update_fields=(
            "status",
            "reason_code",
            "latency_ms",
            "cost_micros",
            "cost_minor",
            "settled_at",
        )
    )
    if reason == "provider_timeout":
        circuit, opened = AICircuit.objects.get_or_create(
            organization_id=invocation.organization_id,
            configuration_id=invocation.configuration_id,
            closed_at=None,
            defaults={"opened_at": timezone.now()},
        )
        if opened:
            record_phase1_event(
                "ai.circuit.opened",
                clinic_id=invocation.clinic_id,
                affected_record_id=circuit.pk,
            )
    record_phase1_event(
        "ai.invocation.failed",
        clinic_id=invocation.clinic_id,
        affected_record_id=invocation.pk,
    )
    transaction.on_commit(
        partial(
            METRICS.record_ai_invocation,
            purpose=invocation.purpose,
            outcome="timeout" if reason == "provider_timeout" else "error",
            seconds=latency_ms / 1000,
            cost_micros=invocation.cost_micros,
        )
    )


def finish_success(
    *, invocation_id: UUID, result: ProviderResult, latency_ms: int
) -> None:
    """Atomically retain encrypted output and exact metering once."""
    invocation = (
        AIInvocation.objects.select_for_update(of=("self",))
        .select_related("configuration", "inputs_ref")
        .get(pk=invocation_id)
    )
    if invocation.status != "reserved":
        return
    source = invocation.inputs_ref
    artifact = AIArtifact.objects.create(
        organization_id=invocation.organization_id,
        clinic_id=invocation.clinic_id,
        user_id=invocation.user_id,
        patient_enrollment_id=source.patient_enrollment_id,
        purpose=invocation.purpose,
        method=source.method,
        kind="output",
        invocation=invocation,
        content=result.content,
        retention_class="review_draft",
        expires_at=timezone.now() + timedelta(days=7),
    )
    from apps.ai.policy import price_for  # noqa: PLC0415 - avoid model import cycle

    invocation.tokens_in = result.usage.tokens_in
    invocation.tokens_out = result.usage.tokens_out
    invocation.tokens_cached = result.usage.tokens_cached
    invocation.audio_seconds = result.usage.audio_seconds
    invocation.cost_micros = price_for(invocation.configuration).cost(result.usage)
    invocation.cost_minor = (
        invocation.cost_micros + MICROS_PER_CENTAVO - 1
    ) // MICROS_PER_CENTAVO
    invocation.latency_ms = latency_ms
    invocation.status = "succeeded"
    invocation.settled_at = timezone.now()
    invocation.save(
        update_fields=(
            "tokens_in",
            "tokens_out",
            "tokens_cached",
            "audio_seconds",
            "cost_micros",
            "cost_minor",
            "latency_ms",
            "status",
            "settled_at",
        )
    )
    record_phase1_event(
        "ai.artifact.created",
        clinic_id=invocation.clinic_id,
        affected_record_id=artifact.pk,
    )
    record_phase1_event(
        "ai.invocation.succeeded",
        clinic_id=invocation.clinic_id,
        affected_record_id=invocation.pk,
    )
    transaction.on_commit(
        partial(
            METRICS.record_ai_invocation,
            purpose=invocation.purpose,
            outcome="success",
            seconds=latency_ms / 1000,
            cost_micros=invocation.cost_micros,
        )
    )
