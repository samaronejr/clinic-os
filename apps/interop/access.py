"""Current import authority, step-up binding and payload-free refusals."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID

from django.db import DatabaseError, connection
from django.db.models import Model, QuerySet

from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.identity.session_inventory import (
    SessionInventoryDeniedError,
    require_verified_session,
    session_hash,
)
from apps.tenancy.envelope import _kek

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from datetime import datetime

    from apps.tenancy.fields import EncryptedField


class Code(StrEnum):
    """The closed machine vocabulary of import refusals and conflicts."""

    ARCHIVE_LAYOUT = "archive_layout"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    DRY_RUN_STALE = "dry_run_stale"
    DUPLICATES_UNACKNOWLEDGED = "duplicates_unacknowledged"
    EMPTY_DATASET = "empty_dataset"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INTEROP_BATCHES_PENDING = "interop_batches_pending"
    INTEROP_IMMUTABLE = "interop_immutable"
    INTEROP_INVALID = "interop_invalid"
    INTEROP_ROLLBACK_WINDOW = "interop_rollback_window"
    INTEROP_STALE = "interop_stale"
    INTEROP_TRANSITION = "interop_transition"
    INVALID_CHECKSUM = "invalid_checksum"
    INVALID_COMMAND = "invalid_command"
    INVALID_ENCODING = "invalid_encoding"
    INVALID_HEADER = "invalid_header"
    INVALID_MAPPING = "invalid_mapping"
    INVALID_PLAN = "invalid_plan"
    INVALID_PROFILE = "invalid_profile"
    INVALID_REASON = "invalid_reason"
    MALFORMED_ARCHIVE = "malformed_archive"
    MALFORMED_CSV = "malformed_csv"
    MALFORMED_JSON = "malformed_json"
    MALFORMED_SPREADSHEET = "malformed_spreadsheet"
    MANIFEST_MISMATCH = "manifest_mismatch"
    MAPPING_COLUMNS = "mapping_columns"
    MAPPING_SOURCE_KIND = "mapping_source_kind"
    NOTHING_TO_COMMIT = "nothing_to_commit"
    REJECTS_UNACKNOWLEDGED = "rejects_unacknowledged"
    SCAN_UNAVAILABLE = "scan_unavailable"
    SOURCE_SIZE = "source_size"
    TOO_MANY_ROWS = "too_many_rows"
    UNSAFE_SPREADSHEET = "unsafe_spreadsheet"
    UNSUPPORTED_DELIMITER = "unsupported_delimiter"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    UNSUPPORTED_SOURCE = "unsupported_source"


# The clinic staff-management bundle governs migration: clinic managers (and
# legacy clinic admins) hold staff.clinic, organization admins (and owners)
# hold staff.organization at each clinic they administer.
IMPORT_PERMISSIONS: Final = ("staff.clinic", "staff.organization")
CONFLICTS: Final = frozenset(
    {
        Code.INTEROP_STALE,
        Code.INTEROP_TRANSITION,
        Code.INTEROP_INVALID,
        Code.INTEROP_BATCHES_PENDING,
        Code.INTEROP_ROLLBACK_WINDOW,
        Code.INTEROP_IMMUTABLE,
    }
)


class ImportAccessDeniedError(Exception):
    """Unknown, foreign and unauthorized import selectors share one refusal."""

    def __init__(self) -> None:
        """Never reflect a selector, file name or patient value."""
        super().__init__("import access denied")


class ImportStepUpRequiredError(Exception):
    """The command needs this actor's fresh, persisted second-factor session."""

    def __init__(self) -> None:
        """Expose only the fixed step-up requirement."""
        super().__init__("import step-up required")


class ImportConflictError(Exception):
    """A closed machine code describes an invariant, state or replay conflict."""

    def __init__(self, code: str) -> None:
        """Keep the machine-consumed code; never a value from the source."""
        self.code = code
        super().__init__(code)


