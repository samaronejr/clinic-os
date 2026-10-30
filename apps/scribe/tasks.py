"""Reference-only media work on the governed AI-interactive queue."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from celery import shared_task

from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError
from apps.comms.models import IntegrationOperation
from apps.core.fairness import DEFER_COUNTDOWN_SECONDS, fair_acquire
from apps.core.integration import ExecutionResult, _resolve_scope, execute_operation
from apps.ehr.attachment_storage import AttachmentStorageError
from apps.scribe.contracts import ScribeError
from apps.scribe.models import AudioChunk
from apps.scribe.transcription import prepare_audio, request_transcription
from apps.scribe.transcripts import TranscriptAccessDeniedError
from apps.tenancy.db import tenant_context

if TYPE_CHECKING:
    from apps.comms.adapters import OperationScope


class TaskSender(Protocol):
    """Only the stored outbox identifier enters the broker."""

    def apply_async(
        self, *, kwargs: dict[str, str], queue: str, countdown: int = 0
    ) -> None:
        """Publish one reference-only broker message."""


def dispatch(scope: OperationScope) -> None:
    """Publish the stored outbox reference after the outer transaction commits."""
    execute.apply_async(
        kwargs={"operation_id": str(scope.operation_id)}, queue="ai-interactive"
    )


def run_operation(*, operation_id: str) -> ExecutionResult:
    """Evaluate fairness from stored tenant provenance, never broker claims."""
    scope = _resolve_scope(UUID(operation_id))
    if scope is None:
        return "missing"
    if not fair_acquire(organization_id=scope.organization_id, queue="ai-interactive"):
        execute.apply_async(
            kwargs={"operation_id": operation_id},
            queue="ai-interactive",
            countdown=DEFER_COUNTDOWN_SECONDS,
        )
        return "retry"
    return execute_operation(scope.operation_id)


execute: TaskSender = shared_task(name="apps.scribe.tasks.execute")(run_operation)


def dispatch_transcription(scope: OperationScope) -> None:
    """Only a committed media-operation reference enters the ASR queue."""
    transcribe.apply_async(
        kwargs={"operation_id": str(scope.operation_id)},
        queue="ai-interactive",
    )


def run_transcription(*, operation_id: str) -> str:
    """Fetch ciphertext outside transactions, then admit through the AI gateway."""
    scope = _resolve_scope(UUID(operation_id))
    if scope is None:
        return "transcription_pending"
    if not fair_acquire(organization_id=scope.organization_id, queue="ai-interactive"):
        transcribe.apply_async(
            kwargs={"operation_id": operation_id},
            queue="ai-interactive",
            countdown=DEFER_COUNTDOWN_SECONDS,
        )
        return "transcription_pending"
    try:
        with tenant_context(scope.actor_id, scope.organization_id):
            if not IntegrationOperation.objects.filter(
                pk=scope.operation_id,
                provider="scribe_media",
                subject_type="scribe.chunk",
            ).exists():
                return "transcription_pending"
            chunk = AudioChunk.objects.filter(
                operation_id=scope.operation_id,
                state="accepted",
            ).first()
            if chunk is None:
                return "transcription_pending"
            prepared = prepare_audio(clinic_id=scope.clinic_id, chunk_id=chunk.pk)
        ciphertext = prepared.storage.get(prepared.key)
        with tenant_context(scope.actor_id, scope.organization_id):
            return request_transcription(
                clinic_id=scope.clinic_id,
                chunk_id=prepared.chunk_id,
                ciphertext=ciphertext,
            )
    except (
        AttachmentStorageError,
        GatewayAccessDeniedError,
        GatewayRefusedError,
        ScribeError,
        TranscriptAccessDeniedError,
    ):
        return "transcription_pending"


transcribe: TaskSender = shared_task(name="apps.scribe.tasks.transcribe")(
    run_transcription
)
