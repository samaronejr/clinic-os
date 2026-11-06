"""Released results for patient sessions: listing and audited source downloads.

``clinic_app.orders_patient_sources`` already rechecks the session, the
delegate grant, the release audience, the acknowledgment and that the version
is current; this module never widens it. A download materializes the exact
stored bytes after the size and SHA-256 checks and chains one
``orders.result.patient_downloaded`` event bound to the live session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.db import DatabaseError, connection, transaction
from psycopg import Error as PostgreSQLError

from apps.audit.events import build_phase1_audit_event
from apps.audit.services import AuditTrustedContext, _content_hash
from apps.orders.queries import patient_sources
from apps.orders.storage import source_bytes
from apps.orders.values import OrdersAccessDeniedError

if TYPE_CHECKING:
    from datetime import datetime

DOWNLOAD_EVENT: Final = "orders.result.patient_downloaded"
_EXTENSIONS: Final = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
}


@dataclass(frozen=True, slots=True)
class PatientResult:
    """One released, current result version the session may open."""

    version_id: UUID
    number: int
    created_at: datetime
    size_bytes: int
    content_type: str

    @property
    def corrected(self) -> bool:
        """A version after the first replaced an earlier report."""
        return self.number > 1


@dataclass(frozen=True, slots=True)
class PatientResultFile:
    """The verified source bytes of one released result version."""

    data: bytes
    content_type: str
    file_name: str


def patient_results() -> tuple[PatientResult, ...]:
    """List the released current versions for this session, newest first."""
    return tuple(
        PatientResult(
            version_id=source.version_id,
            number=source.number,
            created_at=source.created_at,
            size_bytes=source.size_bytes,
            content_type=source.content_type,
        )
        for source in patient_sources()
    )


def _audit(version_id: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT session_id,organization_id,clinic_id "
            "FROM clinic_app.current_patient_authority('records')"
        )
        row = cursor.fetchone()
    if row is None:
        raise OrdersAccessDeniedError
    session_id, organization_id, clinic_id = row
    event = build_phase1_audit_event(
        DOWNLOAD_EVENT, clinic_id=clinic_id, affected_record_id=version_id
    )
    content_hash = _content_hash(
        event.event,
        AuditTrustedContext(organization_id=organization_id, actor_user_id=session_id),
        dict(event.payload),
    )
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.orders_patient_download_audit(%s,%s,%s)",
                [version_id, event.event.occurred_at_utc, content_hash],
            )
    except DatabaseError as error:
        cause = error.__cause__
        if isinstance(cause, PostgreSQLError) and cause.sqlstate == "42501":
            raise OrdersAccessDeniedError from error
        raise


def patient_result_file(*, version_id: UUID) -> PatientResultFile:
    """Return one released source; unknown and unreleased ids are one denial."""
    if type(version_id) is not UUID:
        raise OrdersAccessDeniedError
    sources = patient_sources(version_id=version_id)
    if len(sources) != 1:
        raise OrdersAccessDeniedError
    source = sources[0]
    with transaction.atomic():
        data = source_bytes(source)
        _audit(version_id)
    extension = _EXTENSIONS.get(source.content_type, "bin")
    return PatientResultFile(
        data=data,
        content_type=source.content_type,
        file_name=f"resultado-{source.number}.{extension}",
    )
