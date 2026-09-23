"""Owner-side migration of already stored attachment objects to envelopes.

New uploads are encrypted before ``put``; this boundary re-encrypts objects
stored before the envelope boundary existed. It runs as ``clinic_owner``
with the tenant GUC bound per organization, reads each stored object,
classifies it (already an envelope, legacy plaintext, or unreadable), and
atomically replaces legacy bytes with the tenant envelope. The recorded
``sha256``/``size_bytes`` describe plaintext, so a migrated object decrypts
to exactly the bytes the row already binds — digests never change.

Every step is idempotent: an interrupted run reclassifies each object, so
re-running resumes without double-encrypting. Failures are counted per
object and reported in the receipt; the command exits nonzero when any
object could not be accounted for.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from django.db import connection, transaction

from apps.ehr.attachment_storage import (
    AttachmentStorage,
    AttachmentStorageError,
    default_storage,
)
from apps.ehr.models import ClinicalAttachment
from apps.tenancy.envelope import EnvelopeError, protect, reveal

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

ATTACHMENT_PURPOSE = "ehr.clinicalattachment.bytes"
_ENVELOPE_MARKER = 0x01


@dataclass(frozen=True)
class ObjectMigrationReceipt:
    """Payload-free counts for one owner-side object migration run."""

    organizations: int
    already_enveloped: int
    migrated: int
    failed: int


def _classify(*, row: ClinicalAttachment, backend: AttachmentStorage) -> str:
    """Return 'enveloped', 'legacy', or 'failed' for one stored object.

    An object is an envelope only when it decrypts under this tenant's key
    to bytes matching the recorded digest; a marker-prefixed object that
    fails decryption is corrupt, never plaintext. Legacy plaintext must
    match the recorded digest exactly before it is re-encrypted.
    """
    try:
        data = backend.get(row.storage_key)
    except AttachmentStorageError:
        return "failed"
    if data[:1] == bytes([_ENVELOPE_MARKER]):
        try:
            plaintext = reveal(purpose=ATTACHMENT_PURPOSE, envelope=data)
        except EnvelopeError:
            return "failed"
        return (
            "enveloped"
            if hashlib.sha256(plaintext).hexdigest() == row.sha256
            else "failed"
        )
    if hashlib.sha256(data).hexdigest() != row.sha256:
        return "failed"
    try:
        backend.replace(
            row.storage_key,
            protect(purpose=ATTACHMENT_PURPOSE, plaintext=data),
        )
        verified = reveal(
            purpose=ATTACHMENT_PURPOSE, envelope=backend.get(row.storage_key)
        )
    except (AttachmentStorageError, EnvelopeError):
        return "failed"
    return "migrated" if verified == data else "failed"


def migrate_attachment_objects(
    organization_ids: Iterable[UUID],
    *,
    storage: AttachmentStorage | None = None,
) -> ObjectMigrationReceipt:
    """Re-encrypt every stored attachment object under its tenant envelope.

    Runs as ``clinic_owner``; organizations are explicit inputs because the
    owner cannot enumerate them without a tenant context. Each
    organization's objects are processed in one transaction that binds
    ``app.current_tenant`` so ``protect``/``reveal`` resolve that tenant's
    DEK. The row scan uses the owner ``setup_tenant`` policy, so no RLS or
    trigger is ever suspended.
    """
    backend = storage if storage is not None else default_storage()
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        if cursor.fetchone() != ("clinic_owner",):
            raise AttachmentStorageError
    enveloped = migrated = failed = 0
    seen = 0
    for organization_id in organization_ids:
        seen += 1
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(organization_id)],
            )
            rows = ClinicalAttachment.objects.filter(
                organization_id=organization_id
            ).order_by("storage_key")
            for row in rows:
                outcome = _classify(row=row, backend=backend)
                if outcome == "enveloped":
                    enveloped += 1
                elif outcome == "migrated":
                    migrated += 1
                else:
                    failed += 1
    return ObjectMigrationReceipt(
        organizations=seen,
        already_enveloped=enveloped,
        migrated=migrated,
        failed=failed,
    )


def receipt_json(receipt: ObjectMigrationReceipt) -> str:
    """Serialize the receipt as canonical JSON for evidence capture."""
    return json.dumps(asdict(receipt), separators=(",", ":"), sort_keys=True)
