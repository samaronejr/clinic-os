"""Attachment lifecycle: validate, quarantine, scan and authorized download.

Every predicate is rechecked against canonical role assignments and the
encounter's care relationship inside the current tenant transaction; FORCE
RLS enforces the same rules independently. Object keys are storage details,
never identifiers or authority.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.audit.canonical import AuditEventInput
from apps.audit.services import record_event, record_phase1_event
from apps.ehr.attachment_scanner import (
    AttachmentScanner,
    AttachmentScanUnavailableError,
    default_scanner,
)
from apps.ehr.attachment_storage import (
    AttachmentStorage,
    AttachmentStorageError,
    default_storage,
)
from apps.ehr.models import ClinicalAttachment, Encounter
from apps.ehr.services import ClinicalAccessDeniedError, _denied
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_current_actor_clinic_roles,
)
from apps.identity.models import UserClinicRole
from apps.tenancy.envelope import EnvelopeError, protect, reveal

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_FILE_NAME = 120
ALLOWED_TYPES: tuple[str, ...] = ("application/pdf", "image/jpeg", "image/png")
EXTENSIONS: dict[str, str] = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
}
_ARCHIVE_MAGIC = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"\x1f\x8b\x08",
    b"Rar!\x1a\x07",
    b"7z\xbc\xaf",
    b"BZh",
    b"\xfd7zXZ",
)
_USTAR_MAGIC = b"ustar"
_USTAR_OFFSET = 257
_USTAR_END = 262


class AttachmentScanFailedError(Exception):
    """Report a scan that could not complete; the row stays quarantined."""


@dataclass(frozen=True)
class AttachmentInput:
    """Bounded caller-supplied file facts for one upload."""

    file_name: str
    declared_type: str
    data: bytes


@dataclass(frozen=True)
class AttachmentContext:
    """The encounter's visible attachments plus the actor's write authority."""

    attachments: list[ClinicalAttachment]
    can_write: bool


@dataclass(frozen=True)
class AttachmentDownload:
    """Fully materialized, authorized bytes ready for a bounded response."""

    data: bytes
    content_type: str
    file_name: str


def detect_attachment_type(data: bytes) -> str | None:
    """Classify bytes by magic signature; unknown content returns ``None``."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def _rejected_kind(data: bytes) -> str | None:
    """Name the disallowed container or markup family, if any."""
    head = data[:512]
    if head.lstrip()[:1] == b"<":
        return "active_markup"
    if (
        any(data.startswith(magic) for magic in _ARCHIVE_MAGIC)
        or data[_USTAR_OFFSET:_USTAR_END] == _USTAR_MAGIC
    ):
        return "archive"
    return None


def _clean_file_name(name: str) -> str:
    """Keep a bounded display name; it is never a path or a response header."""
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    normalized = unicodedata.normalize("NFC", base).strip()
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", "", normalized)[:MAX_FILE_NAME].strip(" .")
    return cleaned or "anexo"


def authorize_attachment_encounter(
    clinic_id: UUID, encounter_id: UUID, *, write: bool = False
) -> Encounter:
    """Recheck the clinical predicate for this encounter on every call."""
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    try:
        require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    except CurrentActorError:
        _denied(clinic_id, encounter_id, "role_denied")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ehr_assigned(%s), clinic_app.ehr_care(%s)",
            [encounter_id, encounter_id],
        )
        assigned, care = cursor.fetchone() or (False, False)
    if not care or (write and not assigned):
        _denied(
            clinic_id, encounter_id, "not_assigned" if write else "no_care_relationship"
        )
    return encounter


