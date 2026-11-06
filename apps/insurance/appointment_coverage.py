"""Minimal booking projections, not a new clinical or financial read permission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db import connection

from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.resource_errors import SchedulingRuleError

if TYPE_CHECKING:
    from datetime import date

    from apps.identity.models import Clinic
    from apps.scheduling.appointment_values import (
        CreateAppointmentRequest,
        PreparedAppointment,
    )


@dataclass(frozen=True, slots=True)
class BookingCoverage:
    """Only the selected membership/contract identities and coverage dates."""

    membership_id: UUID
    contract_id: UUID
    clinic_id: UUID
    patient_id: UUID
    payer_id: UUID
    contract_payer_id: UUID
    valid_from: date
    valid_to: date | None
    starts_on: date
    ends_on: date | None
    eligibility_supported: bool
    authorization_supported: bool


def booking_coverage(
    *, clinic_id: UUID, patient_id: UUID, membership_id: UUID, contract_id: UUID
) -> BookingCoverage:
    """Use the fixed booking-authorized projection, never query around RLS."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.insurance_booking_coverage(%s,%s,%s,%s)",
            [clinic_id, patient_id, membership_id, contract_id],
        )
        row = cursor.fetchone()
    if row is None:
        raise AppointmentAccessDeniedError
    return BookingCoverage(
        UUID(str(row[0])),
        UUID(str(row[1])),
        UUID(str(row[2])),
        UUID(str(row[3])),
        UUID(str(row[4])),
        UUID(str(row[5])),
        cast("date", row[6]),
        cast("date | None", row[7]),
        cast("date", row[8]),
        cast("date | None", row[9]),
        bool(row[10]),
        bool(row[11]),
    )


def validate_insured_booking(
    *,
    clinic: Clinic,
    request: CreateAppointmentRequest,
    prepared: PreparedAppointment,
) -> None:
    """Refuse foreign selectors and unsupported payer promises before insertion."""
    member, contract = request.payer_membership_id, request.payer_contract_id
    if member is None and contract is None:
        return
    if type(member) is not UUID or type(contract) is not UUID:
        raise AppointmentAccessDeniedError
    selected = booking_coverage(
        clinic_id=clinic.pk,
        patient_id=prepared.enrollment.patient_id,
        membership_id=member,
        contract_id=contract,
    )
    if (
        selected.clinic_id != clinic.pk
        or selected.patient_id != prepared.enrollment.patient_id
        or selected.membership_id != member
        or selected.contract_id != contract
        or selected.payer_id != selected.contract_payer_id
    ):
        raise AppointmentAccessDeniedError
    timezone_key = clinic.timezone
    if timezone_key is None:
        raise AppointmentAccessDeniedError
    day = prepared.start_at.astimezone(ZoneInfo(timezone_key)).date()
    if (
        day < selected.valid_from
        or (selected.valid_to is not None and day > selected.valid_to)
        or day < selected.starts_on
        or (selected.ends_on is not None and day > selected.ends_on)
    ):
        code = "payer_refused"
        raise SchedulingRuleError(code)
    if request.initial_status == "scheduled" and (
        selected.eligibility_supported or selected.authorization_supported
    ):
        code = "payer_pending"
        raise SchedulingRuleError(code)
