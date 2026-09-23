"""Privacy-limited public verification, release, revocation and delivery.

The QR handle resolves only to a minimal verification result: status,
document version, digests, issuer/clinic labels and the recorded issuance
time. It never returns patient or clinical content and never grants
download access — the handle is a public token, not an authority.
Anonymous lookups are rate-limited per probe through a resolver-owned
allowance table the runtime role cannot read or reset.

A status is published only after actual verification: the privileged
database resolver recomputes the stored digests and independently
verifies the signed bytes inside its own boundary before reporting
``issued`` or ``rehearsal_complete``. Signed bytes never leave the
resolver — the public projection carries only the verdict, so the QR
handle can never grant document access. A disabled verifier reports
``unavailable``, never a false valid status. Revocation and
supersession are recorded on separate rows; signed bytes are never
rewritten.

Patient downloads require an explicit issuer release and a live patient
session with the ``records`` operation; every read goes through resolver
functions that re-validate the session. Approved delivery goes through
the shared comms outbox as a link-only message — never an attachment.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast
from uuid import NAMESPACE_DNS, uuid5

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone

from apps.audit.canonical import AuditEventInput
from apps.audit.services import (
    AuditTrustedContext,
    _content_hash,
    _normalize_payload,
    record_event,
)
from apps.comms.models import IntegrationOperation
from apps.core.integration import (
    OperationRequest,
    enqueue_operation,
    hold_subject_mutation_key,
)
from apps.core.secrets import secret_store
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    _denied,
    record_denial,
)
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_current_actor_clinic_roles,
)
from apps.identity.models import UserClinicRole
from apps.intake.models import PatientContact
from apps.prescription.models import (
    COMPLETED_SIGNATURE_STATES,
    PrescriptionDocument,
    PrescriptionDocumentRelease,
    PrescriptionDocumentRevocation,
    SignatureOperation,
)
from apps.prescription.signature_provider import signature_capability
from apps.tenancy.envelope import KEK_SECRET_NAME

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from apps.comms.adapters import OperationScope

DELIVERY_SUBJECT_TYPE = "prescription.document"
DELIVERY_PROVIDER = "document-delivery-v1"
DELIVERY_CHANNEL = "email"
DELIVERY_IDEMPOTENCY_NAMESPACE = uuid5(
    NAMESPACE_DNS, "clinic-os.invalid prescription-document-delivery"
)
VERIFY_MAX_LOOKUPS = 30
VERIFY_WINDOW_SECONDS = 60
VERIFY_HANDLE_MIN = 32
VERIFY_HANDLE_MAX = 64
_VERIFY_COLUMNS = 7

type VerificationStatus = Literal[
    "issued",
    "rehearsal_complete",
    "superseded",
    "revoked",
    "invalid",
    "unavailable",
]


class VerificationLimitedError(Exception):
    """Report that the anonymous probe exhausted its lookup allowance."""


class VerificationUnavailableError(Exception):
    """Report that the verification boundary cannot answer now."""


@dataclass(frozen=True, kw_only=True)
class VerificationResult:
    """The minimal public verdict; never patient or clinical content."""

    status: VerificationStatus
    document_version: int | None = None
    issued_at: datetime | None = None
    content_digest: str | None = None
    signed_digest: str | None = None
    issuer_label: str | None = None
    clinic_label: str | None = None


@dataclass(frozen=True, kw_only=True)
class PatientDocument:
    """One released document's metadata for the patient surface."""

    document_id: UUID
    document_version: int
    state: str
    issued_at: datetime
    verification_url: str


@dataclass(frozen=True, kw_only=True)
class PatientDocumentDownload:
    """Fully materialized released bytes for a bounded response."""

    data: bytes
    content_type: str
    file_name: str


def _probe_key(remote_addr: str | None) -> bytes:
    """Derive the stored probe key; raw addresses are never persisted."""
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        (remote_addr or "").encode("utf-8"),
        hashlib.sha256,
    ).digest()


