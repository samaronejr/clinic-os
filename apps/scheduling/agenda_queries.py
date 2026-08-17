"""Clinic-local role-scoped day and ISO-week agenda queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.identity.current_context import (
    CurrentActorError,
    list_active_clinic_physicians,
    practitioner_display_label,
)
from apps.scheduling.access import (
    AvailabilityAccessDeniedError,
    AvailabilityViewScope,
    authorized_view_scope,
)
from apps.scheduling.models import Appointment
from apps.scheduling.timezones import (
    civil_day_bounds,
    civil_week_bounds,
    format_local_minute,
)

if TYPE_CHECKING:
    from datetime import datetime

type AgendaView = Literal["day", "week"]
AGENDA_PAGE_SIZE: Final = 25


class AgendaInputError(ValueError):
    """Reject malformed agenda view, date, or page input without reflection."""

    def __init__(self) -> None:
        """Expose one stable non-identifying validation message."""
        super().__init__("agenda input is invalid")


@dataclass(frozen=True, slots=True)
class AgendaItem:
    """Expose one clinic-local appointment row without birth date."""

    appointment_id: UUID
    patient_display_name: str
    practitioner_id: UUID
    practitioner_display_identifier: str
    start_local: str
    end_local: str
    status: str


@dataclass(frozen=True, slots=True)
class AgendaPage:
    """Expose one deterministic bounded agenda page and its UTC bounds."""

    items: tuple[AgendaItem, ...]
    view: AgendaView
    date: date
    page: int
    total: int
    page_count: int
    start_at: datetime
    end_at: datetime


def _inputs(view: str, date_value: str, page: int) -> tuple[AgendaView, date, int]:
    if view == "day":
        selected_view: AgendaView = "day"
    elif view == "week":
        selected_view = "week"
    else:
        raise AgendaInputError
    if type(date_value) is not str:
        raise AgendaInputError
    try:
        civil_date = date.fromisoformat(date_value)
    except ValueError as error:
        raise AgendaInputError from error
    if civil_date.isoformat() != date_value or type(page) is not int or page < 1:
        raise AgendaInputError
    return selected_view, civil_date, page


def _labels(
    scope: AvailabilityViewScope,
    practitioner_ids: tuple[UUID, ...],
) -> dict[UUID, str]:
    if not practitioner_ids:
        return {}
    try:
        if scope.practitioner_id is not None:
            return {
                scope.practitioner_id: practitioner_display_label(
                    scope.practitioner_id,
                    scope.clinic.pk,
                )
            }
        active: dict[UUID, str] = {}
        for physician in list_active_clinic_physicians(scope.clinic.pk):
            active[physician.user_id] = physician.display_label
    except CurrentActorError as error:
        raise AvailabilityAccessDeniedError from error
    return {
        practitioner_id: active.get(practitioner_id, str(practitioner_id))
        for practitioner_id in practitioner_ids
    }


def view_agenda(
    *,
    clinic_id: UUID,
    view: str,
    date: str,
    page: int = 1,
) -> AgendaPage:
    """Return one authorized clinic-local day or ISO-week appointment page."""
    if type(clinic_id) is not UUID:
        raise AvailabilityAccessDeniedError
    with transaction.atomic():
        scope = authorized_view_scope(clinic_id)
        timezone_key = scope.clinic.timezone
        if not isinstance(timezone_key, str):
            raise AvailabilityAccessDeniedError
        selected_view, civil_date, selected_page = _inputs(view, date, page)
        bounds = (
            civil_day_bounds(civil_date, timezone_key)
            if selected_view == "day"
            else civil_week_bounds(civil_date, timezone_key)
        )
        start_at, end_at = bounds
        rows = Appointment.objects.filter(
            organization_id=scope.clinic.organization_id,
            clinic_id=clinic_id,
            start_at__gte=start_at,
            start_at__lt=end_at,
            status__in=(Appointment.Status.SCHEDULED, Appointment.Status.CANCELLED),
        )
        if scope.practitioner_id is not None:
            rows = rows.filter(practitioner_id=scope.practitioner_id)
        ordered = rows.select_related("patient").order_by(
            "start_at",
            "practitioner_id",
            "pk",
        )
        total = ordered.count()
        offset = (selected_page - 1) * AGENDA_PAGE_SIZE
        selected_rows = tuple(ordered[offset : offset + AGENDA_PAGE_SIZE])
        practitioner_ids = tuple(sorted({row.practitioner_id for row in selected_rows}))
        labels = _labels(scope, practitioner_ids)
        items = tuple(
            AgendaItem(
                appointment_id=row.pk,
                patient_display_name=row.patient.full_name,
                practitioner_id=row.practitioner_id,
                practitioner_display_identifier=labels[row.practitioner_id],
                start_local=format_local_minute(row.start_at, timezone_key),
                end_local=format_local_minute(row.end_at, timezone_key),
                status=row.status,
            )
            for row in selected_rows
        )
        result = AgendaPage(
            items=items,
            view=selected_view,
            date=civil_date,
            page=selected_page,
            total=total,
            page_count=(total + AGENDA_PAGE_SIZE - 1) // AGENDA_PAGE_SIZE,
            start_at=start_at,
            end_at=end_at,
        )
        record_phase1_event(
            "scheduling.agenda.viewed",
            clinic_id=clinic_id,
            affected_record_id=clinic_id,
        )
        return result
