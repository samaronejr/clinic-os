"""Savepoint-isolated appointment insert and named constraint mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
from django.db import IntegrityError, transaction

from apps.insurance.appointment_coverage import record_booking_coverage
from apps.scheduling.appointment_errors import (
    AppointmentAvailabilityError,
    AppointmentIdempotencyConflictError,
    AppointmentTerminalError,
    SlotConflict,
)
from apps.scheduling.appointment_values import (
    CreateAppointmentRequest,
    PreparedAppointment,
    replay_appointment,
)
from apps.scheduling.models import Appointment
from apps.scheduling.resource_errors import SchedulingRuleError

if TYPE_CHECKING:
    from datetime import datetime

    from apps.identity.models import Clinic


def insert_appointment(
    clinic: Clinic,
    request: CreateAppointmentRequest,
    prepared: PreparedAppointment,
) -> tuple[Appointment, bool]:
    """Insert once, recover a key loser, and map only known constraints."""
    try:
        with transaction.atomic():
            appointment = Appointment(
                organization_id=clinic.organization_id,
                clinic_id=request.clinic_id,
                patient_id=prepared.enrollment.patient_id,
                practitioner_id=request.practitioner_id,
                start_at=prepared.start_at,
                end_at=prepared.end_at,
                idempotency_key=request.idempotency_key,
                create_fingerprint=prepared.fingerprint,
                service_type_id=request.service_type_id,
                resource_ids=sorted(request.resource_ids),
                status=request.initial_status,
                series_id=request.series_id,
                series_index=request.series_index,
                payer_membership_id=request.payer_membership_id,
                payer_contract_id=request.payer_contract_id,
            )
            record_booking_coverage(
                clinic_id=request.clinic_id, appointment=appointment
            )
            appointment.save(force_insert=True)
    except IntegrityError as error:
        replay = replay_appointment(
            clinic.organization_id,
            request.idempotency_key,
            prepared.fingerprint,
        )
        if replay is not None:
            return replay, False
        constraint = _constraint_name(error)
        _resource_constraint(error, constraint)
        if constraint == "scheduling_appointment_org_idempotency_uniq":
            raise AppointmentIdempotencyConflictError from error
        if constraint in {
            "scheduling_appointment_scheduled_patient_excl",
            "scheduling_appointment_scheduled_practitioner_excl",
        }:
            raise SlotConflict from error
        if constraint == "scheduling_appointment_active_availability_check":
            raise AppointmentAvailabilityError from error
        raise
    appointment.refresh_from_db()
    return appointment, True


def _constraint_name(error: IntegrityError) -> str | None:
    cause = error.__cause__
    if not isinstance(cause, psycopg.Error):
        return None
    return cause.diag.constraint_name


def _resource_constraint(error: IntegrityError, constraint: str | None) -> None:
    codes = {
        "scheduling_payer_refused": "payer_refused",
        "scheduling_payer_pending": "payer_pending",
        "scheduling_resource_conflict": "resource_conflict",
        "scheduling_resource_capacity_excl": "resource_conflict",
        "scheduling_outside_template": "outside_template",
        "scheduling_holiday": "holiday",
        "scheduling_buffer_violation": "buffer_violation",
        "scheduling_z_buffer_practitioner_excl": "buffer_violation",
    }
    if constraint in codes:
        raise SchedulingRuleError(codes[constraint]) from error


def update_appointment_range(
    appointment: Appointment,
    *,
    start_at: datetime,
    end_at: datetime,
) -> Appointment:
    """Update one range in a savepoint and map only named appointment guards."""
    try:
        with transaction.atomic():
            appointment.start_at = start_at
            appointment.end_at = end_at
            record_booking_coverage(
                clinic_id=appointment.clinic_id, appointment=appointment
            )
            appointment.save(update_fields=("start_at", "end_at", "updated_at"))
    except IntegrityError as error:
        constraint = _constraint_name(error)
        _resource_constraint(error, constraint)
        if constraint in {
            "scheduling_appointment_scheduled_patient_excl",
            "scheduling_appointment_scheduled_practitioner_excl",
        }:
            raise SlotConflict from error
        if constraint == "scheduling_appointment_active_availability_check":
            raise AppointmentAvailabilityError from error
        if constraint == "scheduling_appointment_terminal_check":
            raise AppointmentTerminalError from error
        raise
    return appointment