class ImportInputError(ValueError):
    """A malformed command input, refused before any database effect."""

    def __init__(self, code: str) -> None:
        """Keep the machine-consumed code; never the rejected value."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BatchScope:
    """The stored organization, clinic and requester of one commit batch."""

    organization_id: UUID
    clinic_id: UUID
    actor_id: UUID


def import_authority(*, clinic_id: UUID) -> UUID:
    """Recheck current import authority at this exact clinic on every call."""
    if type(clinic_id) is not UUID:
        raise ImportAccessDeniedError
    for permission in IMPORT_PERMISSIONS:
        try:
            return UUID(str(require_permission(permission, clinic_id=clinic_id)))
        except CurrentActorError:
            continue
    raise ImportAccessDeniedError


def identity_authority(*, clinic_id: UUID) -> UUID:
    """Require import authority plus the clinic's demographics access.

    Dry runs compare the file with existing patient identities and commits
    write them; organization admins hold no demographics access by default
    (intake's duplicate guard refuses them too), so a clinic manager runs them.
    """
    actor = import_authority(clinic_id=clinic_id)
    try:
        require_permission("demographics.read", clinic_id=clinic_id)
    except CurrentActorError:
        raise ImportAccessDeniedError from None
    return actor


def import_clinic(*, clinic_id: UUID) -> Clinic:
    """Load the authorized clinic; RLS and the explicit scope must both agree."""
    clinic = Clinic.objects.filter(pk=clinic_id).first()
    if clinic is None:
        raise ImportAccessDeniedError
    return clinic


def record[T: Model](model: type[T], *, clinic_id: UUID, record_id: UUID) -> T:
    """Refuse records outside the explicit clinic even when RLS exposes them."""
    if type(record_id) is not UUID:
        raise ImportAccessDeniedError
    row = QuerySet[T](model=model).filter(pk=record_id, clinic_id=clinic_id).first()
    if row is None:
        raise ImportAccessDeniedError
    return row


def blind_index_sets(
    *, purpose: str, plaintexts: Sequence[bytes]
) -> tuple[frozenset[bytes], ...]:
    """Digest many values under every tenant DEK version in one round trip."""
    if not plaintexts:
        return ()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.position, b.blind_index FROM unnest(%s::bytea[]) WITH ORDINALITY"
            " AS p(plaintext, position) CROSS JOIN LATERAL"
            " clinic_app.protected_blind_index(%s, %s, p.plaintext) AS b"
            " ORDER BY p.position, b.key_version",
            [list(plaintexts), _kek(), purpose],
        )
        rows = cursor.fetchall()
    digests: list[set[bytes]] = [set() for _ in plaintexts]
    for position, digest in rows:
        digests[int(position) - 1].add(bytes(digest))
    if any(not digest for digest in digests):
        raise ImportAccessDeniedError
    return tuple(frozenset(digest) for digest in digests)


def import_step_up(*, session_key: str) -> None:
    """Bind this transaction to the actor's fresh verified framework session.

    The database guards re-verify the same digest before a commit batch or a
    post-approval rollback is recorded; the key itself is never stored.
    """
    if not isinstance(session_key, str) or not session_key:
        raise ImportStepUpRequiredError
    try:
        require_verified_session(session_key=session_key)
        digest = session_hash(session_key).hex()
    except (SessionInventoryDeniedError, UnicodeEncodeError) as error:
        raise ImportStepUpRequiredError from error
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_import_session', %s, true)", [digest]
        )


def clinical_reader(*, clinic_id: UUID, enrollment_id: UUID) -> UUID:
    """Require current patient-scoped clinical access to imported history."""
    if type(clinic_id) is not UUID or type(enrollment_id) is not UUID:
        raise ImportAccessDeniedError
    try:
        return UUID(
            str(
                require_permission(
                    "clinical.read",
                    clinic_id=clinic_id,
                    patient_enrollment_id=enrollment_id,
                )
            )
        )
    except CurrentActorError:
        raise ImportAccessDeniedError from None


def batch_scope(batch_id: UUID) -> BatchScope | None:
    """Resolve a queued batch's stored scope; the message carries no authority."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT organization_id, clinic_id, actor_id "
            "FROM clinic_app.interop_batch_scope(%s)",
            [batch_id],
        )
        row = cursor.fetchone()
    if row is None or not all(isinstance(value, UUID) for value in row):
        return None
    return BatchScope(organization_id=row[0], clinic_id=row[1], actor_id=row[2])