def _allowance(probe: bytes) -> bool:
    """Consume one anonymous lookup from the probe's fixed window."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.prescription_verify_allowance(%s, %s, %s)",
            [probe, VERIFY_MAX_LOOKUPS, VERIFY_WINDOW_SECONDS],
        )
        row = cursor.fetchone()
    return bool(row and row[0])


@dataclass(frozen=True, kw_only=True)
class _VerifyRow:
    """The resolver's fixed projection, type-checked at the boundary.

    The projection is byte-free by contract: the resolver verifies the
    signature internally and returns only the verdict and its metadata.
    """

    status: str
    document_version: int | None
    issued_at: datetime | None
    content_digest: str | None
    signed_digest: str | None
    issuer_label: str | None
    clinic_label: str | None


def _verify_row(handle: str) -> _VerifyRow:
    """Resolve the handle through the minimal-projection resolver."""
    allow_synthetic = (
        settings.CLINIC_DATA_MODE == "synthetic"
        and signature_capability().synthetic_enabled
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.prescription_verify(%s, %s, %s)",
            [secret_store().get_secret(KEK_SECRET_NAME), handle, allow_synthetic],
        )
        row = cursor.fetchone()
    if row is None or len(row) != _VERIFY_COLUMNS:
        raise VerificationUnavailableError
    return _VerifyRow(
        status=cast("str", row[0]),
        document_version=cast("int | None", row[1]),
        issued_at=cast("datetime | None", row[2]),
        content_digest=cast("str | None", row[3]),
        signed_digest=cast("str | None", row[4]),
        issuer_label=cast("str | None", row[5]),
        clinic_label=cast("str | None", row[6]),
    )


def verify_handle(*, handle: object, remote_addr: str | None) -> VerificationResult:
    """Resolve one QR handle to its minimal public verification result.

    Every probe consumes allowance before any lookup, so enumeration of
    unknown, malformed or unissued handles is bounded identically. The
    resolver verifies a completed signature inside its privileged
    boundary before ``issued``/``rehearsal_complete`` is reported; a
    disabled verifier reports ``unavailable``.
    """
    if not _allowance(_probe_key(remote_addr)):
        raise VerificationLimitedError
    if type(handle) is not str:
        return VerificationResult(status="unavailable")
    row = _verify_row(handle)
    return VerificationResult(
        status=cast("VerificationStatus", row.status),
        document_version=row.document_version,
        issued_at=row.issued_at,
        content_digest=row.content_digest,
        signed_digest=row.signed_digest,
        issuer_label=row.issuer_label,
        clinic_label=row.clinic_label,
    )


def _verification_event(
    record_id: UUID,
    clinic_id: UUID,
    action: str,
    *,
    record_type: str = "prescription.document",
) -> None:
    record_event(
        AuditEventInput(
            event_type=f"prescription.document.{action}",
            component_id="clinic-os-web",
            component_ip=None,
            affected_record_type=record_type,
            affected_record_id=str(record_id),
            occurred_at_utc=timezone.now(),
        ),
        payload={"clinic_id": str(clinic_id), "object_verb": action},
    )


def _issuer_document(*, clinic_id: UUID, document_id: UUID) -> PrescriptionDocument:
    """Resolve the document only for its assigned issuer."""
    try:
        actor = require_current_actor_clinic_roles(
            clinic_id, (UserClinicRole.Role.PHYSICIAN,)
        )
    except CurrentActorError:
        _denied(clinic_id, document_id, "role_denied")
    document = PrescriptionDocument.objects.filter(
        pk=document_id, clinic_id=clinic_id
    ).first()
    if document is None:
        raise ClinicalAccessDeniedError
    if document.issuer_id != actor:
        _denied(clinic_id, document.encounter_id, "not_issuer")
    return document


def _completed_operation(document: PrescriptionDocument) -> SignatureOperation:
    """Return the document's completed operation or conflict."""
    operation = (
        SignatureOperation.objects.filter(
            document=document, state__in=COMPLETED_SIGNATURE_STATES
        )
        .order_by("-completed_at")
        .first()
    )
    if operation is None:
        reason = "not_issued"
        raise ClinicalConflictError(reason)
    return operation


