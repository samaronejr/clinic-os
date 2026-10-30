"""LGPD data-subject requests (IS-19) for patient sessions and privacy staff.

Patients and verified delegates file requests through the session-bound
``clinic_app.submit_data_request`` resolver. The database binds the session,
the subject enrollment and the delegate grant, stamps the 15-day due date from
``clinic_app.data_request_now()`` and opens one staff review task due at the
same moment. Access and portability reuse the deterministic released-record
export, so a record restricted from delegates never leaves for a delegate.
Correction never edits clinical content: staff review it, and any clinical
change is a clinician's amendment. An objection withdraws the named optional
purpose at once when the subject holds an accepted text for it; otherwise it
waits for staff like every other request. Every refusal is the same
``PatientAccessDeniedError`` for unknown, foreign and out-of-scope selectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

import rfc8785
from django.db import DatabaseError, connection, transaction
from django.db.models import BooleanField, Q
from django.db.models.expressions import RawSQL
from psycopg import Error as PostgreSQLError

from apps.audit.events import build_phase1_audit_event
from apps.audit.services import AuditTrustedContext, _content_hash, record_phase1_event
from apps.consent.models import ConsentAcceptance, ConsentPurpose
from apps.consent.services import revoke_consent
from apps.identity.current_context import CurrentActorError, require_permission
from apps.intake.access import PatientAccessDeniedError
from apps.intake.data_request_models import (
    DATA_REQUEST_KIND_VALUES,
    DATA_REQUEST_REJECTION_VALUES,
    DataSubjectRequest,
)
from apps.providers.models import ProviderCapability
from apps.providers.services import is_live
from apps.retention.services import (
    RetentionAccessDeniedError,
    RetentionConflictError,
    export_patient_records,
)
from apps.tenancy.envelope import blind_indexes, protect

if TYPE_CHECKING:
    from collections.abc import Sequence

    type _Parameter = (
        str | int | bytes | UUID | datetime | list[str] | list[UUID] | None
    )

__all__ = (
    "DATA_REQUEST_DETAILS_MAX",
    "DATA_REQUEST_DUE",
    "DATA_REQUEST_KINDS",
    "DATA_REQUEST_OPERATIONS",
    "DATA_REQUEST_REJECTION_REASONS",
    "OBJECTION_PURPOSES",
    "PRIVACY_PERMISSION",
    "DataRequestConflictError",
    "DataRequestInputError",
    "DataRequestTransition",
    "DataSubjectCopy",
    "DataSubjectRequestReceipt",
    "PatientDataRequest",
    "PatientRequestScope",
    "SharingEntry",
    "StaffDataRequest",
    "export_data_subject_copy",
    "fulfill_data_subject_request",
    "patient_data_subject_requests",
    "patient_request_scope",
    "reject_data_subject_request",
    "sharing_information",
    "staff_data_subject_requests",
    "start_data_subject_request",
    "submit_data_subject_request",
)

DATA_REQUEST_KINDS: Final = DATA_REQUEST_KIND_VALUES
DATA_REQUEST_REJECTION_REASONS: Final = DATA_REQUEST_REJECTION_VALUES
DATA_REQUEST_DUE: Final = timedelta(days=15)
DATA_REQUEST_DETAILS_MAX: Final = 2000
# The patient-session operation each right needs; the resolver maps the same.
DATA_REQUEST_OPERATIONS: Final = {
    "access": "records",
    "correction": "enrollment_view",
    "portability": "records",
    "sharing_information": "enrollment_view",
    "objection": "consent",
}
# Only optional purposes can be withdrawn; care processing is not consent-based.
OBJECTION_PURPOSES: Final = tuple(ConsentPurpose.values)
EXPORTABLE_KINDS: Final = frozenset({"access", "portability"})
PRIVACY_PERMISSION: Final = "privacy.requests"
_DETAILS_PURPOSE: Final = "intake.datasubjectrequest.details"
_FINGERPRINT_PURPOSE: Final = "intake.datasubjectrequest.fingerprint"
_STAFF_EVENTS: Final = {
    "in_progress": "intake.data_request.started",
    "fulfilled": "intake.data_request.fulfilled",
    "rejected": "intake.data_request.rejected",
}


class DataRequestInputError(ValueError):
    """Reject a malformed request before any write, without echoing input."""

    def __init__(self) -> None:
        """Keep the message free of request content."""
        super().__init__("invalid data subject request")


class DataRequestConflictError(Exception):
    """Reject a reused key or a stale revision without exposing the record."""

    def __init__(self) -> None:
        """Expose only the fixed conflict code."""
        super().__init__("data subject request conflict")


@dataclass(frozen=True, slots=True)
class DataSubjectRequestReceipt:
    """The retained result of one submission; replays return the same value."""

    request_id: UUID
    kind: str
    state: str
    revision: int
    received_at: datetime
    due_at: datetime
    task_id: UUID
    delegate_acted: bool


@dataclass(frozen=True, slots=True)
class PatientDataRequest:
    """One request the current session may see; correction text stays staff-side."""

    request_id: UUID
    kind: str
    purpose: str
    state: str
    rejection_reason: str
    received_at: datetime
    due_at: datetime
    closed_at: datetime | None
    delegate_acted: bool


@dataclass(frozen=True, slots=True)
class StaffDataRequest:
    """One queued request for privacy staff, overdue by database time."""

    request_id: UUID
    enrollment_id: UUID
    kind: str
    purpose: str
    details: str
    state: str
    rejection_reason: str
    received_at: datetime
    due_at: datetime
    closed_at: datetime | None
    revision: int
    overdue: bool
    delegate_acted: bool


@dataclass(frozen=True, slots=True)
class DataRequestTransition:
    """The state a staff command left the request in."""

    request_id: UUID
    state: str
    revision: int


@dataclass(frozen=True, slots=True)
class SharingEntry:
    """One processor capability and whether it may handle real data now."""

    capability_key: str
    state: str
    live: bool


@dataclass(frozen=True, slots=True)
class DataSubjectCopy:
    """The verifiable released-record package delivered for one request."""

    data: bytes
    file_name: str
    record_count: int


@dataclass(frozen=True, slots=True)
class PatientRequestScope:
    """What the current session may file; the resolver rechecks every value."""

    clinic_id: UUID
    kinds: tuple[str, ...]
    objection_purposes: tuple[str, ...]
    acting_for_patient: bool


@dataclass(frozen=True, slots=True)
class _Authority:
    session_id: UUID
    organization_id: UUID
    clinic_id: UUID


def _authority(operation: str | None) -> _Authority | None:
    """Return the live patient authority for one operation (any when None)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT session_id,organization_id,clinic_id "
            "FROM clinic_app.current_patient_authority(%s)",
            [operation],
        )
        row = cursor.fetchone()
    return None if row is None else _Authority(row[0], row[1], row[2])


