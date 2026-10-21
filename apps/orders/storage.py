"""Attachment storage for original result bytes; scans never acknowledge results."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction

from apps.ehr.attachment_scanner import default_scanner
from apps.ehr.attachment_storage import FilesystemAttachmentStorage
from apps.ehr.attachments import (
    ALLOWED_TYPES,
    MAX_ATTACHMENT_BYTES,
    detect_attachment_type,
)
from apps.ehr.models import ClinicalAttachment
from apps.orders.values import INVALID
from apps.tenancy.envelope import protect, reveal

if TYPE_CHECKING:
    from uuid import UUID

    from apps.orders.models import ResultVersion
    from apps.orders.queries import PatientSource
    from apps.orders.values import ResultInput


def result_storage() -> FilesystemAttachmentStorage:
    """Separate pending receipts from the encounter-attachment reconciler."""
    return FilesystemAttachmentStorage(Path(settings.EHR_ATTACHMENT_ROOT) / "orders")


def validate_source(submission: ResultInput) -> None:
    """Reject unsafe types before any source, row, task or audit is written."""
    if (
        not submission.data
        or len(submission.data) > MAX_ATTACHMENT_BYTES
        or submission.content_type not in ALLOWED_TYPES
        or detect_attachment_type(submission.data) != submission.content_type
    ):
        raise ValidationError(INVALID)
    verdict, _reason = default_scanner().scan(
        attachment=ClinicalAttachment(size_bytes=len(submission.data)),
        data=submission.data,
    )
    if verdict != "clean":
        raise ValidationError(INVALID)


def store_source(*, key: str, organization_id: UUID, data: bytes) -> None:
    """Retain a rollback-surviving receipt and store tenant-enveloped bytes."""
    backend = result_storage()
    backend.mark_pending(key, str(organization_id))
    backend.put(key, protect(purpose="orders.result.bytes", plaintext=data))
    transaction.on_commit(lambda: backend.clear_pending(key))


def source_bytes(version: ResultVersion | PatientSource) -> bytes:
    """Verify size and SHA-256 after decrypting; callers must authorize first."""
    data = reveal(
        purpose="orders.result.bytes",
        envelope=result_storage().get(version.storage_key),
    )
    if (
        len(data) != version.size_bytes
        or hashlib.sha256(data).hexdigest() != version.sha256
    ):
        raise ValidationError(INVALID)
    return data


def reconcile_result_uploads() -> int:
    """Owner-only reconciliation preserves committed sources and removes orphans."""
    from apps.orders.models import ResultVersion  # noqa: PLC0415

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        if cursor.fetchone() != ("clinic_owner",):
            message = "result source reconciliation requires clinic_owner"
            raise PermissionError(message)
    count = 0
    backend = result_storage()
    for pending in backend.pending_uploads():
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true)",
                [pending.organization_id],
            )
            if not ResultVersion.objects.filter(storage_key=pending.key).exists():
                backend.delete(pending.key)
                count += 1
            backend.clear_pending(pending.key)
    return count
