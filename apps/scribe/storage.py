"""Per-object pgcrypto keys, tenant wrapping and capability-gated object storage."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from config.settings.contracts import SYNTHETIC_DATA_MODE, resolved_data_mode
from django.conf import settings
from django.db import DatabaseError, connection

from apps.core.secrets import SecretStoreContractError, SecretUnavailableError
from apps.ehr.attachment_storage import (
    AttachmentStorage,
    AttachmentStorageError,
    FilesystemAttachmentStorage,
)
from apps.providers.services import current_version, is_live
from apps.scribe.contracts import MAX_CHUNK_BYTES, Reason, ScribeError
from apps.tenancy.envelope import EnvelopeError, protect, reveal

if TYPE_CHECKING:
    from uuid import UUID


class MediaStorage(AttachmentStorage, Protocol):
    """AttachmentStorage remains the byte-object contract for private media."""


@dataclass(frozen=True, slots=True)
class SealedAudio:
    """Only authenticated ciphertext and a separately wrapped key leave sealing."""

    ciphertext: bytes = field(repr=False)
    wrapped_key: bytes = field(repr=False)


def seal_audio(*, key: str, data: bytes) -> SealedAudio:
    """Use reviewed pgcrypto AES-256/MDC, not an application-written cipher."""
    object_key = secrets.token_hex(32)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.pgp_sym_encrypt_bytea(%s,%s,"
                "'cipher-algo=aes256,compress-algo=0')",
                [data, object_key],
            )
            row = cursor.fetchone()
        if row is None:
            raise ScribeError(Reason.STORAGE_UNAVAILABLE)
        wrapped = protect(
            purpose=f"scribe.object-key:{key}", plaintext=object_key.encode("ascii")
        )
    except (DatabaseError, EnvelopeError) as error:
        raise ScribeError(Reason.STORAGE_UNAVAILABLE) from error
    return SealedAudio(bytes(row[0]), wrapped)


def media_storage(*, clinic_id: UUID) -> AttachmentStorage:
    """No environment flag can approve a real storage provider."""
    from apps.scribe.s3_storage import configured_storage  # noqa: PLC0415

    version = current_version("object_storage_media", clinic_id=clinic_id)
    if version is None or version.state in {"revoked", "degraded"}:
        raise ScribeError(Reason.STORAGE_UNAVAILABLE)
    if resolved_data_mode() == SYNTHETIC_DATA_MODE:
        if not getattr(settings, "SCRIBE_SYNTHETIC_CAPTURE", False):
            raise ScribeError(Reason.STORAGE_UNAVAILABLE)
        if not settings.SCRIBE_DEV_MINIO:
            return FilesystemAttachmentStorage(Path(settings.SCRIBE_MEDIA_ROOT))
    elif not is_live("object_storage_media", clinic_id=clinic_id):
        raise ScribeError(Reason.STORAGE_UNAVAILABLE)
    try:
        return configured_storage(
            dev=resolved_data_mode() == SYNTHETIC_DATA_MODE,
        )
    except (
        AttachmentStorageError,
        SecretStoreContractError,
        SecretUnavailableError,
    ) as error:
        raise ScribeError(Reason.STORAGE_UNAVAILABLE) from error


def open_audio(*, clinic_id: UUID, chunk_id: UUID) -> bytes:
    """Authorize the stored subject before resolving its opaque object key."""
    from apps.scribe.authority import active_consent, recording_scope  # noqa: PLC0415
    from apps.scribe.models import AudioChunk  # noqa: PLC0415

    chunk = AudioChunk.objects.filter(pk=chunk_id, state="accepted").first()
    if chunk is None:
        raise ScribeError
    recording, subject = recording_scope(
        clinic_id=clinic_id, session_id=chunk.session_id, consent_required=False
    )
    active_consent(subject, lock=False, acceptance_id=recording.consent_id)
    if not chunk.wrapped_key:
        raise ScribeError(Reason.AUDIO_UNAVAILABLE)
    try:
        ciphertext = media_storage(clinic_id=clinic_id).get(chunk.storage_key)
        key = reveal(
            purpose=f"scribe.object-key:{chunk.storage_key}",
            envelope=bytes(chunk.wrapped_key),
        ).decode("ascii")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.pgp_sym_decrypt_bytea(%s,%s)",
                [ciphertext, key],
            )
            row = cursor.fetchone()
    except (
        AttachmentStorageError,
        DatabaseError,
        EnvelopeError,
        UnicodeError,
    ) as error:
        raise ScribeError(Reason.AUDIO_UNAVAILABLE) from error
    if row is None:
        raise ScribeError(Reason.AUDIO_UNAVAILABLE)
    data = bytes(row[0])
    if (
        len(data) > MAX_CHUNK_BYTES
        or len(data) != chunk.size_bytes
        or sha256(data).hexdigest() != chunk.sha256
    ):
        raise ScribeError(Reason.AUDIO_UNAVAILABLE)
    recording, subject = recording_scope(
        clinic_id=clinic_id, session_id=chunk.session_id, consent_required=False
    )
    active_consent(subject, lock=False, acceptance_id=recording.consent_id)
    current = AudioChunk.objects.filter(pk=chunk.pk, state="accepted").first()
    if recording.discarded or current is None or not current.wrapped_key:
        raise ScribeError(Reason.AUDIO_UNAVAILABLE)
    return data
