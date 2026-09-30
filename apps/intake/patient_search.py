"""Clinic-scoped patient registry search service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from django.db import connection, transaction

from apps.audit.services import record_phase1_event
from apps.core.idempotency import PatientNameValueError, normalize_patient_name
from apps.identity.current_context import CurrentActorError, require_permission
from apps.intake.access import PatientAccessDeniedError, authorized_manager_clinic
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_name_index import name_indexes
from apps.tenancy.envelope import _kek

if TYPE_CHECKING:
    from uuid import UUID

PAGE_SIZE: Final = 25
MIN_QUERY_LENGTH: Final = 2
MAX_QUERY_LENGTH: Final = 100


class PatientSearchInputError(ValueError):
    """Reject a malformed registry-search value without reflecting it."""

    def __init__(self) -> None:
        """Expose one stable non-identifying validation message."""
        super().__init__("patient search input is invalid")


@dataclass(frozen=True, slots=True)
class PatientSearchItem:
    """Expose one body-only clinic enrollment search result."""

    enrollment_id: UUID
    full_name: str
    birth_date: date


@dataclass(frozen=True, slots=True)
class PatientSearchPage:
    """Expose one deterministic page and its bounded pagination metadata."""

    items: tuple[PatientSearchItem, ...]
    page: int
    total: int
    page_count: int


def _normalized_query(query: str) -> str:
    try:
        normalized = normalize_patient_name(query)
    except PatientNameValueError as error:
        raise PatientSearchInputError from error
    if not MIN_QUERY_LENGTH <= len(normalized) <= MAX_QUERY_LENGTH:
        raise PatientSearchInputError
    return normalized


def search_patients_exact(
    *, clinic_id: UUID, query: str, limit: int
) -> PatientSearchPage:
    """Select by tenant HMAC first, then reveal only the bounded exact matches."""
    query = _normalized_query(query)
    authorized_manager_clinic(clinic_id)
    try:
        require_permission("demographics.read", clinic_id=clinic_id)
    except CurrentActorError as error:
        raise PatientAccessDeniedError from error
    matches = PatientClinicEnrollment.objects.filter(
        clinic_id=clinic_id, patient__full_name_index__in=name_indexes(query)
    ).order_by("id")
    total = matches.count()
    items = (
        tuple(
            PatientSearchItem(row.pk, row.patient.full_name, row.patient.birth_date)
            for row in matches.select_related("patient")[:limit]
        )
        if total <= limit
        else ()
    )
    record_phase1_event(
        "intake.patient.searched", clinic_id=clinic_id, affected_record_id=clinic_id
    )
    return PatientSearchPage(
        items=items, page=1, total=total, page_count=(total + limit - 1) // limit
    )


def search_patients(
    *,
    clinic_id: UUID,
    query: str,
    page: int,
    birth_date: date | None = None,
) -> PatientSearchPage:
    """Return one manager-authorized selected-clinic registry page."""
    normalized_query = _normalized_query(query)
    if type(page) is not int or page < 1:
        raise PatientSearchInputError
    if birth_date is not None and type(birth_date) is not date:
        raise PatientSearchInputError

    with transaction.atomic():
        authorized_manager_clinic(clinic_id)
        # Patient identity lives only in tenant envelopes; the approved
        # search contract (normalized substring, optional exact birth date,
        # lower(name)/birth_date/patient ordering, 25-row pages) executes
        # inside the database boundary so ciphertext never feeds predicates
        # and only the authorized page's plaintext leaves the database.
        kek = _kek()
        offset = (page - 1) * PAGE_SIZE
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.patient_registry_count(%s, %s, %s, %s)",
                [kek, str(clinic_id), normalized_query, birth_date],
            )
            count_row = cursor.fetchone()
            total = 0 if count_row is None else int(count_row[0])
            cursor.execute(
                "SELECT enrollment_id, full_name, birth_date "
                "FROM clinic_app.patient_registry_page(%s, %s, %s, %s, %s, %s)",
                [kek, str(clinic_id), normalized_query, birth_date, offset, PAGE_SIZE],
            )
            rows = cursor.fetchall()
        items = tuple(
            PatientSearchItem(
                enrollment_id=enrollment_id,
                full_name=full_name,
                birth_date=stored_birth,
            )
            for enrollment_id, full_name, stored_birth in rows
        )
        record_phase1_event(
            "intake.patient.searched",
            clinic_id=clinic_id,
            affected_record_id=clinic_id,
        )
        return PatientSearchPage(
            items=items,
            page=page,
            total=total,
            page_count=(total + PAGE_SIZE - 1) // PAGE_SIZE,
        )
