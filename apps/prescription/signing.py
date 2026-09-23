"""Explicit signature lifecycle: draft -> prepared -> signing -> issued|failed.

Every transition is a guarded state change on ``SignatureOperation``:

- ``initiate_signature`` binds the operation to the immutable rendered
  document, its exact byte digest and the issuer's provisioned signing
  subject. No provider call and no signature exist yet.
- ``prepare_signature`` runs task-31's fresh step-up plus current physician
  evidence check and snapshots the validated evidence onto the operation.
- ``dispatch_signature`` runs after commit on stored scope only: it calls
  the provider outside any transaction, then binds the returned provider
  operation reference while moving to ``signing``.
- ``receive_signature_callback`` authenticates the raw provider callback
  before resolving the stored operation, binds it to the stored
  operation/issuer/content, independently verifies the signed bytes and
  records issuance time from the validated signing evidence — never from
  the callback arrival or the database clock.

Signed output is immutable once issued; the original rendered bytes are
preserved verbatim inside the signed envelope and on the unchanged
``PrescriptionDocument`` row, so later draft amendments never rewrite them.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

from django.db import IntegrityError, connection, connections, transaction
from django.utils import timezone
from django.utils.timezone import now as utc_now

from apps.audit.canonical import AuditEventInput
from apps.audit.services import record_event
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    _denied,
)
from apps.identity.models import PhysicianEvidence, PhysicianProfile
from apps.identity.physician_registry import SigningIdentity
from apps.identity.physician_verification import (
    PhysicianVerificationRequired,
    verify_physician_for_signing,
)
from apps.identity.stepup import (
    DEFAULT_STEP_UP_MAX_AGE_SECONDS,
    STEP_UP_SESSION_KEY,
    StepUpRequired,
)
from apps.prescription.models import (
    LIVE_SIGNATURE_STATES,
    PrescriptionDocument,
    SignatureCallback,
    SignatureOperation,
)
from apps.prescription.services import (
    DocumentStorageError,
    _authorize_document_care,
    authorize_encounter,
)
from apps.prescription.signature_provider import (
    MAX_REFERENCE_LENGTH,
    SYNTHETIC_PROVIDER,
    SignatureAcceptance,
    SignatureCallbackError,
    SignatureCallbackFacts,
    SignatureProvider,
    SignatureProviderRejectedError,
    SignatureProviderTransientError,
    SignatureProviderUnavailableError,
    SignatureRequest,
    SignatureVerificationError,
    get_signature_provider,
    signature_capability,
    signature_provider_for,
)
from apps.tenancy.db import clear_connection_tenant_gucs

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from uuid import UUID

    from django.http import HttpRequest

logger = logging.getLogger(__name__)

type SignatureCallbackResult = Literal["applied", "duplicate", "rejected"]
type DispatchResult = Literal["signing", "skipped", "reconcile", "retry", "failed"]

_CLAIM_LOCK_NAMESPACE = "clinic-lock-v1:prescription-signature:"
_LIVE_STATES = LIVE_SIGNATURE_STATES


class SignatureUnavailableError(Exception):
    """Report that no approved signing capability is usable here."""


@dataclass(frozen=True, slots=True)
class _SignatureScope:
    """Stored tenant scope resolved for one operation or callback."""

    operation_id: UUID
    organization_id: UUID
    clinic_id: UUID
    actor_id: UUID
    provider: str


def _signature_event(
    operation_id: UUID,
    clinic_id: UUID,
    action: str,
    *,
    reason_code: str | None = None,
) -> None:
    payload: dict[str, str] = {
        "clinic_id": str(clinic_id),
        "object_verb": action,
    }
    if reason_code is not None:
        payload["reason_code"] = reason_code
    record_event(
        AuditEventInput(
            event_type=f"prescription.signature.{action}",
            component_id="clinic-os-web",
            component_ip=None,
            affected_record_type="prescription.signature",
            affected_record_id=str(operation_id),
            occurred_at_utc=timezone.now(),
        ),
        payload=payload,
    )


def _document_for_signing(clinic_id: UUID, document_id: UUID) -> PrescriptionDocument:
    document = PrescriptionDocument.objects.filter(
        pk=document_id, clinic_id=clinic_id
    ).first()
    if document is None:
        raise ClinicalAccessDeniedError
    return document


def initiate_signature(
    *,
    clinic_id: UUID,
    document_id: UUID,
) -> SignatureOperation:
    """Create the draft operation bound to exact document bytes and issuer.

    The document's stored digest is recomputed from its bytes before the
    operation exists, so a corrupted artifact can never enter signing. The
    signer subject comes from the owner-provisioned profile, never from
    submitted fields.
    """
    document = _document_for_signing(clinic_id, document_id)
    encounter = authorize_encounter(
        clinic_id=clinic_id, encounter_id=document.encounter_id
    )
    actor = encounter.physician_id
    if document.issuer_id != actor:
        _denied(clinic_id, document.encounter_id, "not_issuer")
    if not signature_capability().synthetic_enabled:
        raise SignatureUnavailableError
    if sha256(bytes(document.pdf_bytes)).hexdigest() != document.pdf_digest:
        raise DocumentStorageError
    profile = PhysicianProfile.objects.filter(
        organization_id=document.organization_id,
        user_id=actor,
        jurisdiction=document.clinic.crm_uf,
    ).first()
    if profile is None:
        reason = "registration_profile_required"
        raise PhysicianVerificationRequired(reason)
    existing = SignatureOperation.objects.filter(
        document=document, state__in=_LIVE_STATES
    ).first()
    if existing is not None:
        superseded = SignatureOperation.objects.filter(
            pk=existing.pk, state__in=_LIVE_STATES
        ).update(
            state=SignatureOperation.State.FAILED,
            failure_reason="superseded",
            completed_at=utc_now(),
        )
        if superseded == 1:
            _signature_event(existing.pk, clinic_id, "failed", reason_code="superseded")
    completed = (
        SignatureOperation.objects.filter(
            document=document,
            state__in=(
                SignatureOperation.State.ISSUED,
                SignatureOperation.State.REHEARSAL_COMPLETE,
            ),
        )
        .values_list("state", flat=True)
        .first()
    )
    if completed == SignatureOperation.State.ISSUED:
        reason = "already_issued"
        raise ClinicalConflictError(reason)
    if completed is not None:
        reason = "already_completed"
        raise ClinicalConflictError(reason)
    try:
        operation = SignatureOperation.objects.create(
            organization_id=document.organization_id,
            document=document,
            encounter_id=document.encounter_id,
            clinic_id=clinic_id,
            patient_id=document.patient_id,
            issuer_id=actor,
            provider=get_signature_provider().provider,
            state=SignatureOperation.State.DRAFT,
            content_digest=document.pdf_digest,
            signer_subject=profile.signing_subject,
        )
    except IntegrityError as error:
        reason = "signing_in_progress"
        raise ClinicalConflictError(reason) from error
    _signature_event(operation.pk, clinic_id, "initiated")
    return operation


def _evidence_snapshot(
    operation: SignatureOperation,
    evidence_id: UUID,
) -> dict[str, object]:
    """Freeze the validated evidence and byte binding onto the operation."""
    evidence = PhysicianEvidence.objects.filter(pk=evidence_id).first()
    if evidence is None:
        reason = "evidence_unavailable"
        raise PhysicianVerificationRequired(reason)
    return {
        "evidence_id": str(evidence.pk),
        "provider": evidence.provider,
        "reason_code": evidence.reason_code,
        "status": evidence.status,
        "reference": evidence.reference,
        "synthetic": evidence.synthetic,
        "jurisdiction": evidence.jurisdiction,
        "registration_number": evidence.registration_number,
        "signing_subject": evidence.signing_subject,
        "checked_at": (
            evidence.checked_at.isoformat() if evidence.checked_at else None
        ),
        "expires_at": (
            evidence.expires_at.isoformat() if evidence.expires_at else None
        ),
        "recheck_at": (
            evidence.recheck_at.isoformat() if evidence.recheck_at else None
        ),
        "content_digest": operation.content_digest,
        "document_version": operation.document.document_version,
    }


def _authorization_deadline(
    request: HttpRequest,
    evidence: PhysicianEvidence,
) -> datetime:
    """Freeze when this authorization stops being usable for issuance.

    The deadline is the earliest of the evidence's recheck/expiry bounds
    and the step-up verification's remaining validity. Issuance past it
    fails closed, so a delayed callback or a stale retry can never turn
    expired authority into a completed signature.
    """
    verified_at = request.session.get(STEP_UP_SESSION_KEY)
    if isinstance(verified_at, bool) or not isinstance(verified_at, int):
        raise StepUpRequired
    deadline = datetime.fromtimestamp(verified_at, UTC) + timedelta(
        seconds=DEFAULT_STEP_UP_MAX_AGE_SECONDS
    )
    if evidence.recheck_at is not None and evidence.recheck_at < deadline:
        deadline = evidence.recheck_at
    if evidence.expires_at is not None and evidence.expires_at < deadline:
        deadline = evidence.expires_at
    return deadline


def _prepare_signature(
    *,
    request: HttpRequest,
    clinic_id: UUID,
    operation_id: UUID,
) -> SignatureOperation:
    """Require fresh step-up and current physician evidence before signing.

    The registry is queried on every attempt; the returned evidence is
    snapshotted onto the operation in the same transition that moves it to
    ``prepared``, together with the authorization deadline that issuance
    enforces. A failed check leaves the operation in ``draft`` and the
    retained failed evidence row.
    """
    operation = (
        SignatureOperation.objects.select_for_update()
        .filter(pk=operation_id, clinic_id=clinic_id)
        .first()
    )
    if operation is None:
        raise ClinicalAccessDeniedError
    if operation.state != SignatureOperation.State.DRAFT:
        reason = "invalid_state"
        raise ClinicalConflictError(reason)
    signer = SigningIdentity(
        issuer_id=operation.issuer_id,
        subject=operation.signer_subject,
        synthetic=True,
    )
    evidence = verify_physician_for_signing(
        request=request,
        clinic_id=clinic_id,
        encounter_id=operation.encounter_id,
        signer=signer,
        synthetic=True,
    )
    authorized_until = _authorization_deadline(request, evidence)
    snapshot = _evidence_snapshot(operation, evidence.pk)
    snapshot["authorized_until"] = authorized_until.isoformat()
    updated = SignatureOperation.objects.filter(
        pk=operation.pk, state=SignatureOperation.State.DRAFT
    ).update(
        state=SignatureOperation.State.PREPARED,
        evidence_id=evidence.pk,
        evidence_snapshot=snapshot,
        authorized_until=authorized_until,
    )
    if updated != 1:
        reason = "invalid_state"
        raise ClinicalConflictError(reason)
    _signature_event(operation.pk, clinic_id, "prepared")
    operation.refresh_from_db()
    return operation


def request_signature(
    *,
    request: HttpRequest,
    clinic_id: UUID,
    document_id: UUID,
) -> SignatureOperation:
    """Initiate, verify and prepare one operation; dispatch after commit.

    The provider call itself never runs inside the tenant transaction:
    the committed operation is dispatched through ``dispatch_signature``,
    which re-enters stored scope like the shared integration boundary.
    """
    operation = initiate_signature(clinic_id=clinic_id, document_id=document_id)
    operation = _prepare_signature(
        request=request, clinic_id=clinic_id, operation_id=operation.pk
    )
    transaction.on_commit(lambda: _dispatch_safely(operation.pk))
    return operation


def _dispatch_safely(operation_id: UUID) -> None:
    """Dispatch one committed operation; failures stay recoverable."""
    try:
        dispatch_signature(operation_id)
    except Exception:
        logger.exception("prescription signature dispatch failed")


def _resolve_operation_scope(operation_id: UUID) -> _SignatureScope | None:
    """Resolve stored tenant scope for one operation through the resolver."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT operation_scope.organization_id, "
            "operation_scope.clinic_id, operation_scope.actor_id, "
            "operation_scope.provider "
            "FROM clinic_app.prescription_signature_scope(%s) AS operation_scope",
            [str(operation_id)],
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return _SignatureScope(
        operation_id=operation_id,
        organization_id=row[0],
        clinic_id=row[1],
        actor_id=row[2],
        provider=row[3],
    )


