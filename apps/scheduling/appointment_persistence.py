"""Savepoint-isolated appointment insert and named constraint mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
from django.db import IntegrityError, transaction

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
            appointment = Appointment.objects.create(
                organization_id=clinic.organization_id,
                clinic_id=request.clinic_id,
                patient_id=prepared.enrollment.patient_id,
                practitioner_id=request.practitioner_id,
                start_at=prepared.start_at,
                end_at=prepared.end_at,
                idempotency_key=request.idempotency_key,
                create_fingerprint=prepared.fingerprint,
            )
    except IntegrityError as error:
        replay = replay_appointment(
            clinic.organization_id,
            request.idempotency_key,
            prepared.fingerprint,
        )
        if replay is not None:
            return replay, False
        constraint = _constraint_name(error)
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
    return appointment, True


def _constraint_name(error: IntegrityError) -> str | None:
    cause = error.__cause__
    if not isinstance(cause, psycopg.Error):
        return None
    return cause.diag.constraint_name


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
            appointment.save(update_fields=("start_at", "end_at", "updated_at"))
    except IntegrityError as error:
        constraint = _constraint_name(error)
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