def release_document(
    *, clinic_id: UUID, document_id: UUID
) -> PrescriptionDocumentRelease:
    """Release one completed document to its patient; repeats return it.

    Only the assigned issuer may release, and only a document whose
    signature completed. The release is the sole authority for the
    patient download surface; the QR handle never is.
    """
    document = _issuer_document(clinic_id=clinic_id, document_id=document_id)
    _completed_operation(document)
    actor = current_actor_id()
    with transaction.atomic():
        existing = PrescriptionDocumentRelease.objects.filter(
            document=document, revoked_at__isnull=True
        ).first()
        if existing is not None:
            return existing
        try:
            with transaction.atomic():
                release = PrescriptionDocumentRelease.objects.create(
                    organization_id=document.organization_id,
                    document=document,
                    encounter_id=document.encounter_id,
                    clinic_id=clinic_id,
                    patient_id=document.patient_id,
                    issuer_id=document.issuer_id,
                    released_by_id=actor,
                )
        except IntegrityError:
            return PrescriptionDocumentRelease.objects.get(
                document=document, revoked_at__isnull=True
            )
        _verification_event(document.pk, clinic_id, "released")
        return release


def revoke_document_release(
    *, clinic_id: UUID, document_id: UUID
) -> PrescriptionDocumentRelease:
    """Revoke the active patient release; unknown ids are non-enumerating."""
    document = _issuer_document(clinic_id=clinic_id, document_id=document_id)
    with transaction.atomic():
        release = (
            PrescriptionDocumentRelease.objects.select_for_update()
            .filter(document=document, revoked_at__isnull=True)
            .first()
        )
        if release is None:
            record_denial(clinic_id, document.pk, "not_found")
            raise ClinicalAccessDeniedError
        release.revoked_by_id = current_actor_id()
        release.revoked_at = timezone.now()
        release.save(update_fields=("revoked_by", "revoked_at"))
        _verification_event(document.pk, clinic_id, "release_revoked")
        return release


def revoke_document(
    *, clinic_id: UUID, document_id: UUID, reason: str
) -> PrescriptionDocumentRevocation:
    """Record the terminal revocation of one completed document.

    Revocation never rewrites the signed bytes or the rendered artifact;
    it is an insert-only row that makes every later verification report
    ``revoked``. The delivery boundary holds the subject's mutation lock
    so a committed revocation can never be overtaken by an in-flight send.
    """
    document = _issuer_document(clinic_id=clinic_id, document_id=document_id)
    _completed_operation(document)
    if reason not in PrescriptionDocumentRevocation.Reason.values:
        raise ClinicalAccessDeniedError
    with transaction.atomic():
        hold_subject_mutation_key(f"{DELIVERY_SUBJECT_TYPE}:{document.pk}")
        existing = PrescriptionDocumentRevocation.objects.filter(
            document=document
        ).first()
        if existing is not None:
            return existing
        try:
            with transaction.atomic():
                revocation = PrescriptionDocumentRevocation.objects.create(
                    organization_id=document.organization_id,
                    document=document,
                    encounter_id=document.encounter_id,
                    clinic_id=clinic_id,
                    patient_id=document.patient_id,
                    issuer_id=document.issuer_id,
                    reason=reason,
                    revoked_by_id=current_actor_id(),
                )
        except IntegrityError:
            return PrescriptionDocumentRevocation.objects.get(document=document)
        _verification_event(document.pk, clinic_id, "revoked")
        return revocation


def _verified_email_contact(document: PrescriptionDocument) -> PatientContact | None:
    """Return the patient's currently verified email destination, if any."""
    return (
        PatientContact.objects.filter(
            organization_id=document.organization_id,
            patient_id=document.patient_id,
            channel=DELIVERY_CHANNEL,
        )
        .filter(verified_version__gte=1)
        .filter(verified_version=F("destination_version"))
        .first()
    )