def _resolve_callback_scope(
    provider: str,
    operation_ref: str,
) -> _SignatureScope | None:
    """Resolve stored scope for one authenticated callback reference.

    Correlation is the stored ``(provider, operation_id)`` pair only; a
    reference that resolves to anything but exactly one operation is
    rejected rather than silently attributed to a tenant.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT callback_scope.operation_id, "
            "callback_scope.organization_id, callback_scope.clinic_id, "
            "callback_scope.actor_id, callback_scope.provider "
            "FROM clinic_app.prescription_signature_callback_scope(%s, %s) "
            "AS callback_scope",
            [provider, operation_ref],
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    return _SignatureScope(
        operation_id=row[0],
        organization_id=row[1],
        clinic_id=row[2],
        actor_id=row[3],
        provider=row[4],
    )


@contextmanager
def _stored_scope_context(scope: _SignatureScope) -> Iterator[None]:
    """Attribute bookkeeping to stored scope in one outermost transaction.

    Used only after an external authorization decision (provider callback
    authentication or a failed claim). It never reads tenant claims from
    external input and never performs domain reads.
    """
    if connection.in_atomic_block:
        raise SignatureUnavailableError
    try:
        with transaction.atomic(durable=True), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true), "
                "pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(scope.actor_id), str(scope.organization_id)],
            )
            yield
    finally:
        clear_connection_tenant_gucs()


@contextmanager
def _operation_lock(operation_id: UUID) -> Iterator[bool]:
    """Hold the operation's session advisory lock on a dedicated connection.

    The lock lives on its own connection so it survives the claim
    transaction's commit and is held for the whole dispatch boundary. A
    duplicate dispatch that cannot take the lock reconciles without
    mutating, so it can never double-submit a live provider operation.
    """
    lock_connection = connections.create_connection("default")
    try:
        with lock_connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.pg_try_advisory_lock("
                "pg_catalog.hashtextextended(%s, 0))",
                [f"{_CLAIM_LOCK_NAMESPACE}{operation_id}"],
            )
            row = cursor.fetchone()
        yield row is not None and bool(row[0])
    finally:
        lock_connection.close()


def _fail_operation(scope: _SignatureScope, *, reason_code: str) -> bool:
    """Fail one live operation once and audit the terminal transition."""
    with _stored_scope_context(scope):
        updated = SignatureOperation.objects.filter(
            pk=scope.operation_id,
            state__in=_LIVE_STATES,
        ).update(
            state=SignatureOperation.State.FAILED,
            failure_reason=reason_code,
            completed_at=utc_now(),
        )
        if updated == 1:
            _signature_event(
                scope.operation_id, scope.clinic_id, "failed", reason_code=reason_code
            )
            return True
    return False


def _dispatch_request(scope: _SignatureScope) -> SignatureRequest | None:
    """Load the exact stored signing input inside the stored scope."""
    with _stored_scope_context(scope):
        operation = (
            SignatureOperation.objects.select_for_update()
            .filter(pk=scope.operation_id)
            .first()
        )
        if operation is None or operation.state != SignatureOperation.State.PREPARED:
            return None
        document = operation.document
        content = bytes(document.pdf_bytes)
        if sha256(content).hexdigest() != operation.content_digest:
            raise DocumentStorageError
        return SignatureRequest(
            operation_id=operation.pk,
            issuer_id=operation.issuer_id,
            signer_subject=operation.signer_subject,
            content_digest=operation.content_digest,
            content=content,
        )


def _mark_signing(scope: _SignatureScope, operation_ref: str) -> bool:
    """Bind the provider reference while moving the operation to signing."""
    with _stored_scope_context(scope):
        updated = SignatureOperation.objects.filter(
            pk=scope.operation_id,
            state=SignatureOperation.State.PREPARED,
        ).update(
            state=SignatureOperation.State.SIGNING,
            operation_id=operation_ref,
        )
        if updated == 1:
            _signature_event(scope.operation_id, scope.clinic_id, "signing")
            return True
    return False


def _persisted_dispatch(scope: _SignatureScope) -> DispatchResult:
    """Report the stored state after a guarded dispatch touched no row."""
    with _stored_scope_context(scope):
        state = (
            SignatureOperation.objects.filter(pk=scope.operation_id)
            .values_list("state", flat=True)
            .first()
        )
    if state == SignatureOperation.State.FAILED:
        return "failed"
    if state in (
        SignatureOperation.State.SIGNING,
        SignatureOperation.State.ISSUED,
    ):
        return "signing"
    if state is None:
        return "skipped"
    return "reconcile"


def _fail_and_report(scope: _SignatureScope, reason_code: str) -> DispatchResult:
    """Fail one operation and report the terminal dispatch outcome."""
    _fail_operation(scope, reason_code=reason_code)
    return "failed"


def _provider_begin(
    provider: SignatureProvider,
    request: SignatureRequest,
    scope: _SignatureScope,
) -> SignatureAcceptance | DispatchResult:
    """Call the provider outside any transaction; map failures to outcomes."""
    try:
        return provider.begin(request)
    except SignatureProviderTransientError:
        return "retry"
    except SignatureProviderRejectedError:
        return _fail_and_report(scope, "provider_rejected")
    except SignatureProviderUnavailableError:
        return _fail_and_report(scope, "provider_unavailable")


def _bind_dispatch(scope: _SignatureScope, operation_ref: object) -> DispatchResult:
    """Persist a well-formed provider reference or fail the operation."""
    if (
        type(operation_ref) is not str
        or not 1 <= len(operation_ref) <= MAX_REFERENCE_LENGTH
    ):
        return _fail_and_report(scope, "invalid_provider_reference")
    if _mark_signing(scope, operation_ref):
        return "signing"
    return _persisted_dispatch(scope)


def _dispatch_claimed(scope: _SignatureScope) -> DispatchResult:
    """Dispatch one operation while its claim lock is held."""
    try:
        request = _dispatch_request(scope)
    except DocumentStorageError:
        return _fail_and_report(scope, "content_digest_mismatch")
    if request is None:
        return _persisted_dispatch(scope)
    try:
        provider = signature_provider_for(scope.provider)
    except SignatureProviderUnavailableError:
        return _fail_and_report(scope, "provider_unavailable")
    acceptance = _provider_begin(provider, request, scope)
    if isinstance(acceptance, str):
        return acceptance
    return _bind_dispatch(scope, acceptance.operation_ref)


def dispatch_signature(operation_id: UUID) -> DispatchResult:
    """Dispatch one prepared operation through the trusted boundary.

    The provider ``begin`` call runs with no open transaction; only the
    returned operation reference is persisted, bound to the stored
    operation. A transient provider failure leaves the operation prepared
    for retry; a rejection or missing provider fails it closed. A halted
    live activation raises ``LiveModeHaltedError`` before any stored
    operation is resolved, so rollback stops new signing jobs.
    """
    from ops.release.activation import require_live_runtime  # noqa: PLC0415

    require_live_runtime(os.environ)
    scope = _resolve_operation_scope(operation_id)
    if scope is None:
        return "skipped"
    with _operation_lock(operation_id) as acquired:
        if not acquired:
            return "reconcile"
        return _dispatch_claimed(scope)


def receive_signature_callback(
    *,
    provider: str,
    headers: Mapping[str, str],
    body: bytes,
) -> SignatureCallbackResult:
    """Apply one authenticated provider callback to its stored operation.

    Authentication runs before any stored operation or tenant is resolved;
    only the verified event id, operation reference, status and signed
    bytes are used. A ``signed`` report is never trusted: the signed bytes
    are independently verified against the stored content digest, signer
    and operation reference before the operation can reach ``issued``.
    """
    try:
        authenticator = signature_provider_for(provider)
    except SignatureProviderUnavailableError as error:
        raise SignatureCallbackError from error
    facts = authenticator.authenticate_callback(headers=headers, body=body)
    scope = _resolve_callback_scope(provider, facts.operation_ref)
    if scope is None:
        return "rejected"
    with _stored_scope_context(scope):
        operation = (
            SignatureOperation.objects.select_for_update()
            .filter(pk=scope.operation_id)
            .first()
        )
        if operation is None:
            return "rejected"
        if SignatureCallback.objects.filter(
            operation=operation, event_id=facts.event_id
        ).exists():
            return "duplicate"
        if operation.state != SignatureOperation.State.SIGNING:
            return "rejected"
        verified_at = utc_now()
        SignatureCallback.objects.create(
            organization_id=operation.organization_id,
            operation=operation,
            event_id=facts.event_id,
            payload=facts.payload,
            verified_at=verified_at,
        )
        if facts.status == "signed":
            _apply_signed(authenticator, operation, facts, verified_at)
            return "applied"
        operation.state = SignatureOperation.State.FAILED
        operation.failure_reason = "provider_reported"
        operation.completed_at = verified_at
        operation.save(update_fields=("state", "failure_reason", "completed_at"))
        _signature_event(
            operation.pk,
            operation.clinic_id,
            "failed",
            reason_code="provider_reported",
        )
        return "applied"


def _apply_signed(
    authenticator: SignatureProvider,
    operation: SignatureOperation,
    facts: SignatureCallbackFacts,
    verified_at: datetime,
) -> None:
    """Issue or fail one operation from an authenticated signed report.

    Authorization freshness is enforced here, at issuance, not only at
    preparation: a signed report arriving after ``authorized_until`` fails
    the operation closed and requires a fresh attempt with renewed step-up
    and current evidence. A verified synthetic result reaches only
    ``rehearsal_complete``; ``issued`` is reserved for an approved real
    provider, which no enabled capability currently supplies.
    """
    if operation.authorized_until is None or verified_at >= operation.authorized_until:
        operation.state = SignatureOperation.State.FAILED
        operation.failure_reason = "authorization_expired"
        operation.completed_at = verified_at
        operation.save(update_fields=("state", "failure_reason", "completed_at"))
        _signature_event(
            operation.pk,
            operation.clinic_id,
            "failed",
            reason_code="authorization_expired",
        )
        return
    try:
        verified = authenticator.verify_signature(
            signed_bytes=facts.signed_bytes,
            content_digest=operation.content_digest,
            signer_subject=operation.signer_subject,
            operation_ref=operation.operation_id,
            not_before=operation.created_at,
        )
    except SignatureVerificationError:
        operation.state = SignatureOperation.State.FAILED
        operation.failure_reason = "signature_invalid"
        operation.completed_at = verified_at
        operation.save(update_fields=("state", "failure_reason", "completed_at"))
        _signature_event(
            operation.pk,
            operation.clinic_id,
            "failed",
            reason_code="signature_invalid",
        )
        return
    if authenticator.provider == SYNTHETIC_PROVIDER:
        operation.state = SignatureOperation.State.REHEARSAL_COMPLETE
        event = "rehearsal_completed"
    else:
        operation.state = SignatureOperation.State.ISSUED
        event = "issued"
    operation.signed_bytes = facts.signed_bytes
    operation.signed_digest = sha256(facts.signed_bytes).hexdigest()
    operation.completed_at = verified.signed_at
    operation.save(
        update_fields=(
            "state",
            "signed_bytes",
            "signed_digest",
            "completed_at",
        )
    )
    _signature_event(operation.pk, operation.clinic_id, event)


def signature_status(
    *,
    clinic_id: UUID,
    operation_id: UUID,
) -> SignatureOperation:
    """Return one operation only to its issuer under current assignment."""
    operation = (
        SignatureOperation.objects.filter(pk=operation_id, clinic_id=clinic_id)
        .select_related("document")
        .first()
    )
    if operation is None:
        raise ClinicalAccessDeniedError
    encounter = authorize_encounter(
        clinic_id=clinic_id, encounter_id=operation.encounter_id
    )
    if operation.issuer_id != encounter.physician_id:
        _denied(clinic_id, operation.encounter_id, "not_issuer")
    return operation


def retry_dispatch(
    *,
    clinic_id: UUID,
    operation_id: UUID,
) -> SignatureOperation:
    """Re-dispatch one prepared operation after commit; never duplicates."""
    operation = signature_status(clinic_id=clinic_id, operation_id=operation_id)
    if operation.state != SignatureOperation.State.PREPARED:
        reason = "invalid_state"
        raise ClinicalConflictError(reason)
    transaction.on_commit(lambda: _dispatch_safely(operation.pk))
    _signature_event(operation.pk, clinic_id, "dispatch_retried")
    return operation


def abandon_signature(
    *,
    clinic_id: UUID,
    operation_id: UUID,
) -> SignatureOperation:
    """Abandon one live operation so a fresh attempt can be initiated."""
    operation = signature_status(clinic_id=clinic_id, operation_id=operation_id)
    if operation.state not in _LIVE_STATES:
        reason = "invalid_state"
        raise ClinicalConflictError(reason)
    updated = SignatureOperation.objects.filter(
        pk=operation.pk, state=operation.state
    ).update(
        state=SignatureOperation.State.FAILED,
        failure_reason="abandoned",
        completed_at=utc_now(),
    )
    if updated != 1:
        reason = "invalid_state"
        raise ClinicalConflictError(reason)
    _signature_event(operation.pk, clinic_id, "failed", reason_code="abandoned")
    operation.refresh_from_db()
    return operation


@dataclass(frozen=True, kw_only=True)
class SignedDownload:
    """Fully materialized, authorized signed bytes for a bounded response."""

    data: bytes
    content_type: str
    file_name: str


def download_signed_document(
    *,
    clinic_id: UUID,
    operation_id: UUID,
) -> SignedDownload:
    """Serve the immutable signed output only after verified issuance.

    Authorization mirrors the unsigned artifact path: role plus care
    relationship on the encounter. Stored signed bytes are re-checked
    against the recorded digest before they leave the tenant boundary.
    """
    operation = SignatureOperation.objects.filter(
        pk=operation_id, clinic_id=clinic_id
    ).first()
    if operation is None or operation.state not in (
        SignatureOperation.State.ISSUED,
        SignatureOperation.State.REHEARSAL_COMPLETE,
    ):
        raise ClinicalAccessDeniedError
    _authorize_document_care(clinic_id, operation.encounter_id)
    data = bytes(operation.signed_bytes or b"")
    if not data or sha256(data).hexdigest() != operation.signed_digest:
        raise DocumentStorageError
    _signature_event(operation.pk, clinic_id, "downloaded")
    return SignedDownload(
        data=data,
        content_type="application/pdf",
        file_name=f"prescricao-assinada-{operation.pk}.pdf",
    )
