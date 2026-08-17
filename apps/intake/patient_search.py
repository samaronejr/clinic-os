"""Clinic-scoped patient registry search service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from django.db import transaction
from django.db.models.functions import Lower

from apps.audit.services import record_phase1_event
from apps.core.idempotency import PatientNameValueError, normalize_patient_name
from apps.intake.access import authorized_manager_clinic
from apps.intake.models import PatientClinicEnrollment

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
        clinic = authorized_manager_clinic(clinic_id)
        enrollments = PatientClinicEnrollment.objects.filter(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            patient__full_name__icontains=normalized_query,
        )
        if birth_date is not None:
            enrollments = enrollments.filter(patient__birth_date=birth_date)
        ordered = enrollments.select_related("patient").order_by(
            Lower("patient__full_name"),
            "patient__birth_date",
            "patient_id",
        )
        total = ordered.count()
        offset = (page - 1) * PAGE_SIZE
        rows = tuple(ordered[offset : offset + PAGE_SIZE])
        items = tuple(
            PatientSearchItem(
                enrollment_id=enrollment.pk,
                full_name=enrollment.patient.full_name,
                birth_date=enrollment.patient.birth_date,
            )
            for enrollment in rows
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
