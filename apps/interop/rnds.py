"""RNDS laboratory-result submission: eligibility, sealed bundle, outbox send.

``exchange_clinical_record`` runs inside the caller's staff tenant transaction.
It refuses before any read unless the fixture rehearsal is open (EG-7 is unmet),
validates the typed exam document, then requires ``result.release`` for the
result's patient, the current acknowledged version, the patient's unrevoked
``rnds_exchange`` consent and a valid CNS or CPF. It builds and validates the
REL bundle, stores it sealed through ``interop_rnds_request`` (which re-checks
all of it) and enqueues one ``action`` operation that sends it after commit.

At send time the outbox rechecks the actor; ``submission_eligible`` rechecks
consent, the capability version and the result, so a revocation before the send
cancels it. The answer settles the submission while its operation is in flight.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast
from zoneinfo import ZoneInfo

import rfc8785
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.comms.adapters import (
    OperationScope,
    PermanentSendError,
    SendResult,
    TransientSendError,
)
from apps.comms.models import IntegrationOperation
from apps.consent.services import consent_for_future_use
from apps.core.integration import ActionOperationRequest, enqueue_operation
from apps.identity.current_context import CurrentActorError
from apps.identity.models import Clinic
from apps.intake.models import PatientClinicEnrollment, PatientIdentifier
from apps.interop.errors import (
    ALREADY_SUBMITTED,
    CONSENT_REQUIRED,
    IDENTIFIER_REQUIRED,
    NOT_ACKNOWLEDGED,
    RNDS_INELIGIBLE_REASONS,
    SOURCE_SUPERSEDED,
    InteropAccessDeniedError,
    InteropConflictError,
    InteropInputError,
    RndsIneligibleError,
    RndsUnavailableError,
    RndsValidationError,
)
from apps.interop.models import RndsSubmission
from apps.interop.rnds_bundle import (
    JsonValue,
    LabExam,
    build_bundle,
    cns_valid,
    cpf_valid,
    parse_exam,
    validate_bundle,
)
from apps.interop.rnds_gate import require_rnds_homologation
from apps.interop.rnds_transport import (
    HTTP_CREATED,
    HTTP_UNAUTHORIZED,
    TOKEN_EXPIRED,
    RndsTransportError,
    establishment_for,
    transport_for,
)
from apps.orders.access import require_version
from apps.orders.models import ResultAcknowledgment
from apps.orders.values import OrdersAccessDeniedError, OrdersConflictError
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from apps.tenancy.envelope import protect

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from apps.interop.rnds_transport import RndsAnswer
    from apps.providers.models import CapabilityVersion

__all__ = (
    "PROVIDER",
    "SUBJECT_TYPE",
    "RndsActionAdapter",
    "RndsReceipt",
    "exchange_clinical_record",
    "submission_eligible",
)

PROVIDER: Final = "interop-rnds-v1"
SUBJECT_TYPE: Final = "interop.rnds_submission"
CONSENT_PURPOSE: Final = "rnds_exchange"
BUNDLE_PURPOSE: Final = "interop.rndssubmission.bundle"
DUPLICATE: Final = "EHR-ERR866"
_REQUEST_DOMAIN: Final = "clinic-rnds-request-v1"
_OPERATION_NAMESPACE: Final = uuid.UUID("2b7f0c6e-3a51-4d8e-9f62-0c4b1e7a5d93")
_IDENTIFIER_KINDS: Final = (("cns", cns_valid), ("cpf", cpf_valid))
_ANSWER_CODE_LIMIT: Final = 32
_HTTP_CLIENT_ERROR: Final = 400


@dataclass(frozen=True, slots=True)
class RndsReceipt:
    """The submission a command created or, on replay, already holds."""

    submission_id: UUID
    state: str
    created: bool


def _request_digest(result_version_id: UUID, exam: LabExam) -> str:
    material: dict[str, JsonValue] = {
        "domain": _REQUEST_DOMAIN,
        "result_version": str(result_version_id),
        "exam": exam.canonical(),
    }
    return hashlib.sha256(rfc8785.dumps(material)).hexdigest()


def _patient_identifier(clinic_id: UUID, enrollment_id: UUID) -> str | None:
    """Return the patient's current valid CNS, else CPF; never a retired one."""
    enrollment = (
        PatientClinicEnrollment.objects.filter(pk=enrollment_id, clinic_id=clinic_id)
        .values_list("organization_id", "patient_id")
        .first()
    )
    if enrollment is None:
        return None
    organization_id, patient_id = enrollment
    for kind, valid in _IDENTIFIER_KINDS:
        current = (
            PatientIdentifier.objects.filter(
                organization_id=organization_id, patient_id=patient_id, kind=kind
            )
            .order_by("-version")
            .first()
        )
        if current is not None and current.retired_at is None:
            value = str(current.value)
            if valid(value):
                return value
    return None


