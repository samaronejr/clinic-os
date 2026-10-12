"""Opaque appointment discovery and immutable transition lock identity."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_locking import AppointmentWriteTarget
from apps.scheduling.models import Appointment

if TYPE_CHECKING:
    from uuid import UUID


def discover_transition_appointment(appointment_id: UUID) -> Appointment:
    """Pre-read only the opaque row needed to discover lock identities."""
    try:
        return Appointment.objects.get(pk=appointment_id)
    except Appointment.DoesNotExist as error:
        raise AppointmentAccessDeniedError from error


def transition_write_target(appointment: Appointment) -> AppointmentWriteTarget:
    """Freeze the clinic, patient, and practitioner gate identity."""
    return AppointmentWriteTarget(
        organization_id=appointment.organization_id,
        clinic_id=appointment.clinic_id,
        patient_id=appointment.patient_id,
        practitioner_ids=(appointment.practitioner_id,),
        resource_ids=tuple(appointment.resource_ids),
    )


def reload_transition_appointment(
    target: AppointmentWriteTarget,
    appointment_id: UUID,
    *,
    for_update: bool = False,
) -> Appointment:
    """Re-read only the row matching every immutable post-gate identity."""
    appointments = Appointment.objects.all()
    if for_update:
        appointments = appointments.select_for_update()
    try:
        return appointments.get(
            pk=appointment_id,
            organization_id=target.organization_id,
            clinic_id=target.clinic_id,
            patient_id=target.patient_id,
            practitioner_id__in=target.practitioner_ids,
        )
    except Appointment.DoesNotExist as error:
        raise AppointmentAccessDeniedError from error