def hide_reversible(job_id: UUID) -> tuple[tuple[UUID, bool], ...]:
    """Hide every still-unused imported enrollment of a rolled-back job."""
    with translated(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT imported_enrollment, hidden "
            "FROM clinic_app.interop_hide_reversible(%s)",
            [job_id],
        )
        rows = cursor.fetchall()
    return tuple((row[0], bool(row[1])) for row in rows)


def opened(model: type[Model], field: str, envelope: bytes | None) -> Any:  # noqa: ANN401
    """Open one sealed payload with its own field's envelope purpose and codec."""
    sealed = cast("EncryptedField", model._meta.get_field(field))  # noqa: SLF001
    return sealed.from_db_value(envelope, None, connection)


def source_envelope(job_id: UUID) -> bytes | None:
    """Return the sealed stored source, only to the pipeline of an in-flight job."""
    with translated(), connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.interop_source(%s)", [job_id])
        row = cursor.fetchone()
    return None if row is None or row[0] is None else bytes(row[0])


@dataclass(frozen=True, slots=True)
class HistoryEnvelope:
    """One imported history row: identifiers and its still-sealed payloads."""

    record_id: UUID
    job_id: UUID
    kind: str
    row_number: int
    original_author: bytes | None
    original_recorded_at: datetime | None
    content: bytes | None


def history_envelopes(
    *, clinic_id: UUID, enrollment_id: UUID
) -> tuple[HistoryEnvelope, ...]:
    """Sealed imported history the current clinical reader may see."""
    with translated(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT record_id, job_id, kind, row_number, original_author,"
            " original_recorded_at, content FROM clinic_app.interop_history(%s, %s)",
            [clinic_id, enrollment_id],
        )
        rows = cursor.fetchall()
    return tuple(
        HistoryEnvelope(
            record_id=row[0],
            job_id=row[1],
            kind=str(row[2]),
            row_number=int(row[3]),
            original_author=None if row[4] is None else bytes(row[4]),
            original_recorded_at=row[5],
            content=None if row[6] is None else bytes(row[6]),
        )
        for row in rows
    )


@dataclass(frozen=True, slots=True)
class AttachmentEnvelope:
    """One imported document's metadata and its still-sealed bytes."""

    record_id: UUID
    job_id: UUID
    enrollment_id: UUID
    media_type: str
    sha256: str
    data: bytes


def attachment_envelope(*, clinic_id: UUID, record_id: UUID) -> AttachmentEnvelope:
    """One sealed imported document the current clinical reader may open."""
    if type(clinic_id) is not UUID or type(record_id) is not UUID:
        raise ImportAccessDeniedError
    with translated(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT record_id, job_id, enrollment_id, media_type, sha256, data"
            " FROM clinic_app.interop_attachment(%s, %s)",
            [clinic_id, record_id],
        )
        row = cursor.fetchone()
    if row is None:
        raise ImportAccessDeniedError
    return AttachmentEnvelope(
        record_id=row[0],
        job_id=row[1],
        enrollment_id=row[2],
        media_type=str(row[3]),
        sha256=str(row[4]),
        data=bytes(row[5]),
    )


def attachment_checks(job_id: UUID) -> tuple[int, int, int]:
    """Linked attachments, their bytes and checksum mismatches, counted in SQL."""
    with translated(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT files, total_bytes, mismatches"
            " FROM clinic_app.interop_attachment_checks(%s, %s)",
            [job_id, _kek()],
        )
        row = cursor.fetchone()
    if row is None:
        raise ImportAccessDeniedError
    return int(row[0]), int(row[1]), int(row[2])


@contextmanager
def translated() -> Iterator[None]:
    """Map guard refusals to the domain errors without echoing database text."""
    try:
        yield
    except DatabaseError as error:
        state = getattr(error.__cause__, "sqlstate", None)
        code = getattr(getattr(error.__cause__, "diag", None), "message_primary", "")
        if code == "interop_step_up":
            raise ImportStepUpRequiredError from None
        if state == "42501":
            raise ImportAccessDeniedError from None
        if code in CONFLICTS:
            raise ImportConflictError(code) from None
        if state == "23505":
            raise ImportConflictError(Code.INTEROP_STALE) from None
        raise