def attachment_context(encounter: Encounter) -> AttachmentContext:
    """List the attachments this principal may see; quarantine stays private."""
    attachments = list(
        ClinicalAttachment.objects.filter(encounter=encounter).order_by("created_at")
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ehr_assigned(%s)", [encounter.pk])
        can_write = bool(cursor.fetchone()[0])
    return AttachmentContext(attachments, can_write)


def upload_attachment(
    *,
    clinic_id: UUID,
    encounter_id: UUID,
    upload: AttachmentInput,
    storage: AttachmentStorage | None = None,
) -> ClinicalAttachment:
    """Validate declared and detected type, then store bytes quarantined.

    Validation failures write nothing. A durable pending receipt precedes the
    object write and clears on commit, so a request-transaction rollback or a
    failed cleanup always leaves tracked bytes for ``reconcile_pending_uploads``
    — never an untracked orphan.
    """
    encounter = authorize_attachment_encounter(clinic_id, encounter_id, write=True)
    data = upload.data
    if (
        not data
        or len(data) > MAX_ATTACHMENT_BYTES
        or upload.declared_type not in ALLOWED_TYPES
        or _rejected_kind(data) is not None
        or detect_attachment_type(data) != upload.declared_type
    ):
        message = (
            "Envie um arquivo PDF, JPEG ou PNG válido de até 10 MiB. "
            "O conteúdo precisa corresponder ao tipo declarado."
        )
        raise ValidationError(message)
    backend = storage if storage is not None else default_storage()
    key = secrets.token_hex(32)
    # The receipt is written before the object so a request-transaction
    # rollback or crash can never leave untracked bytes; it is cleared only
    # after the outermost transaction commits.
    backend.mark_pending(key, str(encounter.organization_id))
    try:
        # Only the tenant envelope reaches object storage; the recorded
        # digest and size describe the plaintext the scanner and downloads
        # verify after decryption.
        backend.put(
            key,
            protect(purpose="ehr.clinicalattachment.bytes", plaintext=data),
        )
        with transaction.atomic():
            attachment = ClinicalAttachment.objects.create(
                organization_id=encounter.organization_id,
                clinic_id=clinic_id,
                encounter=encounter,
                patient_id=encounter.patient_id,
                uploader_id=current_actor_id(),
                storage_key=key,
                file_name=_clean_file_name(upload.file_name),
                declared_type=upload.declared_type,
                detected_type=upload.declared_type,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
            record_phase1_event(
                "ehr.attachment.uploaded",
                clinic_id=clinic_id,
                affected_record_id=attachment.pk,
            )
    except (DatabaseError, OSError, AttachmentStorageError):
        # A failed delete keeps the receipt, so cleanup stays recoverable.
        backend.delete(key)
        backend.clear_pending(key)
        raise
    transaction.on_commit(lambda: backend.clear_pending(key))
    return attachment


def reconcile_pending_uploads(*, storage: AttachmentStorage | None = None) -> list[str]:
    """Remove objects whose upload transaction never committed.

    Every stored object is tracked by a durable receipt until its row commits,
    so a request rollback leaves cleanup metadata instead of an orphan. The
    existence check must run as ``clinic_owner`` (or a superuser/BYPASSRLS
    role): the runtime role's read policy hides committed rows and would
    misreport them as orphans. Run during maintenance, when no upload
    transaction is in flight.
    """
    backend = storage if storage is not None else default_storage()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user = 'clinic_owner' OR "
            "(SELECT rolsuper OR rolbypassrls FROM pg_roles "
            "WHERE rolname = current_user)"
        )
        privileged = bool(cursor.fetchone()[0])
    if not privileged:
        raise AttachmentStorageError
    removed: list[str] = []
    for pending in backend.pending_uploads():
        try:
            organization_id = UUID(pending.organization_id)
        except ValueError:
            continue  # A malformed receipt stays for operator review.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(organization_id)],
            )
            exists = ClinicalAttachment.objects.filter(storage_key=pending.key).exists()
            if exists:
                # The row committed but its receipt survived; just clear it.
                backend.clear_pending(pending.key)
            else:
                backend.delete(pending.key)
                backend.clear_pending(pending.key)
                removed.append(pending.key)
    return removed


def _record_scan_failure(attachment: ClinicalAttachment, reason: str) -> None:
    record_event(
        AuditEventInput(
            event_type="ehr.attachment.scan_failed",
            component_id="clinic-os-web",
            component_ip=None,
            affected_record_type="ehr.clinical_attachment",
            affected_record_id=str(attachment.pk),
            occurred_at_utc=timezone.now(),
        ),
        payload={"clinic_id": str(attachment.clinic_id), "reason_code": reason},
    )


