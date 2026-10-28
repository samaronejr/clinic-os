"""Register consent-bound ASR without a generic gateway dependency on scribe."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from django.db import transaction

from apps.ai.adapters import (
    RecordedFixtureProvider,
    SyntheticProvider,
)
from apps.ai.completion import finish_failure, finish_success
from apps.ai.contracts import (
    GatewayInputError,
    GatewayRefusedError,
    ProviderResult,
    ProviderUnavailableError,
    Purpose,
    Usage,
)
from apps.ai.models import AIInvocation
from apps.ai.outbox import (
    GatewayActionAdapter,
)
from apps.ai.outbox import (
    eligible as gateway_eligible,
)
from apps.comms.adapters import TransientSendError
from apps.comms.models import IntegrationOperation
from apps.scribe.asr import decode_audio, parse_batch, recorded_gateway_result
from apps.scribe.draft_contracts import (
    DraftAccessDeniedError,
    DraftConflictError,
    DraftInputError,
    synthetic_reply,
)
from apps.scribe.drafting import complete_draft, eligible_draft, materialize_draft
from apps.scribe.transcript_contracts import TranscriptError
from apps.scribe.transcription import (
    complete_transcription,
    eligible_input,
    materialize_input,
)
from apps.scribe.transcripts import TranscriptAccessDeniedError

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.contracts import ProviderRequest
    from apps.ai.outbox import PreparedInference
    from apps.comms.adapters import OperationScope


def _recorded(
    provider: SyntheticProvider, request: ProviderRequest
) -> ProviderResult | None:
    provider.check_boundary(request)
    try:
        content = recorded_gateway_result(request.content)
        if content is None:
            return None
        parse_batch(content, duration_ms=decode_audio(request.content).duration_ms)
    except TranscriptError as error:
        raise GatewayInputError from error
    return ProviderResult(content, Usage(audio_seconds=request.limits.audio_seconds))


def _drafted(provider: SyntheticProvider, request: ProviderRequest) -> ProviderResult:
    provider.check_boundary(request)
    try:
        content = synthetic_reply(request.content)
    except DraftInputError as error:
        raise GatewayInputError from error
    return ProviderResult(
        content,
        Usage(
            tokens_in=min(request.limits.tokens_in, 4),
            tokens_out=min(request.limits.tokens_out, 6),
        ),
    )


class ScribeSyntheticProvider(SyntheticProvider):
    """Add the closed synthetic audio envelope to the existing provider."""

    def transcribe(self, *, request: ProviderRequest) -> ProviderResult:
        """Keep fixture decoding and result validation inside the domain adapter."""
        return _recorded(self, request) or super().transcribe(request=request)

    def complete(self, *, request: ProviderRequest) -> ProviderResult:
        """Extract only source-exact suggestions from the closed draft envelope."""
        if '"schema": "scribe-material-v1"' not in request.content:
            return super().complete(request=request)
        return _drafted(self, request)


class ScribeRecordedProvider(RecordedFixtureProvider):
    """Preserve the generic fixture model and its non-scribe methods."""

    def transcribe(self, *, request: ProviderRequest) -> ProviderResult:
        """Replay only the approved fixture model with closed ASR output."""
        if request.model_id != "fixture-v1":
            raise ProviderUnavailableError
        return _recorded(self, request) or super().transcribe(request=request)

    def complete(self, *, request: ProviderRequest) -> ProviderResult:
        """Retain the recorded model boundary for offline source extraction."""
        if '"schema": "scribe-material-v1"' not in request.content:
            return super().complete(request=request)
        if request.model_id != "fixture-v1":
            raise ProviderUnavailableError
        return _drafted(self, request)


def eligible(scope: OperationScope) -> bool:
    """Extend the existing subject recheck with current audio authority."""
    if not gateway_eligible(scope):
        return False
    invocation = AIInvocation.objects.select_related("inputs_ref").get(
        pk=IntegrationOperation.objects.values_list("subject_id", flat=True).get(
            pk=scope.operation_id
        )
    )
    if invocation.purpose == Purpose.SCRIBE_DRAFT:
        try:
            if eligible_draft(invocation.inputs_ref):
                return True
        except (
            DraftAccessDeniedError,
            DraftConflictError,
            DraftInputError,
            GatewayRefusedError,
        ):
            pass
        finish_failure(invocation, reason="authority_revoked")
        return False
    if invocation.purpose != Purpose.SCRIBE_ASR:
        return True
    if eligible_input(invocation.inputs_ref):
        return True
    finish_failure(invocation, reason="authority_revoked")
    return False


class ScribeGatewayActionAdapter(GatewayActionAdapter):
    """Materialize and project ASR inside the registered shared outbox adapter."""

    def prepare(self, operation: IntegrationOperation) -> PreparedInference:
        """Commit an authority refusal before the outbox's final subject recheck."""
        prepared = super().prepare(operation)
        invocation = AIInvocation.objects.select_related("inputs_ref").get(
            pk=prepared.invocation_id
        )
        if invocation.purpose == Purpose.SCRIBE_DRAFT:
            try:
                content = materialize_draft(invocation.inputs_ref)
            except (
                DraftAccessDeniedError,
                DraftConflictError,
                DraftInputError,
                GatewayRefusedError,
            ):
                finish_failure(invocation, reason="authority_revoked")
                return prepared
            return (
                replace(prepared, request=replace(prepared.request, content=content))
                if content is not None
                else prepared
            )
        if invocation.purpose != Purpose.SCRIBE_ASR:
            return prepared
        try:
            content = materialize_input(invocation.inputs_ref)
        except (TranscriptError, TranscriptAccessDeniedError):
            # Returning commits this failure. Raising inside core's preparation
            # transaction would roll it back. The final recheck refuses the
            # failed invocation before the provider can receive this object.
            finish_failure(invocation, reason="authority_revoked")
            return prepared
        return replace(prepared, request=replace(prepared.request, content=content))

    def settle_success(
        self, *, invocation_id: UUID, result: ProviderResult, latency_ms: int
    ) -> None:
        """Roll back a failed projection and let the shared outbox release it."""
        if AIInvocation.objects.filter(
            pk=invocation_id, purpose=Purpose.SCRIBE_DRAFT
        ).exists():
            try:
                with transaction.atomic():
                    finish_success(
                        invocation_id=invocation_id,
                        result=result,
                        latency_ms=latency_ms,
                        project=partial(
                            complete_draft,
                            invocation_id=invocation_id,
                            content=result.content,
                        ),
                    )
            except DraftInputError:
                invocation = AIInvocation.objects.get(pk=invocation_id)
                finish_failure(
                    invocation, reason="invalid_output", latency_ms=latency_ms
                )
            return
        try:
            finish_success(
                invocation_id=invocation_id,
                result=result,
                latency_ms=latency_ms,
                project=partial(
                    complete_transcription,
                    invocation_id=invocation_id,
                    content=result.content,
                ),
            )
        except TranscriptError as error:
            raise TranscriptSettlementError from error


class TranscriptSettlementError(TransientSendError):
    """Keep projection conflicts and capacity failures content-free."""

    def __init__(self) -> None:
        """Expose only a stable retry reason to the shared outbox."""
        super().__init__("transcript unavailable")