def _resolver_row(sql: str, parameters: Sequence[_Parameter]) -> tuple[object, ...]:
    """Run one fixed resolver command and map its SQLSTATE to the contract."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            row = cursor.fetchone()
    except DatabaseError as error:
        cause = error.__cause__
        if isinstance(cause, PostgreSQLError):
            if cause.sqlstate == "42501":
                raise PatientAccessDeniedError from error
            if cause.sqlstate == "40001":
                raise DataRequestConflictError from error
            if cause.sqlstate in {"22023", "23514"}:
                raise DataRequestInputError from error
        raise
    if row is None:
        raise PatientAccessDeniedError
    return tuple(row)


def _receipt(row: tuple[object, ...]) -> DataSubjectRequestReceipt:
    """Type-check one submission row; anything unexpected fails closed."""
    request_id, kind, state, revision, received_at, due_at, task_id, acted = row[:8]
    if not (
        isinstance(request_id, UUID)
        and isinstance(kind, str)
        and isinstance(state, str)
        and type(revision) is int
        and isinstance(received_at, datetime)
        and isinstance(due_at, datetime)
        and isinstance(task_id, UUID)
        and type(acted) is bool
    ):
        raise PatientAccessDeniedError
    return DataSubjectRequestReceipt(
        request_id, kind, state, revision, received_at, due_at, task_id, acted
    )


def _validated(kind: object, details: object, purpose: object) -> str:
    """Return the trimmed correction text; reject every other shape."""
    if (
        type(kind) is not str
        or kind not in DATA_REQUEST_KINDS
        or type(details) is not str
        or type(purpose) is not str
    ):
        raise DataRequestInputError
    text = details.strip()
    if kind == "correction":
        if not text or len(details) > DATA_REQUEST_DETAILS_MAX:
            raise DataRequestInputError
    elif details:
        raise DataRequestInputError
    if (kind == "objection") != bool(purpose) or (
        kind == "objection" and purpose not in OBJECTION_PURPOSES
    ):
        raise DataRequestInputError
    return text


def _patient_audit(event_name: str, record_id: UUID) -> None:
    """Chain one patient-session event through the request-bound resolver."""
    authority = _authority(None)
    if authority is None:
        raise PatientAccessDeniedError
    event = build_phase1_audit_event(
        event_name, clinic_id=authority.clinic_id, affected_record_id=record_id
    )
    content_hash = _content_hash(
        event.event,
        AuditTrustedContext(
            organization_id=authority.organization_id,
            actor_user_id=authority.session_id,
        ),
        dict(event.payload),
    )
    _resolver_row(
        "SELECT clinic_app.data_request_audit(%s,%s,%s,%s)",
        [record_id, event_name, event.event.occurred_at_utc, content_hash],
    )


def _withdraw(purpose: str) -> list[UUID]:
    """Revoke every accepted text of one purpose for the subject enrollment."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT s.enrollment_id FROM clinic_app.current_patient_authority("
            "'consent') s WHERE clinic_app.delegate_purpose_allowed(%s)",
            [purpose],
        )
        row = cursor.fetchone()
    if row is None:
        raise PatientAccessDeniedError
    accepted = ConsentAcceptance.objects.filter(
        enrollment_id=row[0], text__purpose=purpose, revocation__isnull=True
    ).order_by("accepted_at", "pk")
    return [revoke_consent(acceptance_id=item.pk).pk for item in accepted]