def _consented(clinic_id: UUID, enrollment_id: UUID) -> bool:
    try:
        acceptance = consent_for_future_use(
            clinic_id=clinic_id, enrollment_id=enrollment_id, purpose=CONSENT_PURPOSE
        )
    except CurrentActorError:
        return False
    return acceptance is not None


def _eligible_enrollment(clinic_id: UUID, result_version_id: UUID) -> UUID:
    """Require release authority, the current version and its acknowledgment."""
    try:
        matched = require_version(
            clinic_id=clinic_id,
            version_id=result_version_id,
            permission="result.release",
        )
    except OrdersAccessDeniedError as error:
        raise InteropAccessDeniedError from error
    except OrdersConflictError as error:
        raise RndsIneligibleError(SOURCE_SUPERSEDED) from error
    if not ResultAcknowledgment.objects.filter(
        version_id=result_version_id, clinic_id=clinic_id
    ).exists():
        raise RndsIneligibleError(NOT_ACKNOWLEDGED)
    return matched.order.enrollment_id


def _replay(
    clinic_id: UUID, command_id: UUID, result_version_id: UUID, digest: str
) -> RndsReceipt | None:
    """Return the command's existing receipt, or refuse reuse for other terms."""
    existing = RndsSubmission.objects.filter(
        clinic_id=clinic_id, command_id=command_id
    ).first()
    if existing is None:
        return None
    if existing.request_digest != digest or existing.result_version_id != (
        result_version_id
    ):
        raise InteropConflictError
    return RndsReceipt(existing.pk, existing.state, created=False)


def _shareable_patient(
    clinic_id: UUID, result_version_id: UUID, enrollment_id: UUID
) -> str:
    """Require consent, a valid patient identifier and no live submission."""
    if not _consented(clinic_id, enrollment_id):
        raise RndsIneligibleError(CONSENT_REQUIRED)
    patient = _patient_identifier(clinic_id, enrollment_id)
    if patient is None:
        raise RndsIneligibleError(IDENTIFIER_REQUIRED)
    if RndsSubmission.objects.filter(
        result_version_id=result_version_id, state__in=("pending", "accepted")
    ).exists():
        raise RndsIneligibleError(ALREADY_SUBMITTED)
    return patient


@dataclass(frozen=True, slots=True)
class _SealedRequest:
    """One sealed bundle and the exact terms the request resolver binds."""

    clinic_id: UUID
    submission_id: UUID
    result_version_id: UUID
    command_id: UUID
    request_digest: str
    capability_version_id: UUID
    sealed: bytes = field(repr=False)
    bundle_digest: str


def _seal(  # noqa: PLR0913 - one value per bound request term
    *,
    clinic_id: UUID,
    result_version_id: UUID,
    command_id: UUID,
    digest: str,
    capability: CapabilityVersion,
    exam: LabExam,
    patient: str,
) -> _SealedRequest:
    """Build, validate and seal the bundle under a fresh submission id."""
    submission_id = uuid.uuid4()
    zone = Clinic.objects.filter(pk=clinic_id).values_list("timezone", flat=True).get()
    bundle = build_bundle(
        submission_id=submission_id,
        composed_at=timezone.now().astimezone(ZoneInfo(str(zone))),
        exam=exam,
        patient=patient,
        establishment=establishment_for(capability.provider),
    )
    issues = validate_bundle(bundle)
    if issues:
        raise RndsValidationError(issues)
    body = rfc8785.dumps(bundle)
    return _SealedRequest(
        clinic_id=clinic_id,
        submission_id=submission_id,
        result_version_id=result_version_id,
        command_id=command_id,
        request_digest=digest,
        capability_version_id=capability.pk,
        sealed=protect(purpose=BUNDLE_PURPOSE, plaintext=body),
        bundle_digest=hashlib.sha256(body).hexdigest(),
    )