def scan_attachment(
    *,
    clinic_id: UUID,
    attachment_id: UUID,
    storage: AttachmentStorage | None = None,
    scanner: AttachmentScanner | None = None,
) -> ClinicalAttachment:
    """Run the configured scanner once per attempt; retries stay bounded.

    A completed scan is terminal. A scanner failure keeps the row quarantined
    with incremented attempts and a fixed reason, so the caller can retry or
    clean up without ever serving the bytes.
    """
    row = ClinicalAttachment.objects.filter(
        pk=attachment_id, clinic_id=clinic_id
    ).first()
    if row is None:
        raise ClinicalAccessDeniedError
    authorize_attachment_encounter(clinic_id, row.encounter_id, write=True)
    backend = storage if storage is not None else default_storage()
    engine = scanner if scanner is not None else default_scanner()
    failure: str | None = None
    with transaction.atomic():
        locked = (
            ClinicalAttachment.objects.select_for_update()
            .filter(pk=row.pk, state="quarantined")
            .first()
        )
        if locked is None:
            # A concurrent scan already finished; return its terminal result.
            return ClinicalAttachment.objects.get(pk=row.pk)
        try:
            data = reveal(
                purpose="ehr.clinicalattachment.bytes",
                envelope=backend.get(locked.storage_key),
            )
        except AttachmentStorageError:
            failure = "storage_unavailable"
            verdict, reason = "", ""
        except EnvelopeError:
            # An undecryptable object is a storage-integrity failure.
            verdict, reason = "rejected", "digest_mismatch"
        else:
            if hashlib.sha256(data).hexdigest() != locked.sha256:
                verdict, reason = "rejected", "digest_mismatch"
            else:
                try:
                    verdict, reason = engine.scan(attachment=locked, data=data)
                except AttachmentScanUnavailableError:
                    failure = "scan_unavailable"
                    verdict, reason = "", ""
        locked.scan_attempts += 1
        if failure is not None:
            # Retry metadata commits before the failure propagates.
            locked.scan_reason = failure
            locked.save(update_fields=("scan_attempts", "scan_reason"))
            _record_scan_failure(locked, failure)
        else:
            locked.state = (
                ClinicalAttachment.State.AVAILABLE
                if verdict == "clean"
                else ClinicalAttachment.State.REJECTED
            )
            locked.scan_reason = reason
            locked.scanned_at = timezone.now()
            locked.save(
                update_fields=(
                    "state",
                    "scan_attempts",
                    "scan_reason",
                    "scanned_at",
                )
            )
            record_phase1_event(
                "ehr.attachment.scanned",
                clinic_id=clinic_id,
                affected_record_id=locked.pk,
            )
    if failure is not None:
        raise AttachmentScanFailedError
    return locked


def download_attachment(
    *,
    clinic_id: UUID,
    attachment_id: UUID,
    storage: AttachmentStorage | None = None,
) -> AttachmentDownload:
    """Authorize first, then materialize the complete object in one response.

    Only ``available`` rows are served, and only to the assigned physician or
    a same-clinic physician with a care relationship. Quarantined or rejected
    bytes are never returned.
    """
    row = ClinicalAttachment.objects.filter(
        pk=attachment_id, clinic_id=clinic_id
    ).first()
    if row is None:
        raise ClinicalAccessDeniedError
    authorize_attachment_encounter(clinic_id, row.encounter_id)
    if row.state != ClinicalAttachment.State.AVAILABLE:
        _denied(clinic_id, row.pk, "not_available")
    backend = storage if storage is not None else default_storage()
    try:
        data = reveal(
            purpose="ehr.clinicalattachment.bytes",
            envelope=backend.get(row.storage_key),
        )
    except (AttachmentStorageError, EnvelopeError) as error:
        raise AttachmentStorageError from error
    if hashlib.sha256(data).hexdigest() != row.sha256:
        raise AttachmentStorageError
    record_phase1_event(
        "ehr.attachment.downloaded",
        clinic_id=clinic_id,
        affected_record_id=row.pk,
    )
    return AttachmentDownload(
        data=data,
        content_type=row.detected_type,
        file_name=f"anexo-{row.pk}.{EXTENSIONS[row.detected_type]}",
    )