def submit_data_subject_request(
    *,
    clinic_id: UUID,
    kind: str,
    details: str,
    purpose: str,
    idempotency_key: UUID,
) -> DataSubjectRequestReceipt:
    """File one request for the session's subject; a replayed key returns it."""
    if type(clinic_id) is not UUID:
        raise PatientAccessDeniedError
    text = _validated(kind, details, purpose)
    if type(idempotency_key) is not UUID:
        raise DataRequestInputError
    authority = _authority(DATA_REQUEST_OPERATIONS[kind])
    if authority is None or authority.clinic_id != clinic_id:
        raise PatientAccessDeniedError
    with transaction.atomic():
        withdrawn = _withdraw(purpose) if kind == "objection" else []
        envelope = (
            protect(purpose=_DETAILS_PURPOSE, plaintext=text.encode("utf-8"))
            if kind == "correction"
            else None
        )
        fingerprints = [
            digest.hex()
            for digest in blind_indexes(
                purpose=_FINGERPRINT_PURPOSE,
                plaintext=rfc8785.dumps(
                    {"kind": kind, "purpose": purpose, "details": text}
                ),
            )
        ]
        row = _resolver_row(
            "SELECT * FROM clinic_app.submit_data_request("
            "%s,%s,%s,%s,%s,%s::text[],%s::uuid[])",
            [
                clinic_id,
                kind,
                purpose,
                envelope,
                idempotency_key,
                fingerprints,
                withdrawn,
            ],
        )
        receipt = _receipt(row)
        if row[8] is True:
            _patient_audit("intake.data_request.received", receipt.request_id)
            if receipt.state == DataSubjectRequest.State.FULFILLED:
                _patient_audit("intake.data_request.fulfilled", receipt.request_id)
        return receipt


def patient_request_scope() -> PatientRequestScope | None:
    """Return the rights this session may exercise, or None without a session."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_id,operations,delegate_grant_id IS NOT NULL "
            "FROM clinic_app.current_patient_authority(NULL)"
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            "SELECT p FROM unnest(%s::text[]) p "
            "WHERE clinic_app.delegate_purpose_allowed(p) ORDER BY p",
            [list(OBJECTION_PURPOSES)],
        )
        purposes = tuple(str(item[0]) for item in cursor.fetchall())
    clinic_id, operations, acting = row
    granted = frozenset(operations)
    return PatientRequestScope(
        clinic_id=clinic_id,
        kinds=tuple(
            kind
            for kind in DATA_REQUEST_KINDS
            if DATA_REQUEST_OPERATIONS[kind] in granted
            and (kind != "objection" or purposes)
        ),
        objection_purposes=purposes,
        acting_for_patient=acting is True,
    )


def patient_data_subject_requests() -> tuple[PatientDataRequest, ...]:
    """List the requests this session may see, newest first."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.patient_data_requests()")
        return tuple(PatientDataRequest(*row) for row in cursor.fetchall())


def export_data_subject_copy(*, clinic_id: UUID, request_id: UUID) -> DataSubjectCopy:
    """Deliver the released-record export for one visible access request."""
    if type(clinic_id) is not UUID or type(request_id) is not UUID:
        raise PatientAccessDeniedError
    authority = _authority(None)
    if authority is None or authority.clinic_id != clinic_id:
        raise PatientAccessDeniedError
    request = next(
        (
            row
            for row in patient_data_subject_requests()
            if row.request_id == request_id
        ),
        None,
    )
    if request is None:
        raise PatientAccessDeniedError
    if (
        request.kind not in EXPORTABLE_KINDS
        or request.state == DataSubjectRequest.State.REJECTED
    ):
        raise DataRequestInputError
    with transaction.atomic():
        try:
            package = export_patient_records()
        except RetentionAccessDeniedError as error:
            raise PatientAccessDeniedError from error
        except RetentionConflictError as error:
            raise DataRequestConflictError from error
        _patient_audit("intake.data_request.exported", request_id)
    return DataSubjectCopy(
        data=package.data,
        file_name=f"meus-dados-{request_id}.zip",
        record_count=package.export.record_count,
    )


