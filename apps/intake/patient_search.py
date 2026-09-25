"""Clinic-scoped patient registry search service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from django.db import connection, transaction

from apps.audit.services import record_phase1_event
from apps.core.idempotency import PatientNameValueError, normalize_patient_name
from apps.intake.access import authorized_manager_clinic
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
    """Expose one body-only clinic enrollment search result.

    ``display_name`` prefers the recorded social name (then legal name,
    then the registry name) so staff surfaces greet the patient the way
    the patient asked; ``full_name`` stays the legal registry name.
    """

    enrollment_id: UUID
    full_name: str
    birth_date: date
    display_name: str = ""


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
                "SELECT enrollment_id, full_name, birth_date, social_name "
                "FROM clinic_app.patient_registry_page(%s, %s, %s, %s, %s, %s)",
                [kek, str(clinic_id), normalized_query, birth_date, offset, PAGE_SIZE],
            )
            rows = cursor.fetchall()
        items = tuple(
            PatientSearchItem(
                enrollment_id=enrollment_id,
                full_name=full_name,
                birth_date=stored_birth,
                display_name=social_name or full_name,
            )
            for enrollment_id, full_name, stored_birth, social_name in rows
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
