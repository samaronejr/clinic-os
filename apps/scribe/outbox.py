"""Write private media only after commit through the shared action outbox."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from django.db import connection, transaction

from apps.ai.adapters import register_provider
from apps.ai.outbox import organization_lock_key
from apps.audit.services import record_phase1_event
from apps.comms.adapters import OperationScope, PermanentSendError, SendResult
from apps.core.integration import register_action_adapter, register_subject_recheck
from apps.ehr.attachment_storage import AttachmentStorage, AttachmentStorageError
from apps.providers.services import current_version
from apps.scribe.authority import active_consent, recording_scope
from apps.scribe.contracts import ScribeError
from apps.scribe.gateway import (
    ScribeGatewayActionAdapter,
    ScribeRecordedProvider,
    ScribeSyntheticProvider,
)
from apps.scribe.gateway import (
    eligible as gateway_eligible,
)
from apps.scribe.models import AudioChunk, RecordingSession
from apps.scribe.storage import media_storage
from apps.scribe.tasks import dispatch_transcription
from apps.tenancy.db import tenant_context

if TYPE_CHECKING:
    from uuid import UUID

    from apps.comms.models import IntegrationOperation


@dataclass(frozen=True, slots=True)
class PreparedMedia:
    """A remote request carries ciphertext, never plaintext audio or data keys."""

    scope: OperationScope
    chunk_id: UUID
    key: str
    storage: AttachmentStorage
    ciphertext: bytes = field(repr=False)


class MediaTransportError(PermanentSendError):
    """Retain no object key, payload, network detail or provider response."""

    def __init__(self) -> None:
        """Expose one stable failure reason to the outbox."""
        super().__init__("media_unavailable")


def eligible(scope: OperationScope) -> bool:
    """Recheck assignment, exact consent, version and cancellation before a send."""
    chunk = AudioChunk.objects.filter(operation_id=scope.operation_id).first()
    if chunk is None or chunk.state != "buffered" or not chunk.wrapped_key:
        return False
    try:
        recording, subject = recording_scope(
            clinic_id=scope.clinic_id,
            session_id=chunk.session_id,
            consent_required=False,
        )
        # The outer outbox send holds the consent boundary lock. Taking the
        # publication purpose lock here would invert publication's lock order.
        active_consent(subject, lock=False, acceptance_id=recording.consent_id)
        version = current_version("object_storage_media", clinic_id=scope.clinic_id)
        media_storage(clinic_id=scope.clinic_id)
    except ScribeError:
        return False
    return (
        recording.physician_id == scope.actor_id
        and not recording.discarded
        and version is not None
        and version.pk == recording.capability_version_id
    )


def consent_lock_key(scope: OperationScope, _chunk_id: UUID) -> str:
    """Order provider writes against DB-enforced consent withdrawal/publication."""
    return f"scribe-consent:{scope.clinic_id}"


def register_gateway() -> None:
    """Extend inference only through the existing startup registries."""
    register_provider(ScribeSyntheticProvider())
    register_provider(ScribeRecordedProvider())
    register_action_adapter(ScribeGatewayActionAdapter())
    register_subject_recheck(
        "ai.invocation", gateway_eligible, lock_key=organization_lock_key
    )


class MediaActionAdapter:
    """Prepare ciphertext inside the tenant transaction; store it outside."""

    provider = "scribe_media"

    def prepare(self, operation: IntegrationOperation) -> PreparedMedia:
        """Load only the encrypted staged bytes of the committed subject."""
        chunk = AudioChunk.objects.get(
            pk=operation.subject_id,
            operation_id=operation.pk,
            organization_id=operation.organization_id,
            state="buffered",
        )
        if operation.payload_digest != chunk.sha256:
            raise MediaTransportError
        return PreparedMedia(
            OperationScope(
                operation.pk,
                operation.organization_id,
                operation.clinic_id,
                operation.actor_id,
            ),
            chunk.pk,
            chunk.storage_key,
            media_storage(clinic_id=operation.clinic_id),
            bytes(chunk.encrypted_data),
        )

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Use an immutable key to reconcile an interrupted or repeated upload."""
        if (
            not isinstance(prepared, PreparedMedia)
            or operation_id != prepared.scope.operation_id
            or connection.in_atomic_block
        ):
            raise MediaTransportError
        scope = prepared.scope
        storage = prepared.storage
        try:
            storage.mark_pending(prepared.key, str(scope.organization_id))
            try:
                storage.put(prepared.key, prepared.ciphertext)
            except AttachmentStorageError:
                # A worker may die after PUT. Exact ciphertext at the immutable
                # key is its receipt; different bytes can never be overwritten.
                if storage.get(prepared.key) != prepared.ciphertext:
                    raise MediaTransportError from None
            with tenant_context(scope.actor_id, scope.organization_id):
                session_id = AudioChunk.objects.values_list(
                    "session_id", flat=True
                ).get(pk=prepared.chunk_id)
                # Match ingest, transcript settlement and human review:
                # consent boundary -> session -> chunk -> transcript/audit.
                RecordingSession.objects.select_for_update().get(pk=session_id)
                if not eligible(scope):
                    raise MediaTransportError
                chunk = AudioChunk.objects.select_for_update().get(pk=prepared.chunk_id)
                chunk.state = "accepted"
                chunk.encrypted_data = b""
                chunk.save(update_fields=("state", "encrypted_data"))
                record_phase1_event(
                    "scribe.chunk.accepted",
                    clinic_id=scope.clinic_id,
                    affected_record_id=chunk.pk,
                )
                transaction.on_commit(lambda: storage.clear_pending(prepared.key))
                transaction.on_commit(partial(dispatch_transcription, scope))
        except (AttachmentStorageError, ScribeError) as error:
            raise MediaTransportError from error
        return SendResult(provider_reference=prepared.chunk_id.hex)