def sharing_information() -> tuple[SharingEntry, ...]:
    """Project the provider registry for the session clinic, without secrets."""
    authority = _authority(None)
    if authority is None:
        raise PatientAccessDeniedError
    chosen: dict[str, ProviderCapability] = {}
    for capability in ProviderCapability.objects.select_related(
        "current_version"
    ).filter(Q(clinic_id__isnull=True) | Q(clinic_id=authority.clinic_id)):
        if capability.key not in chosen or capability.clinic_id is not None:
            chosen[capability.key] = capability
    return tuple(
        SharingEntry(
            capability_key=key,
            state=(
                capability.current_version.state
                if capability.current_version is not None
                else "unregistered"
            ),
            live=is_live(key, clinic_id=authority.clinic_id),
        )
        for key, capability in sorted(chosen.items())
    )


def _require_privacy(clinic_id: object) -> UUID:
    """Admit only clinic privacy staff; everyone else matches an unknown request."""
    if type(clinic_id) is not UUID:
        raise PatientAccessDeniedError
    try:
        return require_permission(PRIVACY_PERMISSION, clinic_id=clinic_id)
    except CurrentActorError as error:
        raise PatientAccessDeniedError from error


def staff_data_subject_requests(*, clinic_id: UUID) -> tuple[StaffDataRequest, ...]:
    """List the clinic's requests for privacy staff, earliest deadline first."""
    _require_privacy(clinic_id)
    rows = (
        DataSubjectRequest.objects.filter(clinic_id=clinic_id)
        .annotate(
            overdue=RawSQL(
                "intake_datasubjectrequest.state IN ('received','in_progress') "
                "AND clinic_app.data_request_now()>=intake_datasubjectrequest.due_at",
                (),
                output_field=BooleanField(),
            )
        )
        .order_by("due_at", "received_at", "pk")
    )
    requests = tuple(
        StaffDataRequest(
            request_id=row.pk,
            enrollment_id=row.enrollment_id,
            kind=row.kind,
            purpose=row.purpose,
            details=row.details,
            state=row.state,
            rejection_reason=row.rejection_reason,
            received_at=row.received_at,
            due_at=row.due_at,
            closed_at=row.closed_at,
            revision=row.revision,
            overdue=bool(row.overdue),
            delegate_acted=row.delegate_grant_id is not None,
        )
        for row in rows
    )
    record_phase1_event(
        "intake.data_requests.viewed", clinic_id=clinic_id, affected_record_id=clinic_id
    )
    return requests


def _transition(
    clinic_id: UUID, request_id: UUID, expected_revision: int, state: str, reason: str
) -> DataRequestTransition:
    """Apply one staff answer; the database closes the SLA task on a final state."""
    _require_privacy(clinic_id)
    if type(request_id) is not UUID:
        raise PatientAccessDeniedError
    if type(expected_revision) is not int or expected_revision < 1:
        raise DataRequestInputError
    with transaction.atomic():
        row = _resolver_row(
            "SELECT * FROM clinic_app.transition_data_request(%s,%s,%s,%s,%s)",
            [clinic_id, request_id, expected_revision, state, reason],
        )
        request, current, revision = row
        if not (
            isinstance(request, UUID)
            and isinstance(current, str)
            and type(revision) is int
        ):
            raise PatientAccessDeniedError
        record_phase1_event(
            _STAFF_EVENTS[state], clinic_id=clinic_id, affected_record_id=request
        )
        return DataRequestTransition(request, current, revision)


def start_data_subject_request(
    *, clinic_id: UUID, request_id: UUID, expected_revision: int
) -> DataRequestTransition:
    """Take a received request into review."""
    return _transition(clinic_id, request_id, expected_revision, "in_progress", "")


def fulfill_data_subject_request(
    *, clinic_id: UUID, request_id: UUID, expected_revision: int
) -> DataRequestTransition:
    """Record that the clinic answered the request; it never edits a record."""
    return _transition(clinic_id, request_id, expected_revision, "fulfilled", "")


def reject_data_subject_request(
    *, clinic_id: UUID, request_id: UUID, expected_revision: int, reason_code: str
) -> DataRequestTransition:
    """Close a request with one fixed reason the patient can see."""
    if (
        type(reason_code) is not str
        or reason_code not in DATA_REQUEST_REJECTION_REASONS
    ):
        _require_privacy(clinic_id)
        raise DataRequestInputError
    return _transition(
        clinic_id, request_id, expected_revision, "rejected", reason_code
    )