def deliver_document(*, clinic_id: UUID, document_id: UUID) -> UUID:
    """Enqueue approved link-only delivery through the shared outbox.

    The operation carries only routing metadata: the document reference,
    the issuer's authority and the patient's verified email channel. The
    provider message contains the public verification URL and portal
    instructions — never the document bytes or an attachment. A verified
    email contact is required up front; the send-time recheck re-validates
    completion, release, revocation and destination before any send.
    """
    document = _issuer_document(clinic_id=clinic_id, document_id=document_id)
    _completed_operation(document)
    if PrescriptionDocumentRevocation.objects.filter(document=document).exists():
        reason = "revoked"
        raise ClinicalConflictError(reason)
    release = PrescriptionDocumentRelease.objects.filter(
        document=document, revoked_at__isnull=True
    ).first()
    if release is None:
        reason = "not_released"
        raise ClinicalConflictError(reason)
    if _verified_email_contact(document) is None:
        reason = "no_verified_contact"
        raise ClinicalConflictError(reason)
    operation_id = enqueue_operation(
        OperationRequest(
            channel=DELIVERY_CHANNEL,
            provider=DELIVERY_PROVIDER,
            clinic_id=clinic_id,
            subject_type=DELIVERY_SUBJECT_TYPE,
            subject_id=document.pk,
            idempotency_key=uuid5(
                DELIVERY_IDEMPOTENCY_NAMESPACE,
                f"document-delivery:{release.pk}",
            ),
            max_attempts=3,
        )
    )
    _verification_event(document.pk, clinic_id, "delivery_enqueued")
    return operation_id


def document_delivery_eligible(scope: OperationScope) -> bool:
    """Recheck completion, release, revocation and destination at send time."""
    operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
    if operation is None:
        return False
    document = PrescriptionDocument.objects.filter(pk=operation.subject_id).first()
    if document is None:
        return False
    return (
        document.organization_id == scope.organization_id
        and document.clinic_id == scope.clinic_id
        and SignatureOperation.objects.filter(
            document=document, state__in=COMPLETED_SIGNATURE_STATES
        ).exists()
        and not PrescriptionDocumentRevocation.objects.filter(
            document=document
        ).exists()
        and PrescriptionDocumentRelease.objects.filter(
            document=document, revoked_at__isnull=True
        ).exists()
        and _verified_email_contact(document) is not None
    )


def document_delivery_lock_key(_scope: OperationScope, subject_id: UUID) -> str:
    """Name the stable mutation boundary for one document's delivery."""
    return f"{DELIVERY_SUBJECT_TYPE}:{subject_id}"


def patient_documents() -> list[PatientDocument]:
    """List the session patient's released documents through the resolver."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.prescription_patient_documents(%s)",
            [secret_store().get_secret(KEK_SECRET_NAME)],
        )
        rows = cursor.fetchall()
    return [
        PatientDocument(
            document_id=row[0],
            document_version=row[1],
            state=row[2],
            issued_at=row[3],
            verification_url=row[4],
        )
        for row in rows
    ]


def _record_patient_download(document_id: UUID) -> None:
    """Append the fixed download event under the live records session."""
    event = AuditEventInput(
        event_type="prescription.document.downloaded",
        component_id="clinic-os-web",
        component_ip=None,
        affected_record_type="prescription.document",
        affected_record_id=str(document_id),
        occurred_at_utc=timezone.now(),
    )
    session_id, organization_id, clinic_id, _ = _patient_session_scope()
    payload = _normalize_payload(
        {"clinic_id": str(clinic_id), "object_verb": "downloaded"}
    )
    content_hash = _content_hash(
        event,
        AuditTrustedContext(organization_id=organization_id, actor_user_id=session_id),
        payload,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.prescription_document_viewed("
            "%s::uuid, %s::timestamptz, %s::bytea)",
            [str(document_id), event.occurred_at_utc, content_hash],
        )
        appended = cursor.fetchone()
    if appended is None:
        raise ClinicalAccessDeniedError


def _patient_session_scope() -> tuple[UUID, UUID, UUID, UUID]:
    """Return the live records session binding or deny the boundary."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.retention_records_session()")
        row = cursor.fetchone()
    if row is None:
        raise ClinicalAccessDeniedError
    return row[0], row[1], row[2], row[3]


def patient_document_download(*, document_id: UUID) -> PatientDocumentDownload:
    """Serve released signed bytes to the session patient only.

    The resolver re-validates the live session, the active release and the
    stored digest; every failure returns NULL and is indistinguishable.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.prescription_patient_document_bytes(%s, %s)",
            [secret_store().get_secret(KEK_SECRET_NAME), str(document_id)],
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        raise ClinicalAccessDeniedError
    _record_patient_download(document_id)
    return PatientDocumentDownload(
        data=bytes(row[0]),
        content_type="application/pdf",
        file_name=f"prescricao-{document_id}.pdf",
    )