def _store(request: _SealedRequest) -> RndsReceipt:
    """Insert through the resolver, which re-checks every rule in the database."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT record_id,outcome FROM clinic_app.interop_rnds_request("
                "%s,%s,%s,%s,%s,%s,%s,%s)",
                [
                    request.clinic_id,
                    request.submission_id,
                    request.result_version_id,
                    request.command_id,
                    request.request_digest,
                    request.capability_version_id,
                    request.sealed,
                    request.bundle_digest,
                ],
            )
            row = cursor.fetchone()
    except DatabaseError as error:
        raise InteropAccessDeniedError from error
    record_id, outcome = (None, "") if row is None else (row[0], str(row[1]))
    if outcome == "replayed" and record_id is not None:
        replayed = RndsSubmission.objects.get(pk=record_id)
        return RndsReceipt(replayed.pk, replayed.state, created=False)
    if outcome == "unavailable":
        raise RndsUnavailableError
    if outcome == "conflict":
        raise InteropConflictError
    if outcome in RNDS_INELIGIBLE_REASONS:
        raise RndsIneligibleError(outcome)
    if outcome != "created" or record_id is None:
        raise InteropAccessDeniedError
    return RndsReceipt(cast("UUID", record_id), "pending", created=True)


def exchange_clinical_record(
    *,
    clinic_id: UUID,
    result_version_id: UUID,
    exam: Mapping[str, object],
    command_id: UUID,
) -> RndsReceipt:
    """Submit one acknowledged laboratory result to RNDS homologation.

    Refusals: ``RndsUnavailableError`` (no open leg), ``RndsValidationError``
    (every failing exam or bundle element), ``InteropAccessDeniedError``
    (unknown, foreign or unauthorized record, all alike),
    ``RndsIneligibleError`` (a visible record that cannot be shared now) and
    ``InteropConflictError`` (the command was used for different terms).
    """
    if (
        type(clinic_id) is not uuid.UUID
        or type(result_version_id) is not uuid.UUID
        or type(command_id) is not uuid.UUID
    ):
        raise InteropInputError
    capability = require_rnds_homologation(clinic_id=clinic_id)
    document = parse_exam(exam, now=timezone.now())
    digest = _request_digest(result_version_id, document)
    with transaction.atomic():
        enrollment_id = _eligible_enrollment(clinic_id, result_version_id)
        replayed = _replay(clinic_id, command_id, result_version_id, digest)
        if replayed is not None:
            return replayed
        patient = _shareable_patient(clinic_id, result_version_id, enrollment_id)
        request = _seal(
            clinic_id=clinic_id,
            result_version_id=result_version_id,
            command_id=command_id,
            digest=digest,
            capability=capability,
            exam=document,
            patient=patient,
        )
        receipt = _store(request)
        if receipt.created:
            enqueue_operation(
                ActionOperationRequest(
                    provider=PROVIDER,
                    clinic_id=clinic_id,
                    subject_type=SUBJECT_TYPE,
                    subject_id=receipt.submission_id,
                    idempotency_key=uuid.uuid5(
                        _OPERATION_NAMESPACE, str(receipt.submission_id)
                    ),
                    payload_digest=request.bundle_digest,
                )
            )
            record_phase1_event(
                "interop.rnds_submission.requested",
                clinic_id=clinic_id,
                affected_record_id=receipt.submission_id,
            )
    return receipt


def submission_eligible(scope: OperationScope) -> bool:
    """Recheck at send time: still pending, consented, gated and current."""
    operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
    if operation is None:
        return False
    submission = RndsSubmission.objects.filter(
        pk=operation.subject_id, organization_id=scope.organization_id, state="pending"
    ).first()
    if submission is None or submission.requested_by_id != scope.actor_id:
        return False
    try:
        capability = require_rnds_homologation(clinic_id=submission.clinic_id)
        _eligible_enrollment(submission.clinic_id, submission.result_version_id)
    except (InteropAccessDeniedError, RndsIneligibleError, RndsUnavailableError):
        return False
    return capability.pk == submission.capability_version_id and _consented(
        submission.clinic_id, submission.enrollment_id
    )


@dataclass(frozen=True, slots=True)
class PreparedSubmission:
    """Everything the send needs, loaded inside the tenant transaction."""

    operation_id: UUID
    submission_id: UUID
    organization_id: UUID
    clinic_id: UUID
    actor_id: UUID
    provider: str
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Decision:
    outcome: str
    identifier: str = ""
    code: str = ""


def _decide(answer: RndsAnswer) -> _Decision | None:
    """Classify one RNDS answer; ``None`` means retry the same bundle later."""
    if answer.status == HTTP_CREATED and answer.identifier:
        return _Decision("accepted", identifier=answer.identifier)
    if DUPLICATE in answer.codes:
        # Identifiers are submission ids under this establishment's requester
        # system, so RNDS already holds exactly this document.
        return _Decision("accepted", code=DUPLICATE)
    if answer.status == HTTP_UNAUTHORIZED and TOKEN_EXPIRED in answer.codes:
        return None
    if answer.status >= _HTTP_CLIENT_ERROR:
        code = answer.codes[0] if answer.codes else f"http_{answer.status}"
        return _Decision("rejected", code=code[:_ANSWER_CODE_LIMIT])
    # No readable identifier: resend the same bundle. A copy that already
    # landed answers EHR-ERR866, so a retry never becomes a second document.
    return None


def _settle(prepared: PreparedSubmission, decision: _Decision) -> None:
    """Record the answer; audit it under the requester while authority holds."""
    statement = "SELECT clinic_app.interop_rnds_settle(%s,%s,%s,%s)"
    params: tuple[UUID, str, str, str] = (
        prepared.operation_id,
        decision.outcome,
        decision.identifier,
        decision.code,
    )
    try:
        with tenant_context(prepared.actor_id, prepared.organization_id):
            with connection.cursor() as cursor:
                cursor.execute(statement, params)
                row = cursor.fetchone()
            if row is not None and row[0] is not None:
                record_phase1_event(
                    f"interop.rnds_submission.{decision.outcome}",
                    clinic_id=prepared.clinic_id,
                    affected_record_id=prepared.submission_id,
                )
    except TenantAccessDeniedError:
        # The answer is a fact either way; the outbox completion event audits it.
        with connection.cursor() as cursor:
            cursor.execute(statement, params)


class RndsActionAdapter:
    """Reload the sealed bundle in the tenant transaction; send it outside."""

    provider = PROVIDER

    def prepare(self, operation: IntegrationOperation) -> PreparedSubmission:
        """Refuse any digest drift between the outbox, the row and the bytes."""
        submission = RndsSubmission.objects.filter(
            pk=operation.subject_id,
            organization_id=operation.organization_id,
            state="pending",
        ).first()
        if submission is None:
            raise PermanentSendError
        body = bytes(submission.bundle)
        digest = hashlib.sha256(body).hexdigest()
        if digest != submission.bundle_sha256 or digest != operation.payload_digest:
            raise PermanentSendError
        version = submission.capability_version
        return PreparedSubmission(
            operation_id=operation.pk,
            submission_id=submission.pk,
            organization_id=submission.organization_id,
            clinic_id=submission.clinic_id,
            actor_id=submission.requested_by_id,
            provider=version.provider,
            body=body,
        )

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Send once; settle accepted or rejected, retry transport failures."""
        if (
            not isinstance(prepared, PreparedSubmission)
            or prepared.operation_id != operation_id
        ):
            raise PermanentSendError
        try:
            channel = transport_for(prepared.provider)
            establishment = establishment_for(prepared.provider)
        except RndsUnavailableError as error:
            raise PermanentSendError from error
        try:
            answer = channel.submit_bundle(
                body=prepared.body, requester_cns=establishment.requester_cns
            )
        except RndsTransportError as error:
            if error.kind == "credential":
                raise PermanentSendError from error
            raise TransientSendError from error
        decision = _decide(answer)
        if decision is None:
            raise TransientSendError
        _settle(prepared, decision)
        if decision.outcome != "accepted":
            raise PermanentSendError
        reference = decision.identifier or str(prepared.submission_id)
        return SendResult(provider_reference=f"rnds:{reference}")
